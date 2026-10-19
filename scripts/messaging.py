"""No-send-by-default message queue and abstract personal-sender adapters."""
from __future__ import annotations

import abc
import json
import hashlib
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from cdk_runtime import normalize_phone
from pathlib import Path

from identity_vault import IdentityVault, Protector, DpapiProtector
from sender_config import SenderConfig

OPT_OUT_RE = re.compile(r"(?i)^\s*(?:STOP|UNSUBSCRIBE|CANCEL|END|QUIT)\s*[.!]?\s*$|\b(?:do not (?:text|contact|message)|stop (?:texting|messaging)|remove me)\b")
COMMITMENT_RE = re.compile(r"(?i)\b(?:guaranteed|approved|approval|monthly payment|down payment|interest rate|credit|financing|trade value|price is|in stock|available today)\b")


class DefinitelyNotSentError(RuntimeError):
    """Adapter guarantees no message left the device/provider."""


class UncertainDeliveryError(RuntimeError):
    """Adapter cannot prove whether the provider accepted the message."""


class SenderAdapter(abc.ABC):
    sender_mode: str
    @abc.abstractmethod
    def send_message(self, destination: str, body: str, thread_id: str = "") -> dict: ...
    @abc.abstractmethod
    def health_check(self) -> bool: ...


class DeviceBridgeAdapter(SenderAdapter):
    sender_mode="PERSONAL_DEVICE"
    @abc.abstractmethod
    def retrieve_threads(self) -> list[dict]: ...
    @abc.abstractmethod
    def retrieve_new_replies(self) -> list[dict]: ...
    @abc.abstractmethod
    def delivery_status(self, provider_message_id: str) -> str | None: ...
    @abc.abstractmethod
    def pause_sender(self) -> None: ...


class ApiProviderAdapter(SenderAdapter):
    sender_mode="PERSONAL_API"
    @abc.abstractmethod
    def receive_webhook(self, payload: bytes, headers: dict[str, str]) -> dict: ...
    @abc.abstractmethod
    def retrieve_conversation(self, thread_id: str) -> list[dict]: ...
    @abc.abstractmethod
    def message_status(self, provider_message_id: str) -> str: ...
    @abc.abstractmethod
    def normalize_phone(self, phone: str) -> str: ...
    @abc.abstractmethod
    def process_opt_out(self, message: str) -> bool: ...


@dataclass(frozen=True)
class EnqueueResult:
    message_id:str|None
    created_new:bool=False
    deduplicated:bool=False
    superseded_message_id:str|None=None
    blocked_reason:str|None=None
@dataclass(frozen=True)
class QueueRequest:
    lead_key: str
    destination: str
    sender_identity_reference: str
    message_body: str
    message_type: str
    eligibility_basis: str
    automation_mode: str
    conversation_thread_id: str = ""
    human_handoff_required: bool = False
    workflow_campaign: str = "default"
    template_version: str = "v1"
    deduplication_period_days: int = 30
    sms_limit: int = 320


class MessageQueue:
    def __init__(self, path: Path, protector: Protector | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path=path
        self.protector = protector or DpapiProtector(b"CDK-Lead-Copilot/message-queue-v1")
        self.connection = sqlite3.connect(path)
        self.connection.execute("""
          CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY, lead_key TEXT NOT NULL, destination BLOB NOT NULL,
            sender_identity_reference TEXT NOT NULL, message_body BLOB NOT NULL, message_type TEXT NOT NULL,
            eligibility_basis TEXT NOT NULL, automation_mode TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, approved_at TEXT, sent_at TEXT, provider_message_id TEXT,
            delivery_status TEXT, failure_reason TEXT, conversation_thread_id BLOB,
            reply_classification TEXT, human_handoff_required INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT NOT NULL, workflow_campaign TEXT NOT NULL, template_version TEXT NOT NULL
          )
        """)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        columns={row[1] for row in self.connection.execute("PRAGMA table_info(messages)")}
        additions=(("idempotency_key","TEXT"),("workflow_campaign","TEXT NOT NULL DEFAULT 'legacy'"),("template_version","TEXT NOT NULL DEFAULT 'legacy'"))
        for name,declaration in additions:
            if name not in columns:self.connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")
        rows=self.connection.execute("SELECT message_id FROM messages WHERE idempotency_key IS NULL OR idempotency_key='' ").fetchall()
        for (message_id,) in rows:self.connection.execute("UPDATE messages SET idempotency_key=? WHERE message_id=?",(hashlib.sha256(("legacy:"+message_id).encode()).hexdigest(),message_id))
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_message_key ON messages(idempotency_key) WHERE status IN ('DRAFT','PENDING_APPROVAL','APPROVED','SENDING','UNCERTAIN_DELIVERY')")
        self.connection.execute("PRAGMA user_version=2");self.connection.commit()

    def backup_before_destructive_migration(self) -> Path:
        backup=self.path.with_suffix(self.path.suffix+".migration-backup") if hasattr(self,"path") else Path("message-queue.db.migration-backup")
        destination=sqlite3.connect(backup)
        try:self.connection.backup(destination)
        finally:destination.close()
        return backup
    def close(self): self.connection.close()

    def enqueue(self, request: QueueRequest, vault: IdentityVault) -> EnqueueResult:
        body=request.message_body.strip()
        if not body or not " ".join(body.split()):raise ValueError("message body is empty")
        if len(body)>request.sms_limit:raise ValueError("message exceeds SMS limit")
        if request.message_type!="SMS":raise ValueError("unsupported message type")
        if request.automation_mode not in {"DRAFT_ONLY","APPROVAL_QUEUE","AUTOPILOT"}:raise ValueError("unsupported automation mode")
        if not request.sender_identity_reference.strip():raise ValueError("sender identity reference is required")
        if not request.workflow_campaign.strip() or not request.template_version.strip():raise ValueError("workflow and template identifiers are required")
        identity=vault.get(request.lead_key)
        if not identity:raise ValueError("unknown lead_key")
        if not request.eligibility_basis or request.eligibility_basis != identity.eligibility_basis or identity.sms_eligibility_status not in {"ELIGIBLE","DOCUMENTED"}:raise ValueError("documented communication eligibility is required")
        if identity.do_not_contact or identity.opt_out_timestamp:raise ValueError("suppressed leads cannot enter the queue")
        if request.destination != identity.phone_e164:raise ValueError("destination must resolve through the identity vault")
        normalized_destination=normalize_phone(request.destination)
        if not normalized_destination or request.destination != identity.phone_e164 or normalized_destination != normalize_phone(identity.phone_e164):raise ValueError("valid destination must resolve through the identity vault")
        normalized_body=" ".join(body.split()).casefold()
        destination_fingerprint=hashlib.sha256(("destination:"+normalized_destination).encode()).hexdigest()
        stale=self.connection.execute("SELECT message_id,destination,message_body,status FROM messages WHERE lead_key=? AND sender_identity_reference=? AND message_type=? AND workflow_campaign=? AND template_version=? AND status IN ('DRAFT','PENDING_APPROVAL','APPROVED','SENDING','UNCERTAIN_DELIVERY')",(request.lead_key,request.sender_identity_reference,request.message_type,request.workflow_campaign,request.template_version)).fetchall()
        if any(row[3] in {"SENDING","UNCERTAIN_DELIVERY"} for row in stale):return EnqueueResult(None,blocked_reason="reconciliation_required")
        superseded=None
        for stale_id,encrypted_destination,encrypted_body,stale_status in stale:
            if self.protector.unprotect(encrypted_destination)!=request.destination:
                changed=self.connection.execute("UPDATE messages SET status='SUPERSEDED' WHERE message_id=? AND status IN ('DRAFT','PENDING_APPROVAL','APPROVED')",(stale_id,)).rowcount
                if changed:superseded=stale_id
        self.connection.commit()
        material="|".join((request.lead_key,request.sender_identity_reference,request.message_type,normalized_body,request.template_version,request.workflow_campaign,destination_fingerprint))
        key=hashlib.sha256(material.encode()).hexdigest()
        cutoff=(datetime.now().astimezone()-timedelta(days=max(0,request.deduplication_period_days))).isoformat(timespec="seconds")
        existing=self.connection.execute("SELECT message_id,status,created_at,sent_at FROM messages WHERE idempotency_key=? ORDER BY created_at DESC LIMIT 1",(key,)).fetchone()
        reference_time=(existing[3] or existing[2]) if existing else ""
        if existing and (existing[1] in {"DRAFT","PENDING_APPROVAL","APPROVED","SENDING","UNCERTAIN_DELIVERY"} or existing[1]=="SENT" and request.deduplication_period_days>0 and reference_time>=cutoff):return EnqueueResult(existing[0],deduplicated=True)
        handoff=request.human_handoff_required or bool(COMMITMENT_RE.search(request.message_body));message_id=str(uuid.uuid4());status="DRAFT" if request.automation_mode=="DRAFT_ONLY" else "PENDING_APPROVAL"
        self.connection.execute("""INSERT INTO messages(message_id,lead_key,destination,sender_identity_reference,message_body,message_type,eligibility_basis,automation_mode,status,created_at,approved_at,sent_at,provider_message_id,delivery_status,failure_reason,conversation_thread_id,reply_classification,human_handoff_required,idempotency_key,workflow_campaign,template_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(message_id,request.lead_key,self.protector.protect(request.destination),request.sender_identity_reference,self.protector.protect(request.message_body),request.message_type,self.protector.protect(request.eligibility_basis),request.automation_mode,status,datetime.now().astimezone().isoformat(timespec="seconds"),None,None,None,None,None,self.protector.protect(request.conversation_thread_id),None,int(handoff),key,request.workflow_campaign,request.template_version))
        self.connection.commit();return EnqueueResult(message_id,created_new=True,superseded_message_id=superseded)
    def approve(self, message_ids: list[str]) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self.connection.executemany("UPDATE messages SET status='APPROVED', approved_at=? WHERE message_id=? AND status='PENDING_APPROVAL'", [(timestamp, item) for item in message_ids])
        self.connection.commit()

    def autopilot_allowed(self, message_id: str, config: SenderConfig, adapter: SenderAdapter, vault: IdentityVault,
                          now: datetime, lead_source: str, frequency_ok: bool, message_valid: bool) -> bool:
        config.validate()
        row = self.connection.execute("SELECT lead_key,human_handoff_required,automation_mode FROM messages WHERE message_id=?", (message_id,)).fetchone()
        if not row or row[2] != "AUTOPILOT" or not config.autopilot_explicitly_enabled: return False
        identity = vault.get(row[0])
        return bool(identity and not identity.do_not_contact and not identity.opt_out_timestamp and
                    identity.sms_eligibility_status in {"ELIGIBLE", "DOCUMENTED"} and
                    lead_source in config.approved_lead_sources and config.within_contact_hours(now) and
                    frequency_ok and message_valid and not row[1] and adapter.health_check())

    def send_approved(self, message_id: str, adapter: SenderAdapter, config: SenderConfig, vault: IdentityVault, now: datetime | None = None) -> dict:
        """Atomically claim one approved message, revalidate, and send at most once."""
        config.validate(); now=now or datetime.now().astimezone()
        if getattr(adapter,"sender_mode",None)!=config.sender_mode:raise ValueError("sender adapter mode mismatch")
        if not config.within_contact_hours(now): raise ValueError("outside permitted contact hours")
        if not adapter.health_check(): raise ValueError("sender adapter is unhealthy")
        self.connection.execute("BEGIN IMMEDIATE")
        row=self.connection.execute("SELECT lead_key,destination,message_body,conversation_thread_id,status,human_handoff_required,eligibility_basis FROM messages WHERE message_id=?",(message_id,)).fetchone()
        if not row or row[4] != "APPROVED" or row[5]: self.connection.rollback(); raise ValueError("message is not sendable")
        sender_ref=self.connection.execute("SELECT sender_identity_reference FROM messages WHERE message_id=?",(message_id,)).fetchone()[0]
        if sender_ref!=config.sender_identity_key:self.connection.rollback();raise ValueError("queued sender does not match active sender")
        identity=vault.get(row[0]);destination=self.protector.unprotect(row[1]);normalized=normalize_phone(destination)
        if not identity or identity.do_not_contact or identity.opt_out_timestamp or identity.sms_eligibility_status not in {"ELIGIBLE","DOCUMENTED"}: self.connection.rollback(); raise ValueError("lead is suppressed or ineligible")
        if not self.protector.unprotect(row[6]) or not normalized or normalized != normalize_phone(identity.phone_e164) or destination != identity.phone_e164: self.connection.rollback(); raise ValueError("destination or eligibility changed")
        changed=self.connection.execute("UPDATE messages SET status='SENDING' WHERE message_id=? AND status='APPROVED'",(message_id,)).rowcount
        if changed != 1:self.connection.rollback();raise ValueError("message was already claimed")
        self.connection.commit()
        try:result=adapter.send_message(destination,self.protector.unprotect(row[2]),self.protector.unprotect(row[3]))
        except DefinitelyNotSentError as exc:
            self.connection.execute("UPDATE messages SET status='FAILED',failure_reason=? WHERE message_id=?",(type(exc).__name__,message_id));self.connection.commit();raise
        except Exception as exc:
            self.connection.execute("UPDATE messages SET status='UNCERTAIN_DELIVERY',failure_reason=? WHERE message_id=?",(type(exc).__name__,message_id));self.connection.commit();raise UncertainDeliveryError("provider outcome requires reconciliation") from exc
        provider_id=str(result.get("provider_message_id") or result.get("id") or "");delivery=result.get("delivery_status");sent_at=now.isoformat(timespec="seconds")
        # Provider acceptance followed by a crash leaves SENDING. Never auto-retry it.
        self.connection.execute("UPDATE messages SET status='SENT',provider_message_id=?,sent_at=?,delivery_status=?,failure_reason=NULL WHERE message_id=? AND status='SENDING'",(provider_id,sent_at,delivery,message_id));self.connection.commit();vault.update_message_state(row[0],sent_at,provider_id);return result
    def retry_failed(self,message_id:str)->bool:
        changed=self.connection.execute("UPDATE messages SET status='PENDING_APPROVAL',approved_at=NULL WHERE message_id=? AND status='FAILED'",(message_id,)).rowcount;self.connection.commit();return changed==1

def process_inbound_opt_out(lead_key: str, message: str, vault: IdentityVault, timestamp: str) -> bool:
    if not OPT_OUT_RE.search(message): return False
    vault.suppress(lead_key, timestamp); return True
