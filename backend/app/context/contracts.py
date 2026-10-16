import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.agent.contracts import StrictModel
from app.security.guards import redact_text, valid_text


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class DocumentInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=1_000_000)
    scope: Literal["private", "tenant"] = "private"
    kind: Literal["reference", "policy"] = "reference"
    visible_roles: list[Literal["customer", "support", "supervisor"]] = Field(
        default_factory=lambda: ["customer", "support", "supervisor"], min_length=1, max_length=3
    )
    effective_at: datetime | None = Field(default=None, strict=False)
    expires_at: datetime | None = Field(default=None, strict=False)

    @field_validator("text", "title")
    @classmethod
    def safe_text(cls, value):
        return redact_text(valid_text(value))

    @field_validator("effective_at", "expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value and value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_at and self.expires_at and self.expires_at <= self.effective_at:
            raise ValueError("invalid_effective_interval")
        if not self.text.strip() or not self.title.strip():
            raise ValueError("empty_document")
        return self


class SearchInput(StrictModel):
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=5, ge=1, le=8)

    @field_validator("query")
    @classmethod
    def safe_query(cls, value):
        return redact_text(valid_text(value))


class PreferenceInput(StrictModel):
    key: Literal["response_style", "contact_channel", "language"]
    value: Literal["concise", "detailed", "in_app", "email", "zh-CN", "en"]
    retain_days: int = Field(default=90, ge=1, le=365)

    @model_validator(mode="after")
    def allowed_pair(self):
        allowed = {
            "response_style": {"concise", "detailed"},
            "contact_channel": {"in_app", "email"},
            "language": {"zh-CN", "en"},
        }
        if self.value not in allowed[self.key]:
            raise ValueError("invalid_preference_value")
        return self


def chunk_text(content, size=700, overlap=100, max_chunks=1500):
    """Bounded Unicode slices; offsets are stable and never contain partial JSON contracts."""
    content = content.replace("\r\n", "\n").replace("\x00", "").strip()
    if not content or len(content) > 1_000_000:
        raise ValueError("document_text_limit")
    if size <= overlap or overlap < 0:
        raise ValueError("invalid_chunk_config")
    result = []
    for start in range(0, len(content), size - overlap):
        if len(result) >= max_chunks:
            raise ValueError("document_chunk_limit")
        part = content[start : start + size].strip()
        if part:
            result.append(part)
        if start + size >= len(content):
            break
    return result


def parse_upload(raw, filename):
    """First ingestion contract supports UTF-8 text/Markdown only, no executable formats."""
    if not re.search(r"\.(txt|md)$", filename or "", re.IGNORECASE):
        raise ValueError("supported_formats_txt_md")
    try:
        value = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("utf8_required") from None
    value = redact_text(valid_text(value))
    chunk_text(value)  # Reject oversized/empty content before committing an ingestion job.
    return value
