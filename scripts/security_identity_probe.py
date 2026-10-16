"""Container-only OIDC protocol fixture with real PostgreSQL and Redis, no external IdP."""

import asyncio
import json
import time
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from app.core.auth import digest
from app.core.config import Settings, get_settings
from app.core.resources import Resources
from app.main import create_app
from app.security import oidc


async def main():
    base = get_settings()
    settings = Settings(
        **{
            **base.model_dump(),
            "app_env": "production",
            "otel_enabled": False,
            "session_secure": True,
            "cors_origins": ["https://agent.example"],
            "oidc_issuer": "https://id.example",
            "oidc_authorization_endpoint": "https://id.example/auth",
            "oidc_token_endpoint": "https://id.example/token",
            "oidc_jwks_uri": "https://id.example/jwks",
            "oidc_client_id": "agent",
            "oidc_redirect_uri": "https://agent.example/api/v1/auth/oidc/callback",
        }
    )
    resources = Resources(base)
    customer = "s7-identity-" + uuid.uuid4().hex[:12]
    subject = "s7-subject-" + uuid.uuid4().hex
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="fixture", use="sig", alg="RS256")
    checks, transactions = [], {}
    active, mode = {}, "valid"
    original = httpx.AsyncClient

    def provider(request):
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.path == "/token" and request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["redirect_uri"] == [settings.oidc_redirect_uri]
        assert oidc.challenge(form["code_verifier"][0]) == active["code_challenge"][0]
        now = int(time.time())
        claims = {
            "iss": settings.oidc_issuer,
            "aud": "agent",
            "sub": subject if mode != "unbound" else "unknown-subject",
            "nonce": active["nonce"][0] if mode != "nonce" else "wrong-nonce",
            "iat": now,
            "exp": now + 300,
            "role": "supervisor",
            "tenant_id": "other-tenant",
        }
        return httpx.Response(
            200,
            json={
                "id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"}),
                "token_type": "Bearer",
                "access_token": "unused",
            },
        )

    try:
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO customer_accounts(id,username,password_hash,role,tenant_id) SELECT :id,:id,password_hash,'customer',:id FROM customer_accounts WHERE id='customer-alice'"
                ),
                {"id": customer},
            )
            await conn.execute(
                text(
                    "INSERT INTO identity_bindings(issuer,subject_hash,customer_id) VALUES(:iss,:sub,:id)"
                ),
                {"iss": settings.oidc_issuer, "sub": digest(subject), "id": customer},
            )
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            original(
                transport=httpx.ASGITransport(app),
                base_url="https://agent.example",
                follow_redirects=False,
            ) as client,
        ):
            response = await client.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "unused"}
            )
            assert response.status_code == 503
            checks.append("production_demo_login_disabled")

            async def start():
                nonlocal active
                response = await client.get("/api/v1/auth/oidc/login")
                assert response.status_code == 302
                active = parse_qs(urlsplit(response.headers["location"]).query)
                assert active["code_challenge_method"] == ["S256"] and active["scope"] == ["openid"]
                transactions[active["state"][0]] = active
                return active["state"][0]

            with patch.object(
                oidc.httpx,
                "AsyncClient",
                lambda **kw: original(transport=httpx.MockTransport(provider), **kw),
            ):
                state = await start()
                response = await client.get(
                    "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                )
                assert response.status_code == 303
                assert (
                    "HttpOnly" in response.headers["set-cookie"]
                    and "Secure" in response.headers["set-cookie"]
                )
                session = await client.get("/api/v1/auth/session")
                assert (
                    session.status_code == 200
                    and session.json()["role"] == "customer"
                    and session.json()["tenant_id"] == customer
                )
                csrf = session.json()["csrf_token"]
                checks.append("signed_code_pkce_login_local_role_binding_secure_session")
                replay = await client.get(
                    "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                )
                assert replay.status_code == 401
                checks.append("one_use_state_replay_rejected")
                old_token = client.cookies.get("agent_session")
                state = await start()
                response = await client.get(
                    "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                )
                assert (
                    response.status_code == 303 and client.cookies.get("agent_session") != old_token
                )
                async with resources.engine.connect() as conn:
                    assert not await conn.scalar(
                        text("SELECT 1 FROM customer_sessions WHERE token_hash=:token"),
                        {"token": digest(old_token)},
                    )
                checks.append("login_rotates_previous_session")
                for mode in ("unbound", "nonce"):
                    state = await start()
                    response = await client.get(
                        "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                    )
                    assert response.status_code == (403 if mode == "unbound" else 401)
                checks.append("unbound_subject_and_nonce_rejected")
                mode = "valid"
                state = await start()
                async with original(
                    transport=httpx.ASGITransport(app), base_url="https://agent.example"
                ) as stranger:
                    response = await stranger.get(
                        "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                    )
                    assert response.status_code == 401
                checks.append("browser_binding_rejects_login_csrf")
                state = await start()
                await app.state.resources.redis.delete("oidc:" + digest(state))
                response = await client.get(
                    "/api/v1/auth/oidc/callback", params={"state": state, "code": "fixture"}
                )
                assert response.status_code == 401
                checks.append("expired_state_rejected")
            # Reuse the latest valid session with its current CSRF, not the first session.
            csrf = (await client.get("/api/v1/auth/session")).json()["csrf_token"]
            response = await client.post(
                "/api/v1/sandbox/execute",
                json={"tool": "money_total", "arguments": {"amounts": ["1.00"]}},
            )
            assert response.status_code == 403
            response = await client.post(
                "/api/v1/sandbox/execute",
                headers={"X-CSRF-Token": csrf},
                json={"tool": "money_total", "arguments": {"amounts": ["0.10", "0.20"]}},
            )
            assert response.status_code == 200 and response.json()["total"] == "0.30"
            checks.append("production_session_csrf_and_real_broker")
            async with resources.engine.begin() as conn:
                await conn.execute(
                    text("UPDATE customer_accounts SET active=false WHERE id=:id"),
                    {"id": customer},
                )
            assert (await client.get("/api/v1/auth/session")).status_code == 401
            checks.append("account_revocation_applies_to_existing_session")
        print(
            json.dumps(
                {
                    "checks": checks,
                    "external_idp": False,
                    "identity_http": "signed_mock_transport",
                    "postgres_redis_sandbox": "real",
                }
            )
        )
    finally:
        async with resources.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM identity_bindings WHERE customer_id=:id"), {"id": customer}
            )
            await conn.execute(
                text("DELETE FROM customer_sessions WHERE customer_id=:id"), {"id": customer}
            )
            await conn.execute(text("DELETE FROM customer_accounts WHERE id=:id"), {"id": customer})
        for state in transactions:
            await resources.redis.delete("oidc:" + digest(state))
        await resources.close()


asyncio.run(main())
