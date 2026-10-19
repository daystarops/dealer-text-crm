"""Optional, privacy-safe Laminar tracing for pipeline orchestration."""
from __future__ import annotations

import math
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

TRACE_METADATA_ALLOWLIST = frozenset(
    {
        "run_id",
        "pipeline_stage",
        "status",
        "batch_size",
        "reviewed_count",
        "ranked_count",
        "newly_queued_count",
        "deduplicated_count",
        "skipped_count",
        "blocked_count",
        "retry_count",
        "duration_ms",
        "error_type",
        "recovery_succeeded",
        "search_scope",
        "automation_mode",
        "sender_mode_configured",
        "dry_run",
        "application_version",
    }
)

TRACE_STAGE_ALLOWLIST = frozenset(
    {
        "cdk.review",
        "browser.extraction",
        "extraction.schema_validation",
        "identity.vault_update",
        "privacy.input_gate",
        "codex.analysis",
        "analysis.schema_validation",
        "privacy.output_gate",
        "report.render",
        "queue.prepare",
        "recovery",
    }
)

_SAFE_STRING = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_PHONE = re.compile(r"(?:\+?\d[\d ()-]{7,}\d|\(\d{3}\)\s*\d{3}-\d{4})")
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_URL = re.compile(r"(?i)\b(?:https?|ftp|file)://|\bwww\.")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|\s)[./~][\\/])")

_initialization_attempted = False
_active = False
_laminar: Any = None


def _safe_value(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return abs(value) <= 1_000_000_000_000
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) <= 1_000_000_000.0
    if isinstance(value, str):
        return bool(
            _SAFE_STRING.fullmatch(value)
            and not _PHONE.search(value)
            and not _EMAIL.search(value)
            and not _URL.search(value)
            and not _PATH.search(value)
        )
    return False


def _safe_run_id(run_id: Any) -> str:
    return run_id if isinstance(run_id, str) and _safe_value(run_id) else "invalid-run-id"


def sanitize_trace_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    return {
        key: value
        for key, value in metadata.items()
        if key in TRACE_METADATA_ALLOWLIST and _safe_value(value)
    }


def initialize_observability() -> bool:
    global _initialization_attempted, _active, _laminar

    if _initialization_attempted:
        return _active

    _initialization_attempted = True

    if os.environ.get("CDK_OBSERVABILITY_ENABLED", "").casefold() != "true":
        return False
    if not os.environ.get("LMNR_PROJECT_API_KEY", "").strip():
        return False

    try:
        from lmnr import Laminar

        Laminar.initialize(
            project_api_key=os.environ.get("LMNR_PROJECT_API_KEY"),
            instruments=set(),
        )
        _laminar = Laminar
        _active = True
    except Exception:
        _laminar = None
        _active = False

    return _active


def _set_safe_span_attributes(span: Any, metadata: dict[str, Any]) -> None:
    safe = sanitize_trace_metadata(metadata)
    if not safe:
        return
    try:
        span.set_attributes(safe)
    except Exception:
        pass


def flush_observability(timeout_seconds: float = 3.0) -> None:
    """Best-effort bounded flush. Never delays or breaks the CRM pipeline."""
    if not _active or _laminar is None:
        return

    finished = threading.Event()

    def worker() -> None:
        try:
            _laminar.force_flush()
        except Exception:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="laminar-flush", daemon=True)
    thread.start()
    finished.wait(max(0.0, min(float(timeout_seconds), 10.0)))


@contextmanager
def observed_stage(
    name: str,
    *,
    run_id: str,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    safe_name = name if name in TRACE_STAGE_ALLOWLIST else "cdk.unknown"
    safe_run_id = _safe_run_id(run_id)
    safe_metadata = sanitize_trace_metadata(metadata)
    safe_metadata["run_id"] = safe_run_id
    started = time.perf_counter()

    if not initialize_observability() or _laminar is None:
        yield
        return

    try:
        manager = _laminar.start_as_current_span(
            name=safe_name,
            session_id=safe_run_id,
            tags=["cdk-lead-copilot"],
            metadata=safe_metadata,
        )
        span = manager.__enter__()
    except Exception:
        yield
        return

    try:
        yield
    except BaseException as error:
        _set_safe_span_attributes(
            span,
            {
                "status": "failed",
                "error_type": type(error).__name__,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            },
        )
        # Do not pass the original exception to Laminar. Its message or stack
        # may contain CRM data, subprocess arguments, or local paths.
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass
        raise
    else:
        _set_safe_span_attributes(
            span,
            {
                "status": "completed",
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            },
        )
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass
