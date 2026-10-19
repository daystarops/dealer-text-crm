import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cdk_detail_index_driver import browser_program
from cdk_runtime import parse_detail_destination
from detail_index import DetailIndex, lead_key
from detail_worker import DetailFailure, DetailWorker, ExtractedDetail
from discovery_index import DiscoveredLead, DiscoveryIndex
from identity_vault import IdentityRecord


def detail_url(number):
    return f"https://crm.connectcdk.com/evo2/fictional/newprospects/OpptyDetails.aspx?lDID={number}&lPID={int(number)+1000}"


class FakeVault:
    def __init__(self):
        self.records = {}

    def extraction_upsert(self, record):
        self.records[record.cdk_opportunity_id] = record
        return record.lead_key


class FakeAdapter:
    def __init__(self, failures=None, mismatch=None):
        self.failures = dict(failures or {})
        self.mismatch = mismatch
        self.cleanup_count = 0
        self.open_count = 0
        self.max_open = 0

    def extract(self, claim):
        self.open_count += 1
        self.max_open = max(self.max_open, self.open_count)
        failure = self.failures.pop(claim.opportunity_id, None)
        if failure:
            raise failure
        opportunity = self.mismatch or claim.opportunity_id
        self.mismatch = None
        identity = IdentityRecord(opportunity, "Fictional", "Person", "Fictional Person", "")
        return ExtractedDetail(opportunity, identity, {"lead_key": claim.stable_lead_key, "status": "active"})

    def cleanup(self):
        self.open_count = max(0, self.open_count - 1)
        self.cleanup_count += 1


class DetailIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "index.db"
        with DiscoveryIndex(self.db_path) as discovery:
            discovery.begin_run("discovery")
            discovery.persist_rows([
                DiscoveredLead(str(i), detail_url(i), f"Fictional Person {i}", str(i))
                for i in range(1, 5)
            ], "discovery")
            discovery.finish_run("discovery", end_reached=False, scale_limit=True)
        self.index = DetailIndex(self.db_path)

    def tearDown(self):
        self.index.close()
        self.temp.cleanup()

    def test_deterministic_pending_selection_and_restart_skip_complete(self):
        expected = self.index.connection.execute("""
          SELECT opportunity_id FROM discovered_leads ORDER BY first_discovered_at,stable_lead_key
        """).fetchone()[0]
        first = self.index.claim_next("run")
        self.assertEqual(first.opportunity_id, expected)
        self.index.complete(first, {"lead_key": first.stable_lead_key}, "run")
        second = self.index.claim_next("restart")
        self.assertNotEqual(second.opportunity_id, first.opportunity_id)

    def test_atomic_and_concurrent_claim_protection(self):
        results = []
        barrier = threading.Barrier(2)
        def claim():
            other = DetailIndex(self.db_path)
            try:
                barrier.wait()
                results.append(other.claim_next("concurrent"))
            finally:
                other.close()
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len({item.stable_lead_key for item in results}), 2)
        self.assertEqual(self.index.state_count("detail_in_progress"), 2)

    def test_stale_in_progress_recovery(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        claim = self.index.claim_next("old", old)
        self.assertEqual(self.index.recover_stale_claims(60), 1)
        row = self.index.connection.execute("""
          SELECT detail_state,last_error_category FROM discovered_leads WHERE stable_lead_key=?
        """, (claim.stable_lead_key,)).fetchone()
        self.assertEqual(tuple(row), ("failed_retryable", "stale_claim_recovered"))

    def test_successful_completion_and_identity_pairing(self):
        claim = self.index.claim_next("run")
        self.index.complete(claim, {"lead_key": claim.stable_lead_key}, "run")
        row = self.index.connection.execute("""
          SELECT d.detail_state,r.opportunity_id_hash FROM discovered_leads d
          JOIN detail_records r USING(stable_lead_key) WHERE d.stable_lead_key=?
        """, (claim.stable_lead_key,)).fetchone()
        self.assertEqual(tuple(row), ("detail_complete", lead_key(claim.opportunity_id)))

    def test_retryable_and_terminal_failures(self):
        retry = self.index.claim_next("run")
        self.assertTrue(self.index.fail(retry, "history_timeout", terminal=False, retry_delay_seconds=60))
        terminal = self.index.claim_next("run")
        self.assertTrue(self.index.fail(terminal, "route_rejected", terminal=True))
        self.assertEqual(self.index.state_count("failed_retryable"), 1)
        self.assertEqual(self.index.state_count("failed_terminal"), 1)

    def test_partial_local_write_rolls_back(self):
        claim = self.index.claim_next("run")
        self.index.connection.execute("""
          CREATE TRIGGER reject_detail BEFORE INSERT ON detail_records BEGIN SELECT RAISE(ABORT,'synthetic'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.complete(claim, {"lead_key": claim.stable_lead_key}, "run")
        self.assertEqual(self.index.connection.execute("SELECT count(*) FROM detail_records").fetchone()[0], 0)
        state = self.index.connection.execute(
            "SELECT detail_state FROM discovered_leads WHERE stable_lead_key=?", (claim.stable_lead_key,)
        ).fetchone()[0]
        self.assertEqual(state, "detail_in_progress")

    def test_pairing_mismatch_is_rejected(self):
        claim = self.index.claim_next("run")
        wrong = type(claim)(lead_key("999"), claim.opportunity_id, claim.detail_url,
                            claim.recorded_display_name, claim.claim_token, claim.attempt_count)
        with self.assertRaises(ValueError):
            self.index.complete(wrong, {}, "run")

    def test_discovery_total_and_checkpoint_are_preserved(self):
        checkpoint = Path(self.temp.name) / "review-progress.json"
        checkpoint.write_text(json.dumps({"reviewed_row_hashes": list(range(15))}), encoding="utf-8")
        before = checkpoint.read_bytes(); total = self.index.discovery_count()
        claim = self.index.claim_next("run")
        self.index.complete(claim, {"lead_key": claim.stable_lead_key}, "run")
        self.assertEqual(self.index.discovery_count(), total)
        self.assertEqual(checkpoint.read_bytes(), before)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "index.db"
        with DiscoveryIndex(self.path) as discovery:
            discovery.begin_run("d")
            discovery.persist_rows([DiscoveredLead(str(i), detail_url(i), f"Fictional Person {i}") for i in range(1, 4)], "d")
        self.index = DetailIndex(self.path)

    def tearDown(self):
        self.index.close();self.temp.cleanup()

    def test_one_failure_does_not_block_later_record_and_tabs_cleanup(self):
        first_id = self.index.connection.execute("SELECT opportunity_id FROM discovered_leads ORDER BY first_discovered_at,stable_lead_key").fetchone()[0]
        adapter = FakeAdapter({first_id: DetailFailure("history_timeout")})
        stats = DetailWorker(self.index, FakeVault(), adapter, "run", 3600).run(2)
        self.assertEqual((stats.attempted, stats.completed, stats.retryable_failures), (3, 2, 1))
        self.assertEqual(adapter.cleanup_count, 3)
        self.assertEqual(adapter.max_open, 1)

    def test_opportunity_mismatch_is_terminal(self):
        adapter = FakeAdapter(mismatch="999")
        stats = DetailWorker(self.index, FakeVault(), adapter, "run").run(1)
        self.assertEqual(stats.mismatches_rejected, 1)
        self.assertEqual(stats.terminal_failures, 1)
        self.assertEqual(stats.completed, 1)


class RouteAndBrowserTests(unittest.TestCase):
    def test_detail_url_and_opportunity_verification(self):
        parsed = parse_detail_destination(detail_url("12"), "https://crm.connectcdk.com/")
        self.assertEqual(parsed.opportunity_id, "12")
        self.assertIsNone(parse_detail_destination("https://example.com/OpptyDetails.aspx?lDID=12&lPID=13", "https://crm.connectcdk.com/"))

    def test_browser_program_is_one_tab_read_only_and_reacquires_frames(self):
        program = browser_program("run", Path("private/index.db"), 5, 900, 60)
        compile(program, "<detail>", "exec")
        self.assertIn("claim_next", program)
        self.assertIn("new_tab(claim.detail_url)", program)
        self.assertIn("len(opened)>1", program)
        self.assertIn("close_created", program)
        self.assertIn("all_frames()", program)
        self.assertIn("Page.createIsolatedWorld", program)
        for forbidden in ("Save", "Send", "Submit", "CreateTask", "score", "outreach"):
            self.assertNotIn(forbidden, program)


if __name__ == "__main__":
    unittest.main()
