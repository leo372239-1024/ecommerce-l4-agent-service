import json
import logging
import re
import sys
from datetime import UTC, datetime

import structlog

SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}|\b[a-f0-9]{32}\.[A-Za-z0-9]{12,}\b")
SENSITIVE_KEYS = {"authorization", "api_key", "password", "token", "cookie", "secret"}


def redact_secrets(_, __, event_dict):
    def redact(value, key=""):
        if any(part in key.lower() for part in SENSITIVE_KEYS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {k: redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return SECRET_PATTERN.sub("[REDACTED]", value)
        return value

    # Apply the same PII policy to third-party logs without retaining raw exception text.
    from app.security.guards import redact as redact_pii

    return redact_pii(redact(event_dict))


class SafeJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "event": record.getMessage(),
            "logger": record.name,
            "level": record.levelname.lower(),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if record.exc_info and record.exc_info[0]:
            # Provider/DB exception text can include secrets or returned user content.
            event["error_type"] = record.exc_info[0].__name__
        return json.dumps(redact_secrets(None, None, event), ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJSONFormatter())
    logging.basicConfig(level=numeric_level, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Metrics cover request counts; do not log paths/query strings or provider HTTP bodies.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
    )
