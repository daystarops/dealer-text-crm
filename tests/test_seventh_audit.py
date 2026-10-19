import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analysis_pipeline
from cdk_review_driver import BROWSER_PROGRAM
from privacy_gate import PrivacyGateError, assert_no_residual_pii, sanitize_text
from report_renderer import render_operational_html


class Vault:
    def __init__(self, identity):
        self.identity = identity

    def get(self, lead_key):
        return self.identity if lead_key == "a" * 64 else None

    def close(self):
        pass


def analysis_payload(position="Unknown"):
    return {
        "run_id": "cdk-20260805T120000-abcdef12",
        "ranked_leads": [
            {
                "lead_key": "a" * 64,
                "first_name": "Alex",
                "priority_score": 50,
                "supporting_evidence": ["Inquiry"],
                "latest_explicit_customer_position": position,
                "main_obstacle": "Unknown",
                "recommended_next_action": "Call",
                "call_opening": "Hi Alex",
                "discovery_questions": ["One?", "Two?", "Three?"],
                "objections": [
                    {"objection": "A", "response": "B"},
                    {"objection": "C", "response": "D"},
                ],
                "voicemail": "Voicemail",
                "follow_up_text": "Hello Alex",
                "appointment_close": "Today or tomorrow?",
                "human_handoff_flags": [],
                "messaging_risk_flags": [],
            }
        ],
    }


class SeventhAuditTests(unittest.TestCase):
    def test_browser_program_uses_user_gesture_polling_and_mutation_probe(self):
        self.assertIn("userGesture=bool(user_gesture)", BROWSER_PROGRAM)
        self.assertIn("user_gesture=True", BROWSER_PROGRAM)
        self.assertIn(",'_blank');return true;}", BROWSER_PROGRAM)
        self.assertIn("__cdkLeadCopilotRefreshState", BROWSER_PROGRAM)
        self.assertIn("for _ in range(40):\n        pause(250);current_tabs=list_tabs()", BROWSER_PROGRAM)
        self.assertIn("Approved detail target did not finish opening", BROWSER_PROGRAM)
        self.assertIn("Detail history frame did not finish loading", BROWSER_PROGRAM)
        self.assertIn("detail_frame=verified_detail_frame();history=verified_history_frame();break", BROWSER_PROGRAM)

    def test_contextual_surname_redaction_covers_normal_sales_notes(self):
        identities = {"Alex Ford": "Alex"}
        self.assertEqual(sanitize_text("2026 Ford Explorer", identities), "2026 Ford Explorer")
        for note in ("Spoke with Ford today", "Called Ford", "Lead Ford requested pricing"):
            sanitized = sanitize_text(note, identities)
            self.assertNotIn("Ford", sanitized)
            assert_no_residual_pii(sanitized, identities)

    def test_operational_report_is_acl_secured_before_and_after_publish(self):
        identity = SimpleNamespace(
            full_name="Alex Ford",
            phone_e164="+12125550123",
            cdk_opportunity_id="123",
            vehicle="2026 Ford Explorer",
        )
        calls = []
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "report.html"
            render_operational_html(analysis_payload(), Vault(identity), output, lambda path: calls.append(path.name))
            self.assertEqual(calls, ["report.html.tmp", "report.html"])
            self.assertTrue(output.exists())

    def test_operational_report_is_deleted_if_post_publish_acl_fails(self):
        identity = SimpleNamespace(
            full_name="Alex Ford",
            phone_e164="+12125550123",
            cdk_opportunity_id="123",
            vehicle="2026 Ford Explorer",
        )
        calls = []
        def acl(path):
            calls.append(path.name)
            if path.name == "report.html":
                raise RuntimeError("ACL verification failed")
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "report.html"
            with self.assertRaises(RuntimeError):
                render_operational_html(analysis_payload(), Vault(identity), output, acl)
            self.assertEqual(calls, ["report.html.tmp", "report.html"])
            self.assertFalse(output.exists())

    def test_direct_render_rechecks_model_output_against_vault_identities(self):
        identity = SimpleNamespace(
            full_name="Alex Ford",
            first_name="Alex",
            phone_e164="+12125550123",
            cdk_opportunity_id="123",
            vehicle="2026 Ford Explorer",
        )
        with tempfile.TemporaryDirectory() as td:
            result = Path(td) / "analysis.json"
            result.write_text(json.dumps(analysis_payload("Spoke with Ford today")), encoding="utf-8")
            with mock.patch.object(analysis_pipeline, "default_vault", return_value=Vault(identity)):
                with self.assertRaises(PrivacyGateError):
                    analysis_pipeline.render(result, Path(td) / "outputs")


if __name__ == "__main__":
    unittest.main()
