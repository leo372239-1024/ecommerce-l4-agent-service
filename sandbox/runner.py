"""Fixed computations only. Never eval, import supplied modules or execute supplied code."""

import csv
import io
import json
import re
import sys
from decimal import Decimal


def execute(body):
    if not isinstance(body, dict) or set(body) != {"tool", "arguments"}:
        raise ValueError("invalid_contract")
    args = body["arguments"]
    if not isinstance(args, dict):
        raise TypeError("invalid_contract")
    if body["tool"] == "money_total":
        values = args.get("amounts")
        if set(args) != {"amounts"} or not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise ValueError("invalid_amounts")
        if any(
            not isinstance(v, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?", v)
            for v in values
        ):
            raise ValueError("invalid_money_precision")
        total = sum((Decimal(v) for v in values), Decimal(0))
        return {"total": format(total, ".2f"), "count": len(values), "business_effect": "none"}
    if body["tool"] in {"csv_summary", "text_stats"}:
        content = args.get("text")
        limit = 32000 if body["tool"] == "csv_summary" else 4000
        if (
            set(args) != {"text"}
            or not isinstance(content, str)
            or not 1 <= len(content) <= limit
            or "\x00" in content
        ):
            raise ValueError("invalid_text")
        if body["tool"] == "text_stats":
            return {
                "characters": len(content),
                "lines": len(content.splitlines()),
                "words": len(content.split()),
                "business_effect": "none",
            }
        rows = list(csv.reader(io.StringIO(content), strict=True))
        if (
            not 1 <= len(rows) <= 200
            or not 1 <= len(rows[0]) <= 20
            or any(len(r) != len(rows[0]) for r in rows)
        ):
            raise ValueError("invalid_table_shape")
        return {"rows": len(rows), "columns": len(rows[0]), "business_effect": "none"}
    raise ValueError("tool_not_allowed")


if __name__ == "__main__":
    # Linux-only child hard limits supplement the container cgroup limits.
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (1, 1))
    resource.setrlimit(resource.RLIMIT_AS, (64 * 1024 * 1024, 64 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        body = json.loads(sys.stdin.buffer.read(65537))
        value = {"ok": True, "result": execute(body)}
    except (ValueError, TypeError, csv.Error, MemoryError):
        value = {"ok": False, "error": "sandbox_request_rejected"}
    sys.stdout.write(json.dumps(value))
