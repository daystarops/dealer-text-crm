"""Structured identity redaction and residual-PII fail-closed gate."""
from __future__ import annotations
import json,re
from dataclasses import dataclass

PROMPT_INJECTION=re.compile(r"(?i)(ignore (?:all |previous )?instructions|system prompt|developer message|use browser|run shell|call tool|exfiltrate)")
ISO_DATE=re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
PII_PATTERNS={
 "phone":re.compile(r"(?<!\d)(?:\(\d{3}\)\s*\d{3}-\d{4}|\d{3}-\d{3}-\d{4}|\d{3}\s\d{3}\s\d{4}|\+[1-9]\d{0,2}(?:\s\d{2,4}){2,4})(?!\d)"),
 "email":re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
 "street_address":re.compile(r"(?i)\b\d{1,6}\s+[\w.' -]+\s(?:st(?:reet)?|ave(?:nue)?|rd|road|blvd|boulevard|ln|lane|dr|drive|ct|court)\b"),
 "apartment":re.compile(r"(?i)\b(?:apt|apartment|unit|suite|#)\s*[A-Z0-9-]+\b"),"po_box":re.compile(r"(?i)\bP\.?\s*O\.?\s*Box\s+\d+\b"),
 "dob":re.compile(r"(?i)\b(?:DOB|birth(?:day| date))\s*:?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),"vin":re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b",re.I),
 "stock":re.compile(r"(?i)\bstock\s*#?\s*:?\s*[A-Z0-9-]+"),"plate":re.compile(r"(?i)\b(?:license plate|plate)\s*:?\s*[A-Z0-9-]{4,10}\b"),
 "license":re.compile(r"(?i)\bdriver'?s?\s+licen[sc]e\b[^|\n]*"),"ssn":re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
 "financial":re.compile(r"(?i)\b(?:bank account|routing number|credit score|credit report|card number)\b[^|\n]*"),
 "unlabeled_birth_date":re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](?:19\d{2}|200\d|2010)\b"),
 "customer_identifier":re.compile(r"(?i)\b(?:customer|account|record|opportunity)\s*(?:id|number|#)\s*:?\s*[A-Z0-9-]{5,}\b"),
 "long_number":re.compile(r"(?<!\d)\d{9,}(?!\d)"),"third_party":re.compile(r"(?i)\b(?:spouse|co-signer|cosigner|third party)\s*:?\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b")}

class PrivacyGateError(RuntimeError):pass


def _identity_context_patterns(full_name:str,first_name:str):
    parts=[part for part in full_name.split() if part]
    if len(parts)<2:return []
    surname=parts[-1];first=first_name or parts[0]
    return [
      (re.compile(rf"(?i)\b(Mr|Mrs|Ms|Miss|Dr)\.?\s+{re.escape(surname)}\b"),lambda m:f"{m.group(1)} {first}"),
      (re.compile(rf"(?i)\b(customer|buyer|prospect|lead|client|spouse|co-signer|cosigner)\s*:\s*{re.escape(surname)}\b"),lambda m:f"{m.group(1)}: {first}"),
      (re.compile(rf"(?i)\b(customer|buyer|prospect|lead|client)\s+{re.escape(surname)}\b"),lambda m:f"{m.group(1)} {first}"),
      (re.compile(rf"(?i)\b(spoke\s+(?:with|to)|called|contacted|texted|emailed|followed\s+up\s+with)\s+{re.escape(surname)}\b"),lambda m:f"{m.group(1)} {first}"),
      (re.compile(rf"(?i)(?<!\w){re.escape(surname)}\s*,\s*{re.escape(first)}(?!\w)"),lambda m:first),
    ]


@dataclass(frozen=True)
class GateResult:safe_payload:dict;untrusted_data_present:bool


def sanitize_text(text:str,identities:dict[str,str]|None=None)->str:
    value=str(text);dates=[]
    def hold(match):dates.append(match.group(0));return f"__ISO_DATE_{len(dates)-1}__"
    value=ISO_DATE.sub(hold,value)
    for label,pattern in PII_PATTERNS.items():value=pattern.sub(f"[{label} removed]",value)
    # Replace only exact complete customer identities, preserving ordinary surname tokens.
    for full_name,first_name in sorted((identities or {}).items(),key=lambda item:len(item[0]),reverse=True):
        if not full_name:continue
        value=re.sub(rf"(?i)(?<!\w){re.escape(full_name)}(?!\w)",first_name or "[customer identity removed]",value)
        for pattern,replacement in _identity_context_patterns(full_name,first_name):value=pattern.sub(replacement,value)
    value=PROMPT_INJECTION.sub("[untrusted instruction removed]",value)
    for index,date_value in enumerate(dates):value=value.replace(f"__ISO_DATE_{index}__",date_value)
    return value


def sanitize_payload(payload:dict,identities:dict[str,str]|None=None)->GateResult:
    def walk(value):
        if isinstance(value,str):return sanitize_text(value,identities)
        if isinstance(value,list):return [walk(item) for item in value]
        if isinstance(value,dict):return {key:(item if key=="lead_key" else walk(item)) for key,item in value.items()}
        return value
    safe=walk(payload)
    def detector_view(value):
        if isinstance(value,list):return [detector_view(item) for item in value]
        if isinstance(value,dict):return {key:("[lead key]" if key=="lead_key" else detector_view(item)) for key,item in value.items()}
        return value
    serialized=json.dumps(detector_view(safe),ensure_ascii=False)
    findings=[label for label,pattern in PII_PATTERNS.items() if pattern.search(ISO_DATE.sub("",serialized))]
    full_names=[name for name in (identities or {}) if name and re.search(rf"(?i)(?<!\w){re.escape(name)}(?!\w)",serialized)]
    contextual=[name for name,first in (identities or {}).items() for pattern,_ in _identity_context_patterns(name,first) if pattern.search(serialized)]
    if findings or full_names or contextual:raise PrivacyGateError(f"Residual sensitive content detected; categories={sorted(set(findings))}; full_identity_count={len(full_names)}; contextual_count={len(contextual)}; analysis was not started")
    return GateResult(safe,bool(PROMPT_INJECTION.search(json.dumps(payload))))


def assert_no_phone(text:str)->None:
    without_dates=ISO_DATE.sub("",text)
    if PII_PATTERNS["phone"].search(without_dates):raise PrivacyGateError("Phone number detected in shareable content")

def assert_no_phone_or_identity(text:str,identities:dict[str,str]|None=None)->None:
    without_dates=ISO_DATE.sub("",text)
    if PII_PATTERNS["phone"].search(without_dates):raise PrivacyGateError("Phone number detected in shareable content")
    for full_name,first_name in (identities or {}).items():
        if full_name and re.search(rf"(?i)(?<!\w){re.escape(full_name)}(?!\w)",text):raise PrivacyGateError("Full customer identity detected")
        if any(pattern.search(text) for pattern,_ in _identity_context_patterns(full_name,first_name)):raise PrivacyGateError("Contextual customer identity detected")

# Compatibility name; semantics now use exact full identities, not surname tokens.
assert_no_phone_or_surname=assert_no_phone_or_identity

def assert_no_residual_pii(text:str,identities:dict[str,str]|None=None)->None:
    """Fail closed on every supported residual PII class."""
    detector_text=text
    try:
        parsed=json.loads(text)
        def mask_keys(value):
            if isinstance(value,list):return [mask_keys(item) for item in value]
            if isinstance(value,dict):return {key:("[lead key]" if key=="lead_key" else mask_keys(item)) for key,item in value.items()}
            return value
        detector_text=json.dumps(mask_keys(parsed),ensure_ascii=False)
    except (json.JSONDecodeError,TypeError):
        pass
    searchable=ISO_DATE.sub("",detector_text)
    findings=[label for label,pattern in PII_PATTERNS.items() if pattern.search(searchable)]
    full_names=[name for name in (identities or {}) if name and re.search(rf"(?i)(?<!\w){re.escape(name)}(?!\w)",detector_text)]
    contextual=[name for name,first in (identities or {}).items() for pattern,_ in _identity_context_patterns(name,first) if pattern.search(detector_text)]
    if findings or full_names or contextual:raise PrivacyGateError(f"Residual sensitive content detected; categories={sorted(set(findings))}; full_identity_count={len(full_names)}; contextual_count={len(contextual)}")