"""Voice-specific rotating audit logger — separate from the main app log."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_audit: logging.Logger | None = None


def get_audit_logger() -> logging.Logger:
    global _audit
    if _audit is not None:
        return _audit
    os.makedirs("data/logs", exist_ok=True)
    logger = logging.getLogger("voice_audit")
    if not logger.handlers:
        handler = RotatingFileHandler(
            "data/logs/voice_audit.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    _audit = logger
    return _audit
