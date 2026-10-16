from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_public_login_config_does_not_expose_identity_secrets():
    settings = Settings(_env_file=None, _secrets_dir=None, app_env="test")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/auth/config")
    assert response.status_code == 200
    assert response.json() == {"demo_login_enabled": True, "oidc_enabled": False}


def test_production_workspace_hides_demo_login():
    settings = Settings(
        _env_file=None,
        _secrets_dir=None,
        app_env="production",
        session_secure=True,
        cors_origins=["https://shop.test"],
        oidc_issuer="https://identity.test",
        oidc_authorization_endpoint="https://identity.test/authorize",
        oidc_token_endpoint="https://identity.test/token",
        oidc_jwks_uri="https://identity.test/keys",
        oidc_client_id="workspace-test",
        oidc_redirect_uri="https://shop.test/api/v1/auth/oidc/callback",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/auth/config")
    assert response.json() == {"demo_login_enabled": False, "oidc_enabled": True}
