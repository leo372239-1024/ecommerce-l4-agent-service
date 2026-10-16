import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECRET_DIR = Path(os.getenv("APP_SECRETS_DIR", str(PROJECT_ROOT / ".secrets")))


class Settings(BaseSettings):
    app_name: str = "l4-ecommerce-customer-agent"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_user: str = "agent"
    postgres_db: str = "agent"
    postgres_password: SecretStr = SecretStr("")
    database_pool_size: int = Field(default=5, ge=1, le=50)
    redis_url: str = "redis://localhost:6380/0"
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    elasticsearch_enabled: bool = False
    elasticsearch_url: str = "http://localhost:9200"
    # Historical setting names retained for existing deployments; OpenAI-compatible LLM.
    zhipu_api_base: str = "https://openrouter.ai/api/v1"
    zhipu_api_key: SecretStr = SecretStr("")
    zhipu_model: str = "deepseek/deepseek-v4.1-flash"
    openrouter_api_key: SecretStr = SecretStr("")
    llm_temperature: float = Field(default=0.3, ge=0, le=2)
    llm_provider_only: list[str] = Field(
        default_factory=lambda: [
            "relace",
            "inference-net",
            "wafer",
            "sail-research",
            "decart",
            "morph",
            "dekallm",
            "deepinfra",
            "streamlake",
            "baidu",
            "deepseek",
            "gmicloud",
            "novita",
        ]
    )
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-api/v1"
    dashscope_embedding_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: SecretStr = SecretStr("")
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: Literal[64, 128, 256, 512, 768, 1024, 1536, 2048] = 1024
    reranker_model: str = "qwen3-rerank"
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    demo_alice_password: SecretStr = SecretStr("")
    demo_bob_password: SecretStr = SecretStr("")
    demo_supervisor_password: SecretStr = SecretStr("")
    demo_support_password: SecretStr = SecretStr("")
    mcp_service_key: SecretStr = SecretStr("")
    mcp_url: str = "http://localhost:8010/mcp"
    tool_routing_enabled: bool = False
    context_engineering_enabled: bool = False
    context_window_tokens: int = Field(default=32768, ge=4096, le=131072)
    context_output_reserve: int = Field(default=2048, ge=256, le=8192)
    context_tool_reserve: int = Field(default=4096, ge=256, le=16384)
    context_safety_reserve: int = Field(default=1024, ge=256, le=8192)
    session_secure: bool = False
    sandbox_enabled: bool = False
    sandbox_socket_path: str = "/run/sandbox/broker.sock"
    oidc_issuer: str = ""
    oidc_authorization_endpoint: str = ""
    oidc_token_endpoint: str = ""
    oidc_jwks_uri: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_uri: str = ""
    worker_concurrency: int = Field(default=2, ge=1, le=8)
    run_active_seconds: int = Field(default=180, ge=1, le=600)
    worker_lease_seconds: int = Field(default=30, ge=10, le=120)
    upload_max_file_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    upload_max_request_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    otel_enabled: bool = False
    otel_service_name: str = "agent-api"
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        secrets_dir=SECRET_DIR if SECRET_DIR.is_dir() else None,
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def parse_dimensions(cls, value):
        return int(value)

    @model_validator(mode="after")
    def secure_identity_config(self):
        values = [
            self.oidc_issuer,
            self.oidc_authorization_endpoint,
            self.oidc_token_endpoint,
            self.oidc_jwks_uri,
            self.oidc_client_id,
            self.oidc_redirect_uri,
        ]
        if any(values):
            if not all(values):
                raise ValueError("complete_oidc_configuration_required")
            for url in [values[i] for i in (0, 1, 2, 3, 5)]:
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or parsed.query
                ):
                    raise ValueError("oidc_fixed_https_urls_required")
        if self.app_env == "production" and (
            not all(values)
            or not self.session_secure
            or any(urlsplit(v).scheme != "https" for v in self.cors_origins)
        ):
            raise ValueError("production_requires_oidc_secure_cookie_and_https_origins")
        return self

    @property
    def llm_api_key(self) -> SecretStr:
        # Credentials are scoped to their gateway. Never reuse an official key at OpenRouter.
        if urlsplit(self.zhipu_api_base).hostname == "openrouter.ai":
            return self.openrouter_api_key
        return self.zhipu_api_key

    @property
    def llm_request_options(self) -> dict:
        if urlsplit(self.zhipu_api_base).hostname == "openrouter.ai":
            return {
                "reasoning": {"enabled": False},
                "provider": {
                    "only": list(self.llm_provider_only),
                    "allow_fallbacks": True,
                    "require_parameters": True,
                    "data_collection": "allow",
                },
            }
        # All evaluation groups use the same non-thinking tool-call protocol.
        return (
            {"thinking": {"type": "disabled"}}
            if urlsplit(self.zhipu_api_base).hostname == "api.deepseek.com"
            else {}
        )

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
