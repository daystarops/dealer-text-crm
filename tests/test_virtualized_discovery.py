import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cdk_discovery_driver import browser_program
from discovery_index import DiscoveredLead, DiscoveryIndex, stable_key
from virtualized_discovery import EndDetector, ScrollObservation


def lead(number: int, name: str | None = None) -> DiscoveredLead:
    return DiscoveredLead(
        str(number),
        f"https://crm.connectcdk.com/evo2/test/newprospects/OpptyDetails.aspx?lDID={number}&lPID={number + 1000}",
        name or f"Fictional Customer {number}",
        str(number),
    )


class DiscoveryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "discovery.db"
        self.db = DiscoveryIndex(self.path)
        self.db.begin_run("run-1")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_virtualized_overlap_is_deduplicated(self):
        self.assertEqual(self.db.persist_rows([lead(i) for i in range(1, 51)], "run-1"), (50, 0, 0))
        self.assertEqual(self.db.persist_rows([lead(i) for i in range(41, 91)], "run-1"), (40, 10, 0))
        self.assertEqual(self.db.count(), 90)

    def test_row_replacement_adds_new_rows_without_order_keys(self):
        self.db.persist_rows([lead(1), lead(2)], "run-1")
        self.db.persist_rows([lead(3), lead(4)], "run-1")
        ids = {row[0] for row in self.db.connection.execute("SELECT opportunity_id FROM discovered_leads")}
        self.assertEqual(ids, {"1", "2", "3", "4"})

    def test_duplicate_opportunity_ids_update_one_record(self):
        self.db.persist_rows([lead(7, "Fictional Original")], "run-1")
        result = self.db.persist_rows([lead(7, "Fictional Updated")], "run-1")
        self.assertEqual(result, (0, 1, 0))
        row = self.db.connection.execute("SELECT recorded_display_name,times_seen FROM discovered_leads").fetchone()
        self.assertEqual(row, ("Fictional Updated", 2))

    def test_missing_stable_identifier_is_malformed(self):
        bad = DiscoveredLead("", "https://crm.connectcdk.com/detail", "Fictional Person")
        self.assertEqual(self.db.persist_rows([bad], "run-1"), (0, 0, 1))
        self.assertEqual(self.db.count(), 0)

    def test_one_malformed_row_does_not_stop_later_rows(self):
        bad = DiscoveredLead("not-an-id", "x", "Fictional Broken")
        self.assertEqual(self.db.persist_rows([lead(1), bad, lead(2)], "run-1"), (2, 0, 1))
        self.assertEqual(self.db.count(), 2)

    def test_restart_resumes_and_marks_stale_run_interrupted(self):
        self.db.persist_rows([lead(1), lead(2)], "run-1")
        self.db.close()
        self.db = DiscoveryIndex(self.path)
        self.assertEqual(self.db.begin_run("run-2"), 2)
        self.db.persist_rows([lead(2), lead(3)], "run-2")
        self.assertEqual(self.db.count(), 3)
        status = self.db.connection.execute("SELECT status FROM discovery_runs WHERE discovery_run_id='run-1'").fetchone()[0]
        self.assertEqual(status, "interrupted")

    def test_database_deduplication_survives_reopen(self):
        self.db.persist_rows([lead(12)], "run-1")
        self.db.close()
        self.db = DiscoveryIndex(self.path)
        self.db.begin_run("run-2")
        self.assertEqual(self.db.persist_rows([lead(12)], "run-2"), (0, 1, 0))
        self.assertEqual(self.db.count(), 1)

    def test_review_checkpoint_is_never_touched(self):
        checkpoint = Path(self.temp.name) / "review-progress.json"
        checkpoint.write_text(json.dumps({"reviewed_row_hashes": ["a", "b"]}), encoding="utf-8")
        before = checkpoint.read_bytes()
        self.db.persist_rows([lead(1)], "run-1")
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_stable_key_is_hashed_opportunity_identity(self):
        self.assertEqual(stable_key("42"), stable_key("42"))
        self.assertNotIn("42", stable_key("42"))


class EndDetectionTests(unittest.TestCase):
    def observation(self, ids=("a", "b"), top=900, new=0, loading=False):
        return ScrollObservation(tuple(ids), top, 100, 1000, loading, new)

    def test_one_timeout_does_not_end_and_later_change_recovers(self):
        detector = EndDetector()
        detector.observe(self.observation())
        self.assertFalse(detector.ready_for_final_probe(self.observation(), 900))
        detector.observe(self.observation(ids=("b", "c"), top=950, new=1))
        self.assertEqual(detector.consecutive_no_new, 0)

    def test_end_requires_three_cycles_and_final_probe(self):
        detector = EndDetector()
        at_end = self.observation()
        for _ in range(2):
            detector.observe(at_end)
            self.assertFalse(detector.ready_for_final_probe(at_end, 900))
        detector.observe(at_end)
        self.assertTrue(detector.ready_for_final_probe(at_end, 900))
        self.assertTrue(detector.confirms_end(at_end, self.observation()))

    def test_loading_or_changed_sequence_blocks_end(self):
        detector = EndDetector()
        for _ in range(3):
            detector.observe(self.observation())
        self.assertFalse(detector.confirms_end(self.observation(), self.observation(ids=("b", "c"))))
        self.assertFalse(detector.confirms_end(self.observation(), self.observation(loading=True)))


class BrowserProgramTests(unittest.TestCase):
    def test_program_is_frame_scoped_internal_and_read_only(self):
        program = browser_program("run", Path("private/index.db"), 150, 500, 60)
        compile(program, "<discovery>", "exec")
        self.assertIn("Page.createIsolatedWorld", program)
        self.assertIn("#MenuSections_SectionLabel_2", program)
        self.assertIn("closest('[role=tab]')", program)
        self.assertNotIn("document.querySelectorAll('#tr86')", program)
        self.assertIn("table.k-selectable", program)
        self.assertIn("scrollHeight", program)
        self.assertIn("clientHeight", program)
        self.assertIn("overflowY", program)
        self.assertIn("ordered_viewport_ids", program)
        self.assertNotIn("window.scroll", program)
        self.assertNotIn("open_lead_detail", program)
        self.assertNotIn("review-progress.json", program)
        self.assertNotIn("send_message", program)


if __name__ == "__main__":
    unittest.main()
