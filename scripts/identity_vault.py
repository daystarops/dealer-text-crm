"""Git-ignored SQLite identity vault with Windows user-scoped DPAPI encryption."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import sys
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


class Protector(Protocol):
    def protect(self, value: str) -> bytes: ...
    def unprotect(self, value: bytes) -> str: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class DpapiProtector:
    """Encrypt data for the current Windows user; no application key is stored."""
    def __init__(self, entropy: bytes = b"CDK-Lead-Copilot/identity-v1"):
        if sys.platform != "win32":
            raise RuntimeError("DPAPI identity vault requires Windows")
        self.entropy = entropy

    @staticmethod
    def _blob(data: bytes):
        buffer = ctypes.create_string_buffer(data)
        return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

    def protect(self, value: str) -> bytes:
        source, source_buffer = self._blob(value.encode("utf-8"))
        entropy, entropy_buffer = self._blob(self.entropy)
        output = _DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "", ctypes.byref(entropy), None, None, 0, ctypes.byref(output))
        if not ok: raise ctypes.WinError()
        try: return ctypes.string_at(output.pbData, output.cbData)
        finally: ctypes.windll.kernel32.LocalFree(output.pbData)

    def unprotect(self, value: bytes) -> str:
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(self.entropy)
        output = _DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0, ctypes.byref(output))
        if not ok: raise ctypes.WinError()
        try: return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally: ctypes.windll.kernel32.LocalFree(output.pbData)


@dataclass
class IdentityRecord:
    cdk_opportunity_id: str
    first_name: str
    last_name: str
    full_name: str
    phone_e164: str
    vehicle: str = ""
    conversation_thread_id: str = ""
    sms_eligibility_status: str = "UNKNOWN"
    eligibility_basis: str = ""
    eligibility_source: str = ""
    eligibility_timestamp: str = ""
    do_not_contact: bool = False
    opt_out_timestamp: str = ""
    last_message_at: str = ""
    provider_message_id: str = ""

    @property
    def lead_key(self) -> str:
        return hashlib.sha256(("cdk-opportunity:" + self.cdk_opportunity_id).encode()).hexdigest()


SENSITIVE_FIELDS = tuple(IdentityRecord.__dataclass_fields__)


class IdentityVault:
    def __init__(self, path: Path, protector: Protector | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector or DpapiProtector()
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self.connection.execute("""
          CREATE TABLE IF NOT EXISTS identities (
            lead_key TEXT PRIMARY KEY,
            cdk_opportunity_id BLOB NOT NULL,
            encrypted_record BLOB NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
          )
        """)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS eligibility_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_key TEXT NOT NULL, status TEXT NOT NULL, basis TEXT NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
        self.connection.commit()

    def _migrate(self) -> None:
        version=self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version<1:
            self.connection.execute("PRAGMA user_version=1");self.connection.commit()

    def backup_before_destructive_migration(self) -> Path:
        backup=self.path.with_suffix(self.path.suffix+".migration-backup")
        destination=sqlite3.connect(backup)
        try:self.connection.backup(destination)
        finally:destination.close()
        return backup
    def close(self) -> None: self.connection.close()

    def upsert(self, record: IdentityRecord) -> str:
        payload = json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"))
        self.connection.execute(
            "INSERT INTO identities(lead_key,cdk_opportunity_id,encrypted_record) VALUES(?,?,?) "
            "ON CONFLICT(lead_key) DO UPDATE SET cdk_opportunity_id=excluded.cdk_opportunity_id, encrypted_record=excluded.encrypted_record, updated_at=CURRENT_TIMESTAMP",
            (record.lead_key, self.protector.protect(record.cdk_opportunity_id), self.protector.protect(payload)),
        )
        self.connection.commit()
        return record.lead_key

    def extraction_upsert(self, extracted: IdentityRecord) -> str:
        """Merge only CRM-derived identity fields; preserve all operational state."""
        current=self.get(extracted.lead_key)
        if current is None:return self.upsert(extracted)
        if extracted.cdk_opportunity_id != current.cdk_opportunity_id:raise ValueError("cdk_opportunity_id is immutable")
        for field in ("first_name","last_name","full_name","vehicle","conversation_thread_id"):
            value=getattr(extracted,field).strip()
            if value:setattr(current,field,value)
        if extracted.phone_e164 and extracted.phone_e164.startswith("+") and extracted.phone_e164[1:].isdigit():current.phone_e164=extracted.phone_e164
        return self.upsert(current)
    def get(self, lead_key: str) -> IdentityRecord | None:
        row = self.connection.execute("SELECT encrypted_record FROM identities WHERE lead_key=?", (lead_key,)).fetchone()
        return IdentityRecord(**json.loads(self.protector.unprotect(row[0]))) if row else None

    def get_by_opportunity(self, opportunity_id: str) -> IdentityRecord | None:
        key = hashlib.sha256(("cdk-opportunity:" + opportunity_id).encode()).hexdigest()
        return self.get(key)

    def update_message_state(self, lead_key:str, timestamp:str, provider_message_id:str) -> None:
        record=self.get(lead_key)
        if not record:raise KeyError(lead_key)
        record.last_message_at=timestamp;record.provider_message_id=provider_message_id;self.upsert(record)
    def suppress(self, lead_key: str, timestamp: str) -> None:
        record = self.get(lead_key)
        if not record: raise KeyError(lead_key)
        record.do_not_contact = True; record.opt_out_timestamp = timestamp; record.sms_eligibility_status = "OPTED_OUT"
        self.upsert(record)

    def set_eligibility(self, lead_key: str, status: str, basis: str, source: str, timestamp: str) -> None:
        if status not in {"UNKNOWN","ELIGIBLE","DOCUMENTED","INELIGIBLE","OPTED_OUT"}: raise ValueError("invalid eligibility status")
        if status in {"ELIGIBLE","DOCUMENTED"} and not basis.strip(): raise ValueError("eligibility basis is required")
        record=self.get(lead_key)
        if not record: raise KeyError(lead_key)
        record.sms_eligibility_status=status;record.eligibility_basis=basis;record.eligibility_source=source;record.eligibility_timestamp=timestamp;self.upsert(record)
        self.connection.execute("INSERT INTO eligibility_events(lead_key,status,basis,source,recorded_at) VALUES(?,?,?,?,?)",(lead_key,status,self.protector.protect(basis),self.protector.protect(source),timestamp));self.connection.commit()

def default_vault(root: Path) -> IdentityVault:
    return IdentityVault(root / "private" / "identity-vault.db")
