"""Privacy-safe activity classification, fingerprinting, and section results."""
from __future__ import annotations

import hashlib
import re

from privacy_gate import assert_no_residual_pii, sanitize_text

CATEGORIES = ("calls", "emails", "texts", "notes", "appointments", "tasks", "other")


def classify_activity(text: str) -> str:
    low = text.casefold()
    if any(word in low for word in ("phone", "call", "voicemail")): return "calls"
    if "email" in low: return "emails"
    if any(word in low for word in ("text message", "sms")): return "texts"
    if any(word in low for word in ("appointment", "showroom")): return "appointments"
    if any(word in low for word in ("task", "follow up", "follow-up", "due")): return "tasks"
    if any(word in low for word in ("note", "comment")): return "notes"
    return "other"


def activity_record(raw_text: str, source_section: str, identities: dict[str, str]) -> dict:
    normalized = " ".join(raw_text.split())
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
    body = sanitize_text(normalized, identities)
    assert_no_residual_pii(body, identities)
    timestamp = next(iter(re.findall(r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}(?:\s*[AP]M)?)?", normalized, re.I)), "")
    low = normalized.casefold()
    direction = "inbound" if "inbound" in low else "outbound" if "outbound" in low else ""
    return {"fingerprint": fingerprint, "activity_type": classify_activity(normalized),
            "activity_timestamp": timestamp, "direction": direction,
            "staff_attribution": "", "status_or_outcome": "",
            "sanitized_body": body, "source_section": source_section}


def deduplicate_activities(rows: list[str], source_section: str, identities: dict[str, str]) -> list[dict]:
    unique = {}
    for row in rows:
        if not row.strip(): continue
        record = activity_record(row, source_section, identities)
        unique.setdefault(record["fingerprint"], record)
    return list(unique.values())


def category_sections(activities: list[dict], scan: dict) -> list[dict]:
    sections = []
    for category in ("calls", "emails", "texts", "notes", "appointments", "tasks"):
        count = sum(item["activity_type"] == category for item in activities)
        sections.append({"section_name": category, "status": "complete" if count else "empty",
                         "activity_count": count, **scan})
    sections.append({"section_name": "activities", "status": "complete" if activities else "empty",
                     "activity_count": len(activities), **scan})
    return sections
