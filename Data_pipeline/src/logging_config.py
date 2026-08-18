"""
Central logging setup. Every module should do:

    from src.logging_config import get_logger
    log = get_logger(__name__)

instead of calling logging.basicConfig() itself (previously every module did
this separately, which is harmless but means format/level can silently drift
between modules). configure_logging() is idempotent and safe to call more
than once — later calls are no-ops unless force=True.

In production (ENVIRONMENT=production or LOG_FORMAT=json) logs are emitted
as single-line JSON so they're easy to ship to a log aggregator (CloudWatch,
Datadog, Grafana Loki, Supabase logs, etc). Otherwise, plain text.
"""
import json
import logging
import sys
from datetime import datetime, timezone

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Allow callers to attach structured fields via logger.info(..., extra={"run_id": ...})
        for key, value in record.__dict__.items():
            if key in payload or key in logging.LogRecord.__dict__ or key.startswith("_"):
                continue
            if key in ("args", "msg", "exc_info", "exc_text", "stack_info"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = str(value)
        return json.dumps(payload, default=str)


def configure_logging(force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    # Imported lazily to avoid a circular import (config doesn't depend on logging).
    from src.config import get_settings
    settings = get_settings()

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # Quiet down noisy third-party loggers unless we're debugging.
    if settings.log_level.upper() != "DEBUG":
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
