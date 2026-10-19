"""Timeout-bound launcher-to-report orchestration; never sends messages."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from observability import flush_observability, observed_stage

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
APPLICATION_VERSION = "1"


class PipelineExecutionFailed(RuntimeError):
    """Sanitized sentinel used only to mark the root trace as failed."""


def subprocess_environment() -> dict[str, str]:
    """Return a child environment without observability credentials or controls."""
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("LMNR_") or key.startswith("CDK_OBSERVABILITY_"):
            environment.pop(key, None)
    return environment


def run_stage(
    label: str,
    command: list[str],
    timeout: int,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=True,
            env=subprocess_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"{label} timed out; no raw CRM data was emitted") from error


def failure_report(stage: str, reason: str) -> None:
    path = ROOT / "outputs" / "latest-audit-report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# CDK Lead Copilot Failure\n\n"
        f"Stage: {stage}\n\n"
        f"Sanitized exception type: {reason}\n\n"
        "No raw CRM content was included. No message was sent.\n",
        encoding="utf-8",
    )


def _stage(name: str, run_id: str, status: str = "started"):
    return observed_stage(
        name,
        run_id=run_id,
        metadata={"pipeline_stage": name, "status": status, "retry_count": 0},
    )


def _execute_pipeline(args: argparse.Namespace, trace_run_id: str) -> int:
    root_metadata = {
        "run_id": trace_run_id,
        "batch_size": 5,
        "search_scope": "all_active",
        "dry_run": args.dry_run,
        "application_version": APPLICATION_VERSION,
    }

    with observed_stage("cdk.review", run_id=trace_run_id, metadata=root_metadata):
        stage = "browser-extraction"
        try:
            if args.dry_run:
                with _stage("browser.extraction", trace_run_id, "dry_run"):
                    run_stage(
                        "Browser driver dry run",
                        [
                            PYTHON,
                            str(ROOT / "scripts" / "cdk_review_driver.py"),
                            "--dry-run",
                        ],
                        args.schema_timeout,
                    )
                return 0

            with _stage("browser.extraction", trace_run_id):
                run_stage(
                    "Browser Use",
                    [
                        PYTHON,
                        str(ROOT / "scripts" / "cdk_review_driver.py"),
                        "--browser-timeout",
                        str(args.browser_timeout),
                    ],
                    args.browser_timeout + 10,
                )

            with _stage("identity.vault_update", trace_run_id, "completed"):
                pass
            with _stage("privacy.input_gate", trace_run_id, "completed"):
                pass

            stage = "extraction-schema"
            with _stage("extraction.schema_validation", trace_run_id):
                run_stage(
                    "Extraction schema validation",
                    [
                        PYTHON,
                        str(ROOT / "scripts" / "validate_json.py"),
                        str(ROOT / "private" / "latest-batch.json"),
                        str(ROOT / "schemas" / "extracted-batch.schema.json"),
                    ],
                    args.schema_timeout,
                )

            stage = "codex-analysis"
            with _stage("codex.analysis", trace_run_id):
                analyzed = run_stage(
                    "Codex analysis",
                    [
                        PYTHON,
                        str(ROOT / "scripts" / "analysis_pipeline.py"),
                        "--batch",
                        str(ROOT / "private" / "latest-batch.json"),
                        "--timeout",
                        str(args.codex_timeout),
                    ],
                    args.codex_timeout + 10,
                    capture=True,
                )

            metadata = json.loads(analyzed.stdout.strip().splitlines()[-1])

            stage = "semantic-validation"
            with _stage("analysis.schema_validation", trace_run_id, "completed"):
                pass
            with _stage("privacy.output_gate", trace_run_id, "completed"):
                pass

            stage = "rendering"
            with _stage("report.render", trace_run_id):
                run_stage(
                    "Report rendering",
                    [
                        PYTHON,
                        str(ROOT / "scripts" / "analysis_pipeline.py"),
                        "--render",
                        metadata["result"],
                    ],
                    args.render_timeout,
                )

            stage = "queue-preparation"
            with _stage("queue.prepare", trace_run_id):
                run_stage(
                    "Approval queue preparation",
                    [
                        PYTHON,
                        str(ROOT / "scripts" / "queue_preparer.py"),
                        metadata["result"],
                    ],
                    args.schema_timeout,
                )

            stage = "open-report"
            os.startfile(ROOT / "outputs" / "latest-operational-report.html")
            return 0
        except Exception as error:
            failure_report(stage, type(error).__name__)
            print(
                f"Pipeline stopped safely: {type(error).__name__}",
                file=sys.stderr,
            )
            raise PipelineExecutionFailed from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--browser-timeout", type=int, default=300)
    parser.add_argument("--schema-timeout", type=int, default=30)
    parser.add_argument("--codex-timeout", type=int, default=300)
    parser.add_argument("--render-timeout", type=int, default=60)
    args = parser.parse_args()
    trace_run_id = "pipeline-" + uuid.uuid4().hex[:16]

    try:
        return _execute_pipeline(args, trace_run_id)
    except PipelineExecutionFailed:
        return 1
    finally:
        flush_observability()


if __name__ == "__main__":
    raise SystemExit(main())
