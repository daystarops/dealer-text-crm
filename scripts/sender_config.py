"""Personal-authorized sender configuration and contact-hour policy."""
from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from cdk_runtime import normalize_phone

VALID_MODES = {"PERSONAL_DEVICE", "PERSONAL_API"}
AUTOMATION_MODES = {"DRAFT_ONLY", "APPROVAL_QUEUE", "AUTOPILOT"}


@dataclass(frozen=True)
class SenderConfig:
    sender_ownership: str
    sender_mode: str
    sender_display_name: str
    sender_phone_e164: str
    dealership_name: str
    authorization_status: str
    authorization_scope: str
    authorized_by: str
    authorization_date: str
    permitted_channels: tuple[str, ...]
    permitted_contact_hours: dict
    default_automation_mode: str = "APPROVAL_QUEUE"
    autopilot_explicitly_enabled: bool = False
    approved_lead_sources: tuple[str, ...] = ()

    @property
    def sender_identity_key(self) -> str:
        self.validate()
        return hashlib.sha256(("sender:"+self.sender_mode+":"+self.sender_phone_e164).encode()).hexdigest()
    def validate(self) -> None:
        if self.sender_ownership != "PERSONAL_AUTHORIZED": raise ValueError("sender ownership is not authorized")
        normalized=normalize_phone(self.sender_phone_e164)
        if not normalized or normalized!=self.sender_phone_e164:raise ValueError("sender phone must already be normalized E.164")
        if not self.sender_display_name.strip() or not self.dealership_name.strip() or not self.authorization_scope.strip():raise ValueError("sender identity and authorization fields are required")
        try:time.fromisoformat(self.permitted_contact_hours["start"]);time.fromisoformat(self.permitted_contact_hours["end"])
        except (KeyError,TypeError,ValueError) as exc:raise ValueError("invalid permitted contact hours") from exc
        if self.authorization_status != "VALID": raise ValueError("personal sender authorization is not valid")
        if self.sender_mode not in VALID_MODES: raise ValueError("unsupported sender mode")
        if self.default_automation_mode not in AUTOMATION_MODES: raise ValueError("unsupported automation mode")
        if "SMS" not in self.permitted_channels: raise ValueError("SMS is not authorized")
        if self.default_automation_mode == "AUTOPILOT" and not self.autopilot_explicitly_enabled:
            raise ValueError("AUTOPILOT requires explicit enablement")

    def within_contact_hours(self, when: datetime) -> bool:
        start = time.fromisoformat(self.permitted_contact_hours["start"])
        end = time.fromisoformat(self.permitted_contact_hours["end"])
        return start <= when.timetz().replace(tzinfo=None) <= end


def load_sender_config(path: Path) -> SenderConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["permitted_channels"] = tuple(raw.get("permitted_channels", ()))
    raw["approved_lead_sources"] = tuple(raw.get("approved_lead_sources", ()))
    config = SenderConfig(**raw); config.validate(); return config
