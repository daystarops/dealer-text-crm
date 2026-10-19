from __future__ import annotations

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import observability
import pipeline_runner


class FakeSpan:
    def __init__(self, client, fail_exit=False):
        self.client = client
        self.fail_exit = fail_exit
        self.attributes = {}

    def __enter__(self):
        return self

    def set_attributes(self, attributes):
        self.attributes.update(attributes)
        self.client.attributes.append(dict(attributes))

    def __exit__(self, *args):
        self.client.exits += 1
        self.client.exit_args.append(args)
        if self.fail_exit:
            raise RuntimeError("exporter leaked raw customer message")


class FakeLaminar:
    initializes = 0
    spans = []
    exits = 0
    exit_args = []
    attributes = []
    force_flushes = 0
    fail_initialize = False
    fail_start = False
    fail_exit = False
    fail_flush = False
    initialize_kwargs = []

    @classmethod
    def initialize(cls, project_api_key=None, **kwargs):
        cls.initializes += 1
        cls.initialize_kwargs.append({"project_api_key": project_api_key, **kwargs})
        if cls.fail_initialize:
            raise RuntimeError("initialization failure with secret")

    @classmethod
    def start_as_current_span(cls, **kwargs):
        if cls.fail_start:
            raise RuntimeError("export failure with customer text")
        cls.spans.append(kwargs)
        return FakeSpan(cls, cls.fail_exit)

    @classmethod
    def force_flush(cls):
        cls.force_flushes += 1
        if cls.fail_flush:
            raise RuntimeError("flush failed with sensitive text")


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        observability._initialization_attempted = False
        observability._active = False
        observability._laminar = None

        FakeLaminar.initializes = 0
        FakeLaminar.spans = []
        FakeLaminar.exits = 0
        FakeLaminar.exit_args = []
        FakeLaminar.attributes = []
        FakeLaminar.force_flushes = 0
        FakeLaminar.fail_initialize = False
        FakeLaminar.fail_start = False
        FakeLaminar.fail_exit = False
        FakeLaminar.fail_flush = False
        FakeLaminar.initialize_kwargs = []

        self.env = patch.dict(
            os.environ,
            {
                "CDK_OBSERVABILITY_ENABLED": "true",
                "LMNR_PROJECT_API_KEY": "test-secret",
            },
            clear=False,
        )
        self.env.start()
        self.module = patch.dict(
            sys.modules,
            {"lmnr": types.SimpleNamespace(Laminar=FakeLaminar)},
        )
        self.module.start()

    def tearDown(self):
        self.module.stop()
        self.env.stop()

    def test_disabled_is_noop(self):
        os.environ["CDK_OBSERVABILITY_ENABLED"] = "false"
        with observability.observed_stage(
            "browser.extraction",
            run_id="run-1",
            metadata={"status": "ok"},
        ):
            value = 7
        self.assertEqual(value, 7)
        self.assertEqual(FakeLaminar.initializes, 0)
        self.assertEqual(FakeLaminar.spans, [])

    def test_missing_key_and_missing_lmnr_are_noop(self):
        os.environ.pop("LMNR_PROJECT_API_KEY")
        self.assertFalse(observability.initialize_observability())

        observability._initialization_attempted = False
        sys.modules.pop("lmnr", None)
        os.environ["LMNR_PROJECT_API_KEY"] = "present"
        with patch("builtins.__import__", side_effect=ImportError("missing")):
            self.assertFalse(observability.initialize_observability())

    def test_initialization_failure_and_once_only(self):
        FakeLaminar.fail_initialize = True
        self.assertFalse(observability.initialize_observability())
        self.assertFalse(observability.initialize_observability())
        self.assertEqual(FakeLaminar.initializes, 1)

    def test_exporter_failures_do_not_change_result(self):
        FakeLaminar.fail_start = True
        with observability.observed_stage(
            "browser.extraction",
            run_id="run-1",
        ):
            result = "same"
        self.assertEqual(result, "same")

        observability._initialization_attempted = False
        observability._active = False
        FakeLaminar.fail_start = False
        FakeLaminar.fail_exit = True
        with observability.observed_stage(
            "browser.extraction",
            run_id="run-1",
        ):
            result = "same"
        self.assertEqual(result, "same")

    def test_initialization_at_most_once(self):
        self.assertTrue(observability.initialize_observability())
        self.assertTrue(observability.initialize_observability())
        self.assertEqual(FakeLaminar.initializes, 1)

    def test_metadata_allowlist_and_nested_spans(self):
        dirty = {
            "status": "ok",
            "reviewed_count": 5,
            "duration_ms": 1.5,
            "phone": "212-555-1234",
            "notes": "call customer now",
        }
        with observability.observed_stage(
            "cdk.review",
            run_id="run-1",
            metadata=dirty,
        ):
            with observability.observed_stage(
                "codex.analysis",
                run_id="run-1",
                metadata={
                    "pipeline_stage": "codex.analysis",
                    "ranked_count": 5,
                },
            ):
                pass

        self.assertEqual(len(FakeLaminar.spans), 2)
        for span in FakeLaminar.spans:
            self.assertLessEqual(
                set(span["metadata"]),
                observability.TRACE_METADATA_ALLOWLIST,
            )
        self.assertNotIn("phone", FakeLaminar.spans[0]["metadata"])
        self.assertNotIn("notes", FakeLaminar.spans[0]["metadata"])

    def test_sensitive_strings_and_unknown_keys_are_removed(self):
        values = {
            "status": "Jane Smith",
            "error_type": "customer said buy now",
            "run_id": "212-555-1234",
            "pipeline_stage": "a@b.com",
            "automation_mode": "https://example.com",
            "application_version": r"C:\Users\Jane\file",
            "unknown": "ok",
        }
        self.assertEqual(observability.sanitize_trace_metadata(values), {})

    def test_raw_exception_is_not_passed_to_laminar(self):
        with self.assertRaisesRegex(ValueError, "customer phone"):
            with observability.observed_stage(
                "browser.extraction",
                run_id="run-1",
                metadata={"error_type": "ValueError"},
            ):
                raise ValueError("customer phone 212-555-1234")

        self.assertEqual(len(FakeLaminar.exit_args), 1)
        self.assertEqual(FakeLaminar.exit_args[0], (None, None, None))
        exported = repr(FakeLaminar.spans) + repr(FakeLaminar.exit_args)
        self.assertNotIn("212-555-1234", exported)
        self.assertNotIn("customer phone", exported)
        self.assertIn(
            {"status": "failed", "error_type": "ValueError"},
            [
                {k: v for k, v in item.items() if k != "duration_ms"}
                for item in FakeLaminar.attributes
            ],
        )

    def test_customer_identity_and_message_content_never_reach_client(self):
        with observability.observed_stage(
            "browser.extraction",
            run_id="run-1",
            metadata={
                "status": "Jane Smith",
                "message_body": "Buy now",
                "crm_notes": "private",
            },
        ):
            pass
        exported = repr(FakeLaminar.spans)
        self.assertNotIn("Jane Smith", exported)
        self.assertNotIn("Buy now", exported)
        self.assertNotIn("private", exported)

    def test_results_identical_enabled_and_disabled(self):
        def execute(enabled):
            os.environ["CDK_OBSERVABILITY_ENABLED"] = (
                "true" if enabled else "false"
            )
            observability._initialization_attempted = False
            observability._active = False
            observability._laminar = None
            with observability.observed_stage(
                "cdk.review",
                run_id="run-1",
                metadata={"dry_run": True},
            ):
                return {"result": 0}

        self.assertEqual(execute(False), execute(True))

    def test_unknown_stage_and_unsafe_run_id_are_not_exported(self):
        with observability.observed_stage(
            "Jane Smith 212-555-1234",
            run_id="Jane Smith 212-555-1234",
            metadata={"status": "ok"},
        ):
            pass

        self.assertEqual(FakeLaminar.spans[0]["name"], "cdk.unknown")
        self.assertEqual(
            FakeLaminar.spans[0]["session_id"],
            "invalid-run-id",
        )
        self.assertEqual(
            FakeLaminar.spans[0]["metadata"]["run_id"],
            "invalid-run-id",
        )

    def test_completion_status_and_duration_are_recorded_safely(self):
        with observability.observed_stage(
            "report.render",
            run_id="run-1",
        ):
            pass

        combined = {}
        for attributes in FakeLaminar.attributes:
            combined.update(attributes)

        self.assertEqual(combined["status"], "completed")
        self.assertIsInstance(combined["duration_ms"], float)
        self.assertGreaterEqual(combined["duration_ms"], 0.0)

    def test_flush_is_best_effort_and_bounded(self):
        self.assertTrue(observability.initialize_observability())
        observability.flush_observability(timeout_seconds=0.25)
        self.assertEqual(FakeLaminar.force_flushes, 1)

        FakeLaminar.fail_flush = True
        observability.flush_observability(timeout_seconds=0.25)
        self.assertEqual(FakeLaminar.force_flushes, 2)

    def test_manual_only_initialization_disables_auto_instruments(self):
        self.assertTrue(observability.initialize_observability())
        self.assertEqual(FakeLaminar.initializes, 1)
        self.assertEqual(
            FakeLaminar.initialize_kwargs[0]["instruments"],
            set(),
        )

    def test_child_processes_do_not_inherit_observability_secrets(self):
        with patch.dict(
            os.environ,
            {
                "LMNR_PROJECT_API_KEY": "secret-key",
                "LMNR_BASE_URL": "https://example.invalid",
                "CDK_OBSERVABILITY_ENABLED": "true",
                "SAFE_SETTING": "kept",
            },
            clear=False,
        ):
            environment = pipeline_runner.subprocess_environment()

        self.assertEqual(environment["SAFE_SETTING"], "kept")
        self.assertFalse(any(key.startswith("LMNR_") for key in environment))
        self.assertFalse(
            any(key.startswith("CDK_OBSERVABILITY_") for key in environment)
        )

    def test_run_stage_uses_scrubbed_child_environment(self):
        completed = __import__("subprocess").CompletedProcess(["tool"], 0)
        with patch.dict(
            os.environ,
            {
                "LMNR_PROJECT_API_KEY": "secret-key",
                "CDK_OBSERVABILITY_ENABLED": "true",
            },
            clear=False,
        ), patch.object(
            pipeline_runner.subprocess,
            "run",
            return_value=completed,
        ) as mocked:
            pipeline_runner.run_stage("test", ["tool"], 1)

        child_environment = mocked.call_args.kwargs["env"]
        self.assertNotIn("LMNR_PROJECT_API_KEY", child_environment)
        self.assertNotIn("CDK_OBSERVABILITY_ENABLED", child_environment)

    def test_pipeline_failure_marks_root_span_failed(self):
        reports = []
        with patch.object(
            sys,
            "argv",
            ["pipeline_runner.py"],
        ), patch.object(
            pipeline_runner,
            "run_stage",
            side_effect=RuntimeError("sensitive failure text"),
        ), patch.object(
            pipeline_runner,
            "failure_report",
            side_effect=lambda stage, reason: reports.append((stage, reason)),
        ), patch.object(
            pipeline_runner,
            "flush_observability",
        ):
            self.assertEqual(pipeline_runner.main(), 1)

        exported = repr(FakeLaminar.spans) + repr(FakeLaminar.attributes)
        self.assertNotIn("sensitive failure text", exported)
        self.assertIn(("browser-extraction", "RuntimeError"), reports)
        self.assertIn(
            {"status": "failed", "error_type": "PipelineExecutionFailed"},
            [
                {key: value for key, value in item.items() if key != "duration_ms"}
                for item in FakeLaminar.attributes
            ],
        )



class AllActiveScopeTests(unittest.TestCase):
    def test_all_active_scope_is_schema_and_trace_default(self):
        schema=json.loads((ROOT / "schemas" / "extracted-batch.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["search_period"]["properties"]["period"]["const"],"All")
        source=(ROOT / "scripts" / "pipeline_runner.py").read_text(encoding="utf-8")
        self.assertIn('"search_scope": "all_active"',source)
        self.assertIn("search_scope",observability.TRACE_METADATA_ALLOWLIST)
        self.assertNotIn("search_period_days",observability.TRACE_METADATA_ALLOWLIST)
if __name__ == "__main__":
    unittest.main()
