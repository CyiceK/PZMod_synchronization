"""
Parser debug logging helper.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import traceback
from typing import Optional

from config import cfg


def log_parse_debug(message: str, *, source: str = "parser", **fields: object) -> None:
    if not cfg.get(cfg.enable_debug):
        return
    path = _get_log_path()
    if path is None:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra = _format_fields(fields)
    line = f"[{timestamp}] [{source}] {message}"
    if extra:
        line = f"{line} {extra}"
    line = f"{line}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        return


def log_parse_exception(
    message: str,
    exc: Exception,
    *,
    source: str = "parser",
    **fields: object,
) -> None:
    extra_fields = dict(fields)
    extra_fields.setdefault("error_type", type(exc).__name__)
    extra_fields.setdefault("error_msg", str(exc))
    trace = _format_traceback(exc)
    if trace:
        extra_fields.setdefault("traceback", trace)
    log_parse_debug(message, source=source, error=repr(exc), **extra_fields)


def _get_log_path() -> Optional[Path]:
    try:
        root = Path(__file__).resolve().parents[1]
        return root / "logs" / f"parse_debug_{datetime.now().strftime('%Y-%m-%d')}.log"
    except Exception:
        return None


def _format_fields(fields: dict) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_stringify_value(value)}")
    return " ".join(parts)


def _format_traceback(exc: Exception) -> str:
    if exc.__traceback__ is None:
        return ""
    try:
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return ""
    return _stringify_value(text, max_len=2000)


def _stringify_value(value: object, max_len: int = 2000) -> str:
    try:
        text = str(value)
    except Exception:
        return "<unprintable>"
    if "\n" in text:
        text = text.replace("\n", "\\n")
    if "\r" in text:
        text = text.replace("\r", "\\r")
    if max_len > 0 and len(text) > max_len:
        text = f"{text[:max_len]}...<truncated>"
    return text
