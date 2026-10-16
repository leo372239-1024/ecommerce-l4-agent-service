import json
import re
import unicodedata

from prometheus_client import Counter

DECISIONS = Counter(
    "agent_guardrail_decisions_total", "Bounded safety decisions", ["stage", "decision"]
)
VERSION = "guardrails-v1"
RULES = {
    "authority_override": r"(?:忽略|绕过|跳过|取消|关闭).{0,12}(?:审批|权限校验|安全规则|系统指令)|(?:ignore|bypass|disable).{0,30}(?:approval|authorization|system instruction|previous instruction)",
    "role_forgery": r"(?:我是|把我当作|现在你是|你现在是).{0,12}(?:超级管理员|系统管理员|root|主管)|(?:system|developer)\s*(?:message|prompt)\s*:|<\|(?:im_start|system|developer)",
    "secret_exfiltration": r"(?:读取|泄露|输出|打印|发送|显示).{0,25}(?:密钥|api[_ -]?key|系统提示词|环境变量|/run/secrets|\.env)|(?:reveal|print|send|read).{0,30}(?:api[_ -]?key|system prompt|/run/secrets|environment variables)",
    "execution_request": r"(?:执行|运行).{0,12}(?:shell|bash|powershell|curl|wget|python代码)|(?:exec|eval|os\.system|subprocess)\s*\(",
}
PATTERNS = {key: re.compile(value, re.IGNORECASE) for key, value in RULES.items()}
SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{12,}|\b[a-f0-9]{32}\.[A-Za-z0-9]{12,}\b|\bBearer\s+[A-Za-z0-9._~-]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|password|client_secret|access_token|refresh_token)[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]{4,}"
)
PII = re.compile(
    r"(?<![A-Za-z0-9_-])(?:1[3-9]\d{9}|\d{17}[\dXx])(?![A-Za-z0-9_-])|(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)
SENSITIVE_KEYS = {
    "password",
    "authorization",
    "cookie",
    "api_key",
    "client_secret",
    "access_token",
    "refresh_token",
    "id_token",
    "phone",
    "mobile",
    "identity_number",
    "address",
    "shipping_address",
}


def valid_text(value):
    if any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("invalid_text_characters")
    return value


def signals(value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    normalized = "".join(
        c for c in unicodedata.normalize("NFKC", raw) if unicodedata.category(c) != "Cf"
    )
    return [key for key, pattern in PATTERNS.items() if pattern.search(normalized)]


def redact_text(value):
    return PII.sub(
        "[PII_REDACTED]",
        ASSIGNMENT.sub(lambda m: m[1] + "=[REDACTED]", SECRET.sub("[REDACTED]", value)),
    )


def redact(value, key=""):
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
                return json.dumps(redact(nested), ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        return redact_text(value)
    return value


def admit_message(value):
    valid_text(value)
    if signals(value):
        DECISIONS.labels("input", "blocked").inc()
        raise ValueError("guardrail_input_rejected")
    DECISIONS.labels("input", "accepted").inc()
    return redact_text(value)


def reference(value):
    # Preserve complete clean records; never repair a malicious record into an instruction.
    if signals(value):
        DECISIONS.labels("reference", "quarantined").inc()
        return None
    return {**redact(value), "trust_class": "untrusted_reference"}


def safe_output(value):
    DECISIONS.labels("output", "checked").inc()
    return redact(value)
