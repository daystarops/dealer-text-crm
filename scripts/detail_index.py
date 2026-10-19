"""Transactional Stage B claims and sanitized detail persistence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAILURE_CATEGORIES = frozenset({
    "route_rejected", "opportunity_mismatch", "required_content_missing",
    "history_timeout", "browser_timeout", "local_write_failed",
    "unexpected_tab", "stale_claim_recovered",
})


def utc_now(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def lead_key(opportunity_id: str) -> str:
    if not opportunity_id.isdigit():
        raise ValueError("invalid opportunity identifier")
    return hashlib.sha256(("cdk-opportunity:" + opportunity_id).encode()).hexdigest()


@dataclass(frozen=True)
class DetailClaim:
    stable_lead_key: str
    opportunity_id: str
    detail_url: str
    recorded_display_name: str
    claim_token: str
    attempt_count: int


class DetailIndex:
    def __init__(self, path: Path | str, *, timeout: float = 5):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, timeout=timeout)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(discovered_leads)")}
        if not columns:
            raise RuntimeError("Stage A discovered_leads table is missing")
        additions = {
            "last_attempted_at": "TEXT", "next_eligible_retry_at": "TEXT",
            "claim_token": "TEXT", "claimed_at": "TEXT",
        }
        with self.connection:
            for name, declaration in additions.items():
                if name not in columns:
                    self.connection.execute(f"ALTER TABLE discovered_leads ADD COLUMN {name} {declaration}")
            self.connection.execute("""
              CREATE TABLE IF NOT EXISTS detail_records (
                stable_lead_key TEXT PRIMARY KEY REFERENCES discovered_leads(stable_lead_key),
                opportunity_id_hash TEXT NOT NULL,
                sanitized_payload TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                detail_run_id TEXT NOT NULL
              )
            """)

    def discovery_count(self) -> int:
        return int(self.connection.execute("SELECT count(*) FROM discovered_leads").fetchone()[0])

    def state_count(self, state: str) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM discovered_leads WHERE detail_state=?", (state,)
        ).fetchone()[0])

    def recover_stale_claims(self, stale_seconds: int, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        cutoff = utc_now(current - timedelta(seconds=stale_seconds))
        with self.connection:
            return self.connection.execute("""
              UPDATE discovered_leads SET detail_state='failed_retryable',
                last_error_category='stale_claim_recovered',next_eligible_retry_at=?,
                claim_token=NULL,claimed_at=NULL
              WHERE detail_state='detail_in_progress' AND claimed_at IS NOT NULL AND claimed_at<=?
            """, (utc_now(current), cutoff)).rowcount

    def claim_next(self, run_id: str, now: datetime | None = None) -> DetailClaim | None:
        current = utc_now(now)
        token = f"{run_id}:{uuid.uuid4().hex}"
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute("""
              SELECT stable_lead_key,opportunity_id,detail_url,recorded_display_name,detail_attempt_count
              FROM discovered_leads
              WHERE detail_state='pending_detail' OR
                (detail_state='failed_retryable' AND
                 (next_eligible_retry_at IS NULL OR next_eligible_retry_at<=?))
              ORDER BY first_discovered_at,stable_lead_key LIMIT 1
            """, (current,)).fetchone()
            if row is None:
                self.connection.commit()
                return None
            changed = self.connection.execute("""
              UPDATE discovered_leads SET detail_state='detail_in_progress',
                detail_attempt_count=detail_attempt_count+1,last_attempted_at=?,
                claim_token=?,claimed_at=?,last_error_category=NULL,next_eligible_retry_at=NULL
              WHERE stable_lead_key=? AND (detail_state='pending_detail' OR
                (detail_state='failed_retryable' AND
                 (next_eligible_retry_at IS NULL OR next_eligible_retry_at<=?)))
            """, (current, token, current, row["stable_lead_key"], current)).rowcount
            if changed != 1:
                self.connection.rollback()
                return None
            self.connection.commit()
            return DetailClaim(row["stable_lead_key"], row["opportunity_id"], row["detail_url"],
                               row["recorded_display_name"], token, row["detail_attempt_count"] + 1)
        except Exception:
            self.connection.rollback()
            raise

    def complete(self, claim: DetailClaim, sanitized_payload: dict, run_id: str) -> None:
        if lead_key(claim.opportunity_id) != claim.stable_lead_key:
            raise ValueError("identity-to-opportunity pairing mismatch")
        serialized = json.dumps(sanitized_payload, ensure_ascii=False, separators=(",", ":"))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("""
              INSERT INTO detail_records(stable_lead_key,opportunity_id_hash,sanitized_payload,indexed_at,detail_run_id)
              VALUES(?,?,?,?,?)
              ON CONFLICT(stable_lead_key) DO UPDATE SET
                opportunity_id_hash=excluded.opportunity_id_hash,
                sanitized_payload=excluded.sanitized_payload,indexed_at=excluded.indexed_at,
                detail_run_id=excluded.detail_run_id
            """, (claim.stable_lead_key, lead_key(claim.opportunity_id), serialized, utc_now(), run_id))
            changed = self.connection.execute("""
              UPDATE discovered_leads SET detail_state='detail_complete',claim_token=NULL,
                claimed_at=NULL,next_eligible_retry_at=NULL,last_error_category=NULL
              WHERE stable_lead_key=? AND opportunity_id=? AND
                detail_state='detail_in_progress' AND claim_token=?
            """, (claim.stable_lead_key, claim.opportunity_id, claim.claim_token)).rowcount
            if changed != 1:
                raise RuntimeError("detail claim is no longer owned")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def fail(self, claim: DetailClaim, category: str, *, terminal: bool,
             retry_delay_seconds: int = 300, now: datetime | None = None) -> bool:
        safe = category if category in FAILURE_CATEGORIES else "required_content_missing"
        current = now or datetime.now(timezone.utc)
        next_retry = None if terminal else utc_now(current + timedelta(seconds=retry_delay_seconds))
        state = "failed_terminal" if terminal else "failed_retryable"
        with self.connection:
            changed = self.connection.execute("""
              UPDATE discovered_leads SET detail_state=?,last_error_category=?,
                next_eligible_retry_at=?,claim_token=NULL,claimed_at=NULL
              WHERE stable_lead_key=? AND detail_state='detail_in_progress' AND claim_token=?
            """, (state, safe, next_retry, claim.stable_lead_key, claim.claim_token)).rowcount
        return changed == 1

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
