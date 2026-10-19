"""Stage B exhaustive activity-section persistence layered on DetailIndex."""
from __future__ import annotations

import json

from detail_index import DetailClaim, DetailIndex, lead_key, utc_now

REQUIRED_SECTIONS = ("calls", "emails", "texts", "notes", "appointments", "tasks", "activities", "audit_trail")
COMPLETE_SECTION_STATES = frozenset({"complete", "empty", "unavailable_terminal"})


class ExhaustiveDetailIndex(DetailIndex):
    def _migrate(self) -> None:
        super()._migrate()
        with self.connection:
            self.connection.execute("""
              CREATE TABLE IF NOT EXISTS detail_sections (
                stable_lead_key TEXT NOT NULL REFERENCES discovered_leads(stable_lead_key),
                section_name TEXT NOT NULL,status TEXT NOT NULL,
                activity_count INTEGER NOT NULL DEFAULT 0,scroll_cycles INTEGER NOT NULL DEFAULT 0,
                true_end INTEGER NOT NULL DEFAULT 0,container_selector TEXT NOT NULL DEFAULT '',
                pagination_detected INTEGER NOT NULL DEFAULT 0,last_error_category TEXT,
                resume_scroll_top REAL NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
                PRIMARY KEY(stable_lead_key,section_name)
              )
            """)
            self.connection.execute("""
              CREATE TABLE IF NOT EXISTS detail_activities (
                stable_lead_key TEXT NOT NULL REFERENCES discovered_leads(stable_lead_key),
                fingerprint TEXT NOT NULL,activity_type TEXT NOT NULL,
                activity_timestamp TEXT NOT NULL DEFAULT '',direction TEXT NOT NULL DEFAULT '',
                staff_attribution TEXT NOT NULL DEFAULT '',status_or_outcome TEXT NOT NULL DEFAULT '',
                sanitized_body TEXT NOT NULL DEFAULT '',source_section TEXT NOT NULL,
                PRIMARY KEY(stable_lead_key,fingerprint)
              )
            """)
            self.connection.execute("CREATE TABLE IF NOT EXISTS detail_index_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            migrated = self.connection.execute(
                "SELECT value FROM detail_index_meta WHERE key='exhaustive_activity_v1'"
            ).fetchone()
            if migrated is None:
                self.connection.execute(
                    "UPDATE discovered_leads SET detail_state='pending_detail' WHERE detail_state='detail_complete'"
                )
                self.connection.execute(
                    "INSERT INTO detail_index_meta(key,value) VALUES('exhaustive_activity_v1',?)", (utc_now(),)
                )

    def save_sections(self, claim: DetailClaim, sections: list[dict], activities: list[dict]) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            owned = self.connection.execute("""
              SELECT 1 FROM discovered_leads WHERE stable_lead_key=?
                AND detail_state='detail_in_progress' AND claim_token=?
            """, (claim.stable_lead_key, claim.claim_token)).fetchone()
            if not owned:
                raise RuntimeError("detail claim is no longer owned")
            for section in sections:
                self.connection.execute("""
                  INSERT INTO detail_sections(stable_lead_key,section_name,status,activity_count,
                    scroll_cycles,true_end,container_selector,pagination_detected,last_error_category,
                    resume_scroll_top,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(stable_lead_key,section_name) DO UPDATE SET status=excluded.status,
                    activity_count=excluded.activity_count,scroll_cycles=excluded.scroll_cycles,
                    true_end=excluded.true_end,container_selector=excluded.container_selector,
                    pagination_detected=excluded.pagination_detected,
                    last_error_category=excluded.last_error_category,
                    resume_scroll_top=excluded.resume_scroll_top,updated_at=excluded.updated_at
                """, (claim.stable_lead_key, section["section_name"], section["status"],
                      int(section.get("activity_count", 0)), int(section.get("scroll_cycles", 0)),
                      int(bool(section.get("true_end"))), section.get("container_selector", ""),
                      int(bool(section.get("pagination_detected"))), section.get("last_error_category"),
                      float(section.get("resume_scroll_top", 0)), utc_now()))
            for activity in activities:
                self.connection.execute("""
                  INSERT INTO detail_activities(stable_lead_key,fingerprint,activity_type,
                    activity_timestamp,direction,staff_attribution,status_or_outcome,sanitized_body,
                    source_section) VALUES(?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(stable_lead_key,fingerprint) DO NOTHING
                """, (claim.stable_lead_key, activity["fingerprint"], activity["activity_type"],
                      activity.get("activity_timestamp", ""), activity.get("direction", ""),
                      activity.get("staff_attribution", ""), activity.get("status_or_outcome", ""),
                      activity.get("sanitized_body", ""), activity["source_section"]))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def section_statuses(self, claim: DetailClaim) -> dict[str, str]:
        return {row[0]: row[1] for row in self.connection.execute(
            "SELECT section_name,status FROM detail_sections WHERE stable_lead_key=?",
            (claim.stable_lead_key,),
        )}

    def complete(self, claim: DetailClaim, sanitized_payload: dict, run_id: str) -> None:
        if lead_key(claim.opportunity_id) != claim.stable_lead_key:
            raise ValueError("identity-to-opportunity pairing mismatch")
        statuses = self.section_statuses(claim)
        if set(statuses) != set(REQUIRED_SECTIONS):
            raise RuntimeError("required activity sections are missing")
        if any(statuses[name] not in COMPLETE_SECTION_STATES for name in REQUIRED_SECTIONS):
            raise RuntimeError("required activity sections are incomplete")
        super().complete(claim, sanitized_payload, run_id)

    def activity_count(self, stable_lead_key: str) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM detail_activities WHERE stable_lead_key=?", (stable_lead_key,)
        ).fetchone()[0])

    def section_report(self, stable_lead_key: str) -> list[dict]:
        return [dict(row) for row in self.connection.execute("""
          SELECT section_name,status,activity_count,scroll_cycles,true_end,
            container_selector,pagination_detected,last_error_category
          FROM detail_sections WHERE stable_lead_key=? ORDER BY section_name
        """, (stable_lead_key,))]
