"""Conservative, local-only campaign readiness classification."""
from __future__ import annotations

import csv
import html
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from exhaustive_detail_index import COMPLETE_SECTION_STATES, REQUIRED_SECTIONS
from identity_vault import default_vault

COHORTS = ("AUTOMATED_SMS_READY", "MANUAL_ONE_TO_ONE_REVIEW",
           "CALL_ONLY_OR_PERSONAL_FOLLOW_UP", "SUPPRESS", "INSUFFICIENT_EVIDENCE")
PHONE_DIGITS = re.compile(r"\D+")
OPT_OUT = re.compile(r"\b(?:replied\s+stop|text(?:ed)?\s+stop|stop\s+(?:texting|calling|contacting)|unsubscribe|do\s+not\s+contact|do\s+not\s+call|do\s+not\s+text|internal\s+dnc)\b", re.I)
WRONG_NUMBER = re.compile(r"\b(?:wrong\s+number|disconnected|invalid\s+number|not\s+in\s+service|landline)\b", re.I)
CLOSED = re.compile(r"\b(?:purchased|bought|leased|sold\s+customer|closed\s+elsewhere|already\s+bought)\b", re.I)
CONSENT = re.compile(r"\b(?:sms\s+consent|text\s+consent|opt(?:ed)?[ -]?in\s+to\s+(?:sms|text)|agreed\s+to\s+receive\s+(?:sms|texts))\b", re.I)
CUSTOMER_REPLY = re.compile(r"\b(?:inbound|customer\s+(?:replied|responded|called|texted|emailed))\b", re.I)
CONTACT_REQUEST = re.compile(r"\b(?:requested\s+(?:a\s+)?(?:call|text|contact)|please\s+(?:call|text|contact))\b", re.I)
COMPLAINT = re.compile(r"\b(?:complain(?:ed|t)?|harass|too\s+many\s+(?:calls|texts)|stop\s+contacting)\b", re.I)


def normalize_phone(value: str) -> str:
    digits = PHONE_DIGITS.sub("", value or "")
    if len(digits) == 11 and digits.startswith("1"): digits = digits[1:]
    return digits if len(digits) == 10 else ""


def parse_timestamp(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%m/%d/%y"):
        try: return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError: pass
    return None


@dataclass
class Candidate:
    stable_lead_key: str; opportunity_id: str; full_name: str; phone: str
    vehicle: str; lead_source: str; indexed_at: str; cohort: str = ""
    reason: str = ""; message: str = ""; evidence: dict | None = None


def _proof_valid(sections: list[sqlite3.Row]) -> bool:
    statuses = {row["section_name"]: row for row in sections}
    if set(statuses) != set(REQUIRED_SECTIONS): return False
    return all(statuses[name]["status"] in COMPLETE_SECTION_STATES and
               (statuses[name]["status"] == "unavailable_terminal" or bool(statuses[name]["true_end"]))
               for name in REQUIRED_SECTIONS)


def classify(candidate: Candidate, activities: list[sqlite3.Row], *, dnc: bool = False,
             duplicate: bool = False, proof_valid: bool = True) -> Candidate:
    bodies = "\n".join(row["sanitized_body"] or "" for row in activities)
    phone_valid = bool(candidate.phone)
    opt_out = dnc or bool(OPT_OUT.search(bodies))
    wrong = bool(WRONG_NUMBER.search(bodies)); closed = bool(CLOSED.search(bodies))
    complaint = bool(COMPLAINT.search(bodies)); consent = bool(CONSENT.search(bodies))
    replies = [row for row in activities if row["direction"] == "inbound" or CUSTOMER_REPLY.search(row["sanitized_body"] or "")]
    outbound = [row for row in activities if row["direction"] == "outbound"]
    customer_times = [parse_timestamp(row["activity_timestamp"]) for row in replies]
    dealer_times = [parse_timestamp(row["activity_timestamp"]) for row in outbound]
    last_customer = max((value for value in customer_times if value), default=None)
    last_dealer = max((value for value in dealer_times if value), default=None)
    two_way_text = any(r["activity_type"] == "texts" for r in replies) and any(r["activity_type"] == "texts" for r in outbound)
    latest = max((parse_timestamp(r["activity_timestamp"]) for r in activities), default=None,
                 key=lambda x: x or datetime.min.replace(tzinfo=timezone.utc))
    last_type = next((r["activity_type"] for r in sorted(activities,
                     key=lambda r: parse_timestamp(r["activity_timestamp"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)), "unknown")
    evidence = {**(candidate.evidence or {}), "phone_valid": phone_valid, "mobile_status": "unknown", "sms_consent": "documented" if consent else "unknown",
                "opt_out": opt_out, "wrong_number": wrong, "closed_outcome": closed,
                "complaint": complaint, "customer_replies": len(replies), "outbound_calls": sum(r["activity_type"] == "calls" and r["direction"] == "outbound" for r in activities),
                "outbound_texts": sum(r["activity_type"] == "texts" and r["direction"] == "outbound" for r in activities),
                "outbound_emails": sum(r["activity_type"] == "emails" and r["direction"] == "outbound" for r in activities),
                "two_way_text": two_way_text, "contact_requested": bool(CONTACT_REQUEST.search(bodies)),
                "last_activity_type": last_type, "last_activity_at": latest.isoformat() if latest else "unknown",
                "lead_age": "unknown", "appointment_status": "unknown", "opportunity_status": "unknown",
                "consecutive_unanswered_attempts": max(0, len(outbound) - len(replies)),
                "opt_out_or_dnc": opt_out, "wrong_or_invalid_number": wrong,
                "unresolved_recent_conversation": bool(replies),
                "recent_outreach_excessive": max(0, len(outbound) - len(replies)) >= 8,
                "last_customer_communication": last_customer.isoformat() if last_customer else "unknown",
                "last_dealership_communication": last_dealer.isoformat() if last_dealer else "unknown",
                "active_salesperson_status": "unknown", "language": "unknown",
                "identity_mapping_valid": True}
    candidate.evidence = evidence
    if duplicate: candidate.cohort, candidate.reason = "SUPPRESS", "Duplicate recipient; another opportunity was selected"
    elif not proof_valid: candidate.cohort, candidate.reason = "SUPPRESS", "Invalid exhaustive section proof"
    elif not phone_valid: candidate.cohort, candidate.reason = "INSUFFICIENT_EVIDENCE", "No valid normalized phone number"
    elif opt_out or wrong or complaint or closed:
        candidate.cohort = "SUPPRESS"
        candidate.reason = "Contact suppression evidence" if opt_out or wrong or complaint else "Completed purchase/closed outcome evidence"
    elif evidence["consecutive_unanswered_attempts"] >= 8:
        candidate.cohort, candidate.reason = "SUPPRESS", "Excessive consecutive unanswered outreach"
    elif replies or two_way_text:
        candidate.cohort, candidate.reason = "CALL_ONLY_OR_PERSONAL_FOLLOW_UP", "Prior engagement requires contextual personal follow-up"
    elif consent and evidence["mobile_status"] == "mobile":
        candidate.cohort, candidate.reason = "AUTOMATED_SMS_READY", "Documented consent and mobile evidence"
    else:
        candidate.cohort, candidate.reason = "MANUAL_ONE_TO_ONE_REVIEW", "Potentially contactable, but automated-SMS consent/mobile evidence is unknown"
    if candidate.cohort in ("AUTOMATED_SMS_READY", "MANUAL_ONE_TO_ONE_REVIEW"):
        vehicle = candidate.vehicle or "your vehicle inquiry"
        candidate.message = f"Hi, this is Jordan from Premier Ford of Bay Ridge following up about {vehicle}. Would you like to review next steps? Reply STOP to opt out."
    return candidate


def audit(root: Path) -> tuple[list[Candidate], dict]:
    db = sqlite3.connect(root / "private" / "all-active-discovery.db"); db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT d.stable_lead_key,d.opportunity_id,d.detail_attempt_count,
      r.sanitized_payload,r.indexed_at FROM discovered_leads d JOIN detail_records r USING(stable_lead_key)
      WHERE d.detail_state='detail_complete' ORDER BY r.indexed_at DESC,d.stable_lead_key""").fetchall()
    vault = default_vault(root); candidates=[]; invalid=0; identity_missing=0
    for row in rows:
        identity = vault.get(row["stable_lead_key"])
        sections = db.execute("SELECT * FROM detail_sections WHERE stable_lead_key=?", (row["stable_lead_key"],)).fetchall()
        valid = _proof_valid(sections); invalid += not valid
        if not identity or identity.cdk_opportunity_id != row["opportunity_id"]: identity_missing += 1; continue
        payload=json.loads(row["sanitized_payload"]); candidates.append(Candidate(row["stable_lead_key"],row["opportunity_id"],identity.full_name,
          normalize_phone(identity.phone_e164),payload.get("vehicle",identity.vehicle),payload.get("lead_source",""),row["indexed_at"],
          evidence={"proof_valid":valid,"attempt_count":row["detail_attempt_count"],"dnc":identity.do_not_contact}))
    grouped={}
    for c in candidates: grouped.setdefault(c.phone or f"missing:{c.stable_lead_key}",[]).append(c)
    selected=[]; duplicate_count=0
    for group in grouped.values():
        group.sort(key=lambda c:c.indexed_at,reverse=True)
        for index,c in enumerate(group):
            acts=db.execute("SELECT * FROM detail_activities WHERE stable_lead_key=?",(c.stable_lead_key,)).fetchall()
            classify(c,acts,dnc=bool(c.evidence["dnc"]),duplicate=index>0,proof_valid=bool(c.evidence["proof_valid"]));selected.append(c)
            duplicate_count += index>0
    counts=Counter(c.cohort for c in selected); attempts=sum(int(c.evidence.get("attempt_count",0)) for c in candidates)
    summary={"database_rows":len(rows),"unique_stable_keys":len({r['stable_lead_key'] for r in rows}),
      "unique_opportunity_ids":len({r['opportunity_id'] for r in rows}),"duplicate_opportunity_ids":len(rows)-len({r['opportunity_id'] for r in rows}),
      "unique_phone_numbers":len({c.phone for c in candidates if c.phone}),"unique_recipients":len(grouped),"duplicate_recipients":duplicate_count,
      "invalid_section_proofs":invalid,"identity_mapping_incomplete":identity_missing,"repeatedly_indexed":sum(int(c.evidence.get('attempt_count',0))>1 for c in candidates),
      "cohorts":{name:counts[name] for name in COHORTS},"attempt_count":attempts}
    vault.close();db.close();return selected,summary


OPERATIONAL_EVIDENCE_FIELDS = ("lead_age","last_customer_communication","last_dealership_communication",
 "last_activity_type","outbound_calls","outbound_texts","outbound_emails","customer_replies",
 "consecutive_unanswered_attempts","two_way_text","contact_requested","sms_consent",
 "opt_out_or_dnc","wrong_or_invalid_number","closed_outcome","appointment_status",
 "active_salesperson_status","unresolved_recent_conversation","recent_outreach_excessive","language")

def _operational_row(c: Candidate) -> dict:
    row={"full_name":c.full_name,"phone":c.phone,"opportunity_id":c.opportunity_id,"vehicle":c.vehicle,
         "lead_source":c.lead_source,"cohort":c.cohort,"reason":c.reason,"recommended_message":c.message}
    row.update({key:(c.evidence or {}).get(key,"unknown") for key in OPERATIONAL_EVIDENCE_FIELDS})
    return row

def write_outputs(root: Path, candidates: list[Candidate], summary: dict) -> list[Path]:
    out=root/"outputs";out.mkdir(parents=True,exist_ok=True)
    recommendation = "AUTOMATED_PILOT_SUPPORTED" if summary["cohorts"]["AUTOMATED_SMS_READY"] else ("SMALL_MANUAL_PILOT_SUPPORTED" if summary["cohorts"]["MANUAL_ONE_TO_ONE_REVIEW"] else "LAUNCH_NOT_SUPPORTED")
    summary_path=out/"campaign-readiness-summary.md"
    summary_path.write_text("# Campaign readiness audit\n\n"+"\n".join(f"- {k}: {v}" for k,v in summary.items() if k!="cohorts")+"\n\n## Cohorts\n\n"+"\n".join(f"- {k}: {v}" for k,v in summary["cohorts"].items())+f"\n\n## Recommendation\n\n{recommendation}. Automated SMS is unsupported without indexed proof of both consent and mobile-line suitability. Any pilot is limited to 10 manually approved recipients, one message each, during normal local business hours; replies and opt-outs route to Jordan, duplicates are suppressed, and no automatic follow-up is permitted.\n",encoding="utf-8")
    fields=["full_name","phone","opportunity_id","vehicle","lead_source","cohort","reason","recommended_message",*OPERATIONAL_EVIDENCE_FIELDS]
    def csv_file(path,rows):
        with path.open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();[w.writerow(_operational_row(c)) for c in rows]
    ready=out/"campaign-ready-recipients.csv";manual=out/"manual-review-recipients.csv";suppressed=out/"suppressed-recipients.csv"
    csv_file(ready,[c for c in candidates if c.cohort=="AUTOMATED_SMS_READY"])
    csv_file(manual,[c for c in candidates if c.cohort in ("MANUAL_ONE_TO_ONE_REVIEW","CALL_ONLY_OR_PERSONAL_FOLLOW_UP")])
    csv_file(suppressed,[c for c in candidates if c.cohort in ("SUPPRESS","INSUFFICIENT_EVIDENCE")])
    operational=out/"campaign-readiness-operational.html"
    rows="".join("<tr>"+"".join(f"<td>{html.escape(str(_operational_row(c)[k]))}</td>" for k in fields)+"</tr>" for c in candidates)
    operational.write_text("<!doctype html><meta charset='utf-8'><title>Campaign readiness</title><h1>Campaign readiness operational review</h1><p>Local salesperson review only. No sending is enabled.</p><table border='1'><thead><tr>"+"".join(f"<th>{html.escape(k)}</th>" for k in fields)+"</tr></thead><tbody>"+rows+"</tbody></table>",encoding="utf-8")
    return [summary_path,operational,ready,manual,suppressed]
