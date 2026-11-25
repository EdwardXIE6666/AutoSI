"""Retry a callable a fixed number of attempts, re-raising the last error."""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")
_log = logging.getLogger(__name__)


def call_with_retries(fn: Callable[[], T], attempts: int = 2,
                      base_delay: float = 0.5,
                      on_retry: Callable[[Exception], None] | None = None) -> T:
    """Call fn up to `attempts` times (>=1). Re-raise the last exception on failure."""
    attempts = max(1, attempts)
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - intentionally broad; re-raised below
            last_exc = exc
            if i < attempts - 1:
                _log.warning("attempt %d/%d failed: %s; retrying",
                             i + 1, attempts, exc)
                if on_retry is not None:
                    on_retry(exc)
                time.sleep(base_delay * (i + 1))
    assert last_exc is not None
    raise last_exc
