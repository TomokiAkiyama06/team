"""JSON 形式のログ出力と秘密情報のマスキング。"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SECRET_PATTERNS = [
    re.compile(r"xoxe\.xox[a-z]-[A-Za-z0-9-]+"),  # Slack refresh token（xoxe.xoxp-…）
    re.compile(r"xox[a-z]-[A-Za-z0-9-]+"),        # Slack tokens（xoxb/xoxp/xoxa/xoxc/xoxe/…）
    re.compile(r"xapp-[A-Za-z0-9-]+"),            # Slack app-level token
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),         # Anthropic API key / Admin key
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?\s*(?:bearer|basic)\s+)[^\s\"']+"),
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    # key=value / "key": "value" 形式の秘密らしき値（input_tokens などの数値フィールドは対象外）
    re.compile(r"(?i)((?:api[_-]?key|secret|password|passwd|[a-z_]*_token|token)[\"']?\s*[:=]\s*[\"']?)"
               r"(?=[^\s\"',}]*[A-Za-z])[^\s\"',}]{8,}"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***REDACTED***", text)
    return text


class JsonFormatter(logging.Formatter):
    _RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in self._RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # ライブラリの冗長なログを抑える（httpx2 は URL/ヘッダを出すことがある）
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "slack_sdk", "slack_bolt", "watchdog"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
