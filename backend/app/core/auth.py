import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import text


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${hashed}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, expected = encoded.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    tenant_id: str


async def principal(request: Request) -> Principal:
    token = request.cookies.get("agent_session", "")
    if not token or len(token) > 128:
        raise HTTPException(401, "authentication_required")
    async with request.app.state.resources.engine.connect() as conn:
        session = (
            (
                await conn.execute(
                    text(
                        "SELECT s.customer_id, s.csrf_hash,a.role,a.tenant_id FROM customer_sessions s "
                        "JOIN customer_accounts a ON a.id=s.customer_id WHERE s.token_hash=:token "
                        "AND s.expires_at>now() AND a.active"
                    ),
                    {"token": digest(token)},
                )
            )
            .mappings()
            .first()
        )
    if not session:
        raise HTTPException(401, "session_expired")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(digest(csrf), session["csrf_hash"]):
            raise HTTPException(403, "csrf_failed")
    return Principal(session["customer_id"], session["role"], session["tenant_id"])


async def identity(request: Request) -> str:
    actor = await principal(request)
    if actor.role != "customer":
        raise HTTPException(403, "customer_role_required")
    return actor.id


async def check_password(password, encoded):
    return await asyncio.to_thread(password_matches, password, encoded)


async def issue_session(request, response, customer_id):
    token = secrets.token_urlsafe(32)
    csrf = digest("csrf:" + token)
    async with request.app.state.resources.engine.begin() as conn:
        # Rotate any previous session in the same browser to prevent fixation.
        await conn.execute(
            text("DELETE FROM customer_sessions WHERE token_hash=:old OR expires_at<now()"),
            {"old": digest(request.cookies.get("agent_session", ""))},
        )
        await conn.execute(
            text(
                "INSERT INTO customer_sessions VALUES (:token,:customer,:csrf,now()+interval '8 hours')"
            ),
            {"token": digest(token), "customer": customer_id, "csrf": digest(csrf)},
        )
    response.set_cookie(
        "agent_session",
        token,
        httponly=True,
        secure=request.app.state.settings.session_secure,
        samesite="strict",
        path="/",
        max_age=28800,
    )
    response.headers["Cache-Control"] = "no-store"
    return csrf
