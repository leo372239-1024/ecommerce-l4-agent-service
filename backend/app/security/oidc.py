"""Fixed issuer, Code + S256 PKCE, one-use browser-bound state, local account binding."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.auth import Principal, digest, issue_session, principal

router = APIRouter()


def challenge(verifier):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


async def limited_json(http, method, url, **kwargs):
    async with http.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 262144:
                raise ValueError("identity_response_limit")
        return json.loads(raw)


def verify_token(token, jwks, settings, nonce):
    if not isinstance(token, str) or len(token) > 16384:
        raise ValueError("identity_token_limit")
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("identity_algorithm_rejected")
    if not isinstance(jwks, dict):
        raise TypeError("identity_keys_invalid")
    keys = jwks.get("keys", [])
    if not isinstance(keys, list) or len(keys) > 32:
        raise ValueError("identity_keys_limit")
    selected = [
        k
        for k in keys
        if isinstance(k, dict)
        and k.get("kid") == header["kid"]
        and k.get("kty") == "RSA"
        and k.get("use", "sig") == "sig"
        and k.get("alg", "RS256") == "RS256"
    ]
    if len(selected) != 1:
        raise ValueError("identity_key_unknown")
    key = jwt.PyJWK.from_dict(selected[0], algorithm="RS256").key
    if key.key_size < 2048:
        raise ValueError("identity_weak_key")
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=settings.oidc_client_id,
        issuer=settings.oidc_issuer,
        leeway=30,
        options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
    )
    if not isinstance(claims["nonce"], str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise ValueError("identity_nonce_mismatch")
    aud = claims["aud"]
    if (
        isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != settings.oidc_client_id
    ) or claims.get("azp", settings.oidc_client_id) != settings.oidc_client_id:
        raise ValueError("identity_authorized_party_mismatch")
    if (
        type(claims["iat"]) not in (int, float)
        or type(claims["exp"]) not in (int, float)
        or claims["iat"] < time.time() - 600
        or claims["exp"] > time.time() + 86400
    ):
        raise ValueError("identity_time_invalid")
    if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255:
        raise ValueError("identity_subject_invalid")
    return claims["sub"]


async def admission(request):
    try:
        n = await request.app.state.resources.redis.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
            1,
            "auth:oidc:" + digest(request.client.host if request.client else "unknown"),
        )
    except (RedisError, TimeoutError):
        raise HTTPException(503, "identity_temporarily_unavailable") from None
    if n > 40:
        raise HTTPException(429, "identity_rate_limit", headers={"Retry-After": "60"})


@router.get("/auth/oidc/login")
async def start(request: Request):
    settings = request.app.state.settings
    if not settings.oidc_issuer:
        raise HTTPException(503, "identity_provider_not_configured")
    await admission(request)
    state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
    transaction = {"nonce": nonce, "verifier": verifier, "browser": digest(browser)}
    try:
        await request.app.state.resources.redis.set(
            "oidc:" + digest(state), json.dumps(transaction), ex=300, nx=True
        )
    except (RedisError, TimeoutError):
        raise HTTPException(503, "identity_temporarily_unavailable") from None
    response = RedirectResponse(
        settings.oidc_authorization_endpoint
        + "?"
        + urlencode(
            {
                "client_id": settings.oidc_client_id,
                "redirect_uri": settings.oidc_redirect_uri,
                "response_type": "code",
                "scope": "openid",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
            }
        ),
        status_code=302,
    )
    response.set_cookie(
        "agent_oidc",
        browser,
        httponly=True,
        secure=settings.session_secure,
        samesite="lax",
        path="/api/v1/auth/oidc",
        max_age=300,
    )
    return response


@router.get("/auth/oidc/callback")
async def callback(
    request: Request,
    code: Annotated[str, Query(min_length=1, max_length=2048)],
    state: Annotated[str, Query(min_length=40, max_length=128)],
):
    settings = request.app.state.settings
    if not settings.oidc_issuer:
        raise HTTPException(503, "identity_provider_not_configured")
    await admission(request)
    try:
        saved = await request.app.state.resources.redis.getdel("oidc:" + digest(state))
    except (RedisError, TimeoutError):
        raise HTTPException(503, "identity_temporarily_unavailable") from None
    if not saved:
        raise HTTPException(401, "identity_transaction_expired")
    tx = json.loads(saved)
    if not hmac.compare_digest(tx["browser"], digest(request.cookies.get("agent_oidc", ""))):
        raise HTTPException(401, "identity_browser_mismatch")
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as http:
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.oidc_redirect_uri,
                "client_id": settings.oidc_client_id,
                "code_verifier": tx["verifier"],
            }
            auth = (
                httpx.BasicAuth(
                    settings.oidc_client_id, settings.oidc_client_secret.get_secret_value()
                )
                if settings.oidc_client_secret.get_secret_value()
                else None
            )
            token = await limited_json(
                http, "POST", settings.oidc_token_endpoint, data=data, auth=auth
            )
            jwks = await limited_json(http, "GET", settings.oidc_jwks_uri)
        subject = verify_token(token["id_token"], jwks, settings, tx["nonce"])
    except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
        raise HTTPException(401, "identity_token_rejected") from None
    async with request.app.state.resources.engine.connect() as conn:
        account = await conn.scalar(
            text(
                "SELECT a.id FROM identity_bindings b JOIN customer_accounts a ON a.id=b.customer_id "
                "WHERE b.issuer=:issuer AND b.subject_hash=:subject AND a.active"
            ),
            {"issuer": settings.oidc_issuer, "subject": digest(subject)},
        )
    if not account:
        raise HTTPException(403, "identity_account_not_bound")
    response = RedirectResponse("/", status_code=303)
    await issue_session(request, response, account)
    response.delete_cookie(
        "agent_oidc",
        path="/api/v1/auth/oidc",
        secure=settings.session_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/config")
async def auth_config(request: Request):
    settings = request.app.state.settings
    return {
        "demo_login_enabled": settings.app_env != "production",
        "oidc_enabled": bool(settings.oidc_issuer),
    }


@router.get("/auth/session")
async def session(request: Request, actor: Annotated[Principal, Depends(principal)]):
    csrf = digest("csrf:" + request.cookies["agent_session"])
    async with request.app.state.resources.engine.begin() as conn:
        # Also upgrades existing development sessions from the previous random-CSRF format.
        await conn.execute(
            text("UPDATE customer_sessions SET csrf_hash=:csrf WHERE token_hash=:token"),
            {"csrf": digest(csrf), "token": digest(request.cookies["agent_session"])},
        )
    return {
        "customer_id": actor.id,
        "role": actor.role,
        "tenant_id": actor.tenant_id,
        "csrf_token": csrf,
        "demo_only": request.app.state.settings.app_env != "production",
    }
