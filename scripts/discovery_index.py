"""Durable, restart-safe index for read-only CDK grid discovery."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DISCOVERY_STATES = frozenset({
    "discovered", "pending_detail", "detail_in_progress", "detail_complete",
    "failed_retryable", "failed_terminal", "skipped",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_key(opportunity_id: str) -> str:
    """Return a non-PII stable key while retaining the operational ID separately."""
    if not opportunity_id or not opportunity_id.isdigit():
        raise ValueError("opportunity ID is missing or malformed")
    return hashlib.sha256(f"cdk-opportunity:{opportunity_id}".encode()).hexdigest()


@dataclass(frozen=True)
class DiscoveredLead:
    opportunity_id: str
    detail_url: str
    recorded_display_name: str
    source_position_hint: str = ""

    @property
    def stable_lead_key(self) -> str:
        return stable_key(self.opportunity_id)


class DiscoveryIndex:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        states = ",".join(repr(state) for state in sorted(DISCOVERY_STATES))
        self.connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS discovery_runs (
          discovery_run_id TEXT PRIMARY KEY,
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('running','interrupted','complete','scale_limit')),
          scroll_cycles INTEGER NOT NULL DEFAULT 0,
          duplicates_ignored INTEGER NOT NULL DEFAULT 0,
          malformed_rows INTEGER NOT NULL DEFAULT 0,
          end_reached INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discovered_leads (
          stable_lead_key TEXT PRIMARY KEY,
          opportunity_id TEXT NOT NULL UNIQUE,
          detail_url TEXT NOT NULL,
          recorded_display_name TEXT NOT NULL,
          discovery_state TEXT NOT NULL CHECK(discovery_state IN ({states})),
          first_discovered_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          discovery_run_id TEXT NOT NULL,
          source_position_hint TEXT NOT NULL DEFAULT '',
          times_seen INTEGER NOT NULL DEFAULT 1,
          detail_state TEXT NOT NULL CHECK(detail_state IN ({states})),
          detail_attempt_count INTEGER NOT NULL DEFAULT 0,
          last_error_category TEXT
        );
        CREATE INDEX IF NOT EXISTS discovered_leads_run_idx
          ON discovered_leads(discovery_run_id);
        PRAGMA user_version=1;
        """)
        self.connection.commit()

    def begin_run(self, run_id: str) -> int:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE discovery_runs SET status='interrupted',updated_at=? WHERE status='running'",
                (now,),
            )
            self.connection.execute(
                "INSERT INTO discovery_runs(discovery_run_id,started_at,updated_at,status) VALUES(?,?,?,'running')",
                (run_id, now, now),
            )
        return self.count()

    def persist_rows(self, rows: list[DiscoveredLead], run_id: str) -> tuple[int, int, int]:
        new = duplicates = malformed = 0
        now = utc_now()
        with self.connection:
            for row in rows:
                try:
                    key = row.stable_lead_key
                    if not row.detail_url or not row.recorded_display_name:
                        raise ValueError("required discovery field missing")
                    existing = self.connection.execute(
                        "SELECT stable_lead_key FROM discovered_leads WHERE opportunity_id=?", (row.opportunity_id,)
                    ).fetchone()
                    if existing:
                        duplicates += 1
                        self.connection.execute("""
                          UPDATE discovered_leads SET detail_url=?,recorded_display_name=?,last_seen_at=?,
                            discovery_run_id=?,source_position_hint=?,times_seen=times_seen+1
                          WHERE stable_lead_key=?
                        """, (row.detail_url, row.recorded_display_name, now, run_id, row.source_position_hint, key))
                    else:
                        self.connection.execute("""
                          INSERT INTO discovered_leads(
                            stable_lead_key,opportunity_id,detail_url,recorded_display_name,
                            discovery_state,first_discovered_at,last_seen_at,discovery_run_id,
                            source_position_hint,times_seen,detail_state,detail_attempt_count
                          ) VALUES(?,?,?,?,'discovered',?,?,?,?,1,'pending_detail',0)
                        """, (key, row.opportunity_id, row.detail_url, row.recorded_display_name,
                              now, now, run_id, row.source_position_hint))
                        new += 1
                except (ValueError, sqlite3.IntegrityError):
                    malformed += 1
        return new, duplicates, malformed

    def record_cycle(self, run_id: str, duplicates: int, malformed: int, *, scrolled: bool = True) -> None:
        with self.connection:
            self.connection.execute("""
              UPDATE discovery_runs SET updated_at=?,scroll_cycles=scroll_cycles+?,
                duplicates_ignored=duplicates_ignored+?,malformed_rows=malformed_rows+?
              WHERE discovery_run_id=? AND status='running'
            """, (utc_now(), int(scrolled), duplicates, malformed, run_id))

    def finish_run(self, run_id: str, *, end_reached: bool, scale_limit: bool = False) -> None:
        status = "complete" if end_reached else "scale_limit" if scale_limit else "interrupted"
        with self.connection:
            self.connection.execute("""
              UPDATE discovery_runs SET updated_at=?,status=?,end_reached=? WHERE discovery_run_id=?
            """, (utc_now(), status, int(end_reached), run_id))

    def count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM discovered_leads").fetchone()[0])

    def run_summary(self, run_id: str) -> dict:
        row = self.connection.execute("""
          SELECT status,scroll_cycles,duplicates_ignored,malformed_rows,end_reached
          FROM discovery_runs WHERE discovery_run_id=?
        """, (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return dict(zip(("status", "scroll_cycles", "duplicates_ignored", "malformed_rows", "end_reached"), row))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
