"""Pure policy for individually reviewed CDK enrichment results."""
from __future__ import annotations
import re
from campaign_readiness import normalize_phone

LIVE_PHONE_VERIFIED="LIVE_PHONE_VERIFIED"
INDEXED_PHONE_VERIFIED_BY_IDENTITY_PAIRING="INDEXED_PHONE_VERIFIED_BY_IDENTITY_PAIRING"
PHONE_CONFLICT="PHONE_CONFLICT"
PHONE_UNAVAILABLE="PHONE_UNAVAILABLE"
PHONE_CONFIDENCE=frozenset({LIVE_PHONE_VERIFIED,INDEXED_PHONE_VERIFIED_BY_IDENTITY_PAIRING,PHONE_CONFLICT,PHONE_UNAVAILABLE})
DIRECT_SUPPRESSION=re.compile(
 r"\b(?:stop|unsubscribe|do not (?:contact|call|text)|wrong number|disconnected|not in service|"
 r"complain(?:ed|t)?|already (?:bought|purchased)|purchased elsewhere|no longer (?:shopping|interested)|"
 r"status\s*:?\s*(?:sold|closed)|opportunity\s+(?:sold|closed))\b",re.I)
QUESTION=re.compile(r"\?|\b(?:asked|question|wondering|can you|could you|how much|payment|finance|trade)\b",re.I)

def phone_confidence(*,indexed_phone,live_phone="",identity_pairing_valid=True,resolver_failed=False):
    """Resolve confidence without making optional live observation a mandatory gate."""
    del resolver_failed
    indexed=normalize_phone(indexed_phone);live=normalize_phone(live_phone)
    if not identity_pairing_valid:return PHONE_UNAVAILABLE,""
    if live:
        if not indexed or live!=indexed:return PHONE_CONFLICT,indexed or live
        return LIVE_PHONE_VERIFIED,indexed
    if indexed:return INDEXED_PHONE_VERIFIED_BY_IDENTITY_PAIRING,indexed
    return PHONE_UNAVAILABLE,""

def deduplicate_by_phone(rows):
    selected=[];seen=set()
    for row in rows:
        phone=normalize_phone(row.get("phone",""))
        if not phone or phone in seen:continue
        seen.add(phone);selected.append(row)
    return selected

def classify_enrichment(*,indexed_phone,live_phone="",identity_pairing_valid=True,
                        resolver_failed=False,status="",activity_text="",
                        inbound_present=False,unanswered_attempts=0,
                        record_context_valid=True,vehicle="",
                        salesperson_assignment=None,contact_preference=None):
    """Classify manual outreach only; ownership and missing generic preference are neutral."""
    del salesperson_assignment,contact_preference
    confidence,phone=phone_confidence(indexed_phone=indexed_phone,live_phone=live_phone,
      identity_pairing_valid=identity_pairing_valid,resolver_failed=resolver_failed)
    if confidence==PHONE_CONFLICT:return "INSUFFICIENT_EVIDENCE","Live and encrypted indexed phones conflict","",confidence,phone
    if confidence==PHONE_UNAVAILABLE:return "INSUFFICIENT_EVIDENCE","No valid paired phone is available","",confidence,phone
    if not record_context_valid:return "INSUFFICIENT_EVIDENCE","Required opportunity or exhaustive record context is incomplete","",confidence,phone
    evidence="\n".join((status or "",activity_text or ""))
    if DIRECT_SUPPRESSION.search(evidence):return "SUPPRESS","Direct suppression, invalid-number, complaint, or closed-outcome evidence","",confidence,phone
    if unanswered_attempts>=8:return "SUPPRESS","Recent unanswered cadence is excessive","",confidence,phone
    if inbound_present and QUESTION.search(evidence):return "PERSONALIZED_RESPONSE_REQUIRED","Existing customer question or objection requires a specific response","",confidence,phone
    if re.search(r"\b(?:appointment|scheduled|cancelled|missed|no show)\b",evidence,re.I) or unanswered_attempts>=4:
        return "PERSONAL_CALL_RECOMMENDED","Appointment/cadence context is better handled by a personal call","",confidence,phone
    subject=vehicle or "your earlier vehicle inquiry"
    message=f"Hi, this is Jordan from Premier Ford of Bay Ridge following up about {subject}. Are you still shopping, or have you already found a vehicle? Reply STOP to opt out."
    return "MANUAL_TEXT_PILOT_APPROVED","Potentially relevant opportunity with no observed direct suppression; individual review required",message,confidence,phone
