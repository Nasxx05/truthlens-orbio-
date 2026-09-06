"""Structured logging.

Two goals:

  * **Machine-readable failures.** Scrape blocks, API errors, filter outcomes
    and LLM failures are the things anyone operating this will need to count
    and alert on, so they are emitted as JSON with stable field names rather
    than as prose that has to be regex-matched later.
  * **No personal data.** Reviews are public, but a reviewer's *name* is
    identifying and this service has no reason to record who wrote what.
    Author names, emails and long review bodies are scrubbed before a record
    is written, in the formatter rather than at each call site — relying on
    every future log call to remember would guarantee a leak eventually.

Set ``LOG_FORMAT=json`` for structured output, or leave it for human-readable
lines in development.
"""

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict

# Fields carried on a LogRecord by the stdlib. Anything else was added by the
# caller through `extra=` and belongs in the structured output.
_STANDARD = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
}

# Keys whose values are personal even though the surrounding data is public.
_SENSITIVE_KEYS = {"author", "reviewer", "reviewer_name", "email", "user", "username", "profile"}

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Long digit runs: order numbers, phone numbers, card fragments.
_LONG_DIGITS = re.compile(r"\b\d{9,}\b")

MAX_TEXT = 200


def scrub(value: Any, key: str = "") -> Any:
    """Remove personal data from a value about to be logged.

    Applied recursively so a nested review dict cannot smuggle an author name
    through in a structured field.
    """
    if key.lower() in _SENSITIVE_KEYS:
        return "[redacted]"

    if isinstance(value, str):
        cleaned = _EMAIL.sub("[email]", value)
        cleaned = _LONG_DIGITS.sub("[digits]", cleaned)
        if len(cleaned) > MAX_TEXT:
            cleaned = cleaned[:MAX_TEXT] + "…"
        return cleaned

    if isinstance(value, dict):
        return {inner: scrub(item, inner) for inner, item in value.items()}

    if isinstance(value, (list, tuple)):
        # Bound the fan-out: nobody needs 150 reviews in a log line.
        return [scrub(item, key) for item in list(value)[:20]]

    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD or key.startswith("_"):
                continue
            payload[key] = scrub(value, key)

        if record.exc_info:
            # The type and message are useful; the full traceback is noise in a
            # log aggregator and can carry scraped content in frames.
            exc_type, exc_value, _tb = record.exc_info
            payload["error_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["error"] = scrub(str(exc_value))

        return json.dumps(payload, default=str, separators=(",", ":"))


class HumanFormatter(logging.Formatter):
    """Readable lines for development, with extras appended."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: scrub(value, key)
            for key, value in record.__dict__.items()
            if key not in _STANDARD and not key.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{key}={value!r}" for key, value in extras.items())
            return f"{base} | {rendered}"
        return base


def configure_logging() -> None:
    """Install the configured formatter on the root logger."""
    level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
    structured = (os.getenv("LOG_FORMAT", "") or "").strip().lower() == "json"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if structured
        else HumanFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    # Replace rather than add: repeated configuration (reload, worker restart)
    # would otherwise duplicate every line.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at INFO and say nothing this service needs.
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
