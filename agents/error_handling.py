"""Small, deterministic error classification shared by workflow nodes.

Classification does not retry by itself.  It makes a node's failure behaviour
explicit so callers can decide whether to retry, block, or retain a degraded
artifact without parsing an arbitrary traceback.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from agents.contracts import ErrorEnvelope


def classify_error(exc: BaseException | str, *, operation: str = "operation") -> ErrorEnvelope:
    """Map common local/remote failures to a user-actionable envelope."""
    message = str(exc).strip() or type(exc).__name__
    lowered = message.lower()

    if (
        isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
        or "timed out" in lowered or "timeout" in lowered
        or "connection reset" in lowered or "connection refused" in lowered
        or "temporarily unavailable" in lowered
    ):
        return ErrorEnvelope(
            category="TRANSIENT", code="OPERATION_TIMEOUT", message=f"{operation} timed out",
            cause=message[:500], retryable=True,
            next_action=f"Retry only {operation} after checking its timeout budget.",
        )
    if isinstance(exc, FileNotFoundError) or "not found" in lowered or "no such file" in lowered:
        return ErrorEnvelope(
            category="UNAVAILABLE", code="DEPENDENCY_UNAVAILABLE", message=f"{operation} dependency is unavailable",
            cause=message[:500], next_action="Install or configure the missing dependency, then rerun this node.",
        )
    if isinstance(exc, PermissionError) or "unauthorized" in lowered or "authentication" in lowered or " 401" in lowered:
        return ErrorEnvelope(
            category="PERMANENT", code="AUTHORIZATION_FAILED", message=f"{operation} authorization failed",
            cause=message[:500], next_action="Correct credentials or permissions before retrying.",
        )
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ErrorEnvelope(
            category="INVALID_INPUT", code="INVALID_INPUT", message=f"{operation} received invalid input",
            cause=message[:500], next_action="Correct the reported input field and rerun this node.",
        )
    return ErrorEnvelope(
        category="INTERNAL_BUG", code="UNEXPECTED_EXCEPTION", message=f"{operation} failed unexpectedly",
        cause=message[:500], next_action="Preserve this error and inspect the node input/output snapshot before retrying.",
    )


def error_to_evidence(error: ErrorEnvelope, *, operation: str) -> dict[str, Any]:
    """Return a serializable evidence record suitable for a contract."""
    payload = error.model_dump() if hasattr(error, "model_dump") else error.dict()
    return {"kind": "error", "operation": operation, **payload}


def retry_transient(operation: str, fn, *, max_attempts: int = 3, base_delay_seconds: float = 0.1):
    """Retry only classified transient failures of an idempotent operation."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            error = classify_error(exc, operation=operation)
            if not error.retryable or attempt == max_attempts:
                raise
            time.sleep(base_delay_seconds * (2 ** (attempt - 1)))
