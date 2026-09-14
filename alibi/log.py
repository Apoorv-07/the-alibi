"""alibi.log — application logging: one place, structured, and answerable to a request id.

Three rules, because the log is the only witness when the UI is not in front of the person debugging:

1. **Structured by default.** One JSON object per line (`ts level logger msg` plus whatever key/value
   pairs a call site attaches). The app also writes `audit` and `change_event` rows for state changes;
   those are *user-facing history*, not logs, and they must not be the only record of a crash.
2. **Never a secret, never a full document.** `redact()` drops obvious key material and truncates long
   values, so a call site can log an ingest failure without pasting a student's group chat into stdout.
3. **No silent no-op.** Before this module existed, `LOG_LEVEL` / `ALIBI_LOG_JSON` were read into `Config`
   and used by nothing — the same class of bug as `ALIBI_MAX_DOC_BYTES` and `OLLAMA_URL`: a knob that looks
   configured and does nothing (FINDINGS §24). `configure()` is called by the CLI, the web app and the
   tests, so the flags now select the actual handler.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization|cookie)")
_LONG = 800

_FORMATS = {
    "text": "%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
}


class JsonFormatter(logging.Formatter):
    """`ts level logger msg <extra…>` — one line, greppable, no stack-trace swallowing.

    Prefers `record.fields` (already redacted by the filter) and falls back to scanning `__dict__`, so the
    formatter is correct even when handed a record that never passed through the handler's filter."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields is None:
            fields = redact({k: v for k, v in record.__dict__.items()
                             if k not in _RESERVED and not k.startswith("_")})
        out.update(fields)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[:_LONG]
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)[:_LONG]
        return json.dumps(out, ensure_ascii=False, default=str)


def redact(value: Any) -> Any:
    """Best-effort: mask key-shaped fields, cap long strings. Not a guarantee — see the module docstring."""
    if isinstance(value, dict):
        return {k: ("[set]" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > _LONG:
        return value[:_LONG] + f"…(+{len(value) - _LONG} chars)"
    return value


class _FieldsFilter(logging.Filter):
    """Collect `extra={"x": 1}` payloads into `record.fields`, redacted.

    Non-destructive on purpose. The first version popped the keys off the record, so the *first* handler's
    filter ate fields that every later handler should also have seen — a second `StreamHandler` (a test, a
    file sink, a future Sentry handler) printed `{"ts":…, "msg":…}` with no fields at all and nothing
    explained why. `JsonFormatter` knows which names are LogRecord internals, so removing them here is
    redundant *and* lossy.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        fields = {k: v for k, v in record.__dict__.items()
                  if k not in _RESERVED and not k.startswith("_")}
        record.fields = redact(fields) if fields else {}   # type: ignore[attr-defined]
        return True


# Standard LogRecord attributes, so that "whatever the call site attached" really means *only* what the
# call site attached. Enumerated from an empty record rather than typed from memory — a missing name here
# silently sprays `relativeCreated`/`processName` through every JSON line.
_RESERVED = (set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
             | {"fields", "asctime", "message", "relativeCreated", "process", "processName",
                "thread", "threadName"}   # the last six are *computed* lazily by Formatters, so they are
                                          # not in __dict__ and must be listed explicitly — that is exactly
                                          # the failure mode of "enumerate the standard attributes".
             )

_configured = False


def configure(cfg: Any = None, *, force: bool = False) -> logging.Logger:
    """Install the handler once per process. `cfg` may be a `Config`, a level name, or nothing."""
    global _configured
    root = logging.getLogger("alibi")
    if _configured and not force:
        return root

    level = "INFO"
    json_out = True
    if isinstance(cfg, str):
        level = cfg.upper()
    elif cfg is not None:
        level = str(getattr(cfg, "log_level", "INFO")).upper()
        json_out = bool(getattr(cfg, "log_json", True))
        if getattr(cfg, "debug", False):
            level = "DEBUG"

    handler = logging.StreamHandler(sys.stdout if level != "DEBUG" else sys.stderr)
    handler.addFilter(_FieldsFilter())
    if json_out:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_FORMATS["text"]))
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    _configured = True
    return root


def get(name: str = "") -> logging.Logger:
    if not _configured:
        configure(None)
    return logging.getLogger("alibi" + (f".{name}" if name else ""))


def log_state(logger: logging.Logger, event: str, **fields: Any) -> None:
    """One structured line for a state transition. Deliberately not a substitute for `change_event`:
    logs say *when the process tried*, the ledger says *what the user's record now is*."""
    logger.info(event, extra={"event": event, **fields})
