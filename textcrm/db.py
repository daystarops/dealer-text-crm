"""SQLite storage: leads, messages, notes, offers."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  first_name TEXT NOT NULL DEFAULT '',
  vehicle TEXT NOT NULL DEFAULT '',
  salesperson TEXT NOT NULL DEFAULT '',
  lead_source TEXT NOT NULL DEFAULT '',
  store TEXT NOT NULL DEFAULT '',
  date_in TEXT,                 -- ISO date the lead entered CDK
  opp_id TEXT,
  lease_end TEXT,               -- ISO date of estimated last payment
  lease_term TEXT, lease_payment TEXT, lease_vehicle TEXT, vin TEXT,
  lease_still_owns INTEGER, lease_renewed INTEGER,
  status TEXT NOT NULL DEFAULT 'active',   -- active | opted_out | paused
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  direction TEXT NOT NULL,      -- in | out
  kind TEXT NOT NULL,           -- promo | auto | manual | inbound
  body TEXT NOT NULL,
  status TEXT NOT NULL,         -- queued | held | sent | failed | dismissed | received
  needs_review INTEGER NOT NULL DEFAULT 0,
  reason TEXT NOT NULL DEFAULT '',
  provider_id TEXT, offer_id INTEGER,
  created_at TEXT NOT NULL, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_lead ON messages(lead_id, id);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id),
  body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
  id INTEGER PRIMARY KEY, body TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT 'all',    -- all | lease
  active INTEGER NOT NULL DEFAULT 1, last_used TEXT, created_at TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',          -- vehicle model the offer is about, if any
  auto_key TEXT                            -- set on offers written by the inventory scan
);
CREATE TABLE IF NOT EXISTS inventory (
  vin TEXT PRIMARY KEY, site TEXT NOT NULL, stock TEXT, year TEXT, model TEXT NOT NULL, trim TEXT,
  msrp INTEGER, status TEXT, arrived TEXT, link TEXT, seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (            -- what the BDC hears about in their updates
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id), kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, reported_at TEXT
);
CREATE TABLE IF NOT EXISTS digests (id INTEGER PRIMARY KEY, slot TEXT NOT NULL UNIQUE, sent INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);  -- sent from the PC: your phone for alerts
CREATE TABLE IF NOT EXISTS audit (               -- each lead's history: where it came from, when CDK confirmed it, reads
  id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id), kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_lead ON audit(lead_id, id);
CREATE TABLE IF NOT EXISTS cdk_records (         -- one row per CDK lead record; a customer can have more than one
  opp_id TEXT PRIMARY KEY, lead_id INTEGER NOT NULL REFERENCES leads(id), text TEXT NOT NULL, read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_appointments (   -- the CDK Desk Log's appointments, as of the PC's last read
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, time TEXT NOT NULL, name TEXT NOT NULL, salesperson TEXT NOT NULL DEFAULT '',
  vehicle TEXT NOT NULL DEFAULT '', phone TEXT, lead_id INTEGER REFERENCES leads(id), confirmed INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT '', read_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    have = {row["name"] for row in con.execute("PRAGMA table_info(offers)")}
    for column, kind in (("model", "TEXT NOT NULL DEFAULT ''"), ("auto_key", "TEXT")):
        if column not in have:  # databases created before the inventory scan existed
            con.execute(f"ALTER TABLE offers ADD COLUMN {column} {kind}")
    have = {row["name"] for row in con.execute("PRAGMA table_info(leads)")}
    # The lead's CDK record address, its scrubbed text, when it was read, and whether it names an excluded rep.
    # person_at: when the rep last texted them from Quo by hand; quo_contact*: the matching Quo contact;
    # snooze_until: no offers before this date (they said "in a year"); last_contact: last contact logged in CDK;
    # plan / plan_resume / plan_every: your plan for this customer, followed by every reply and check-in.
    for column, kind in (("opp_url", "TEXT"), ("cdk_notes", "TEXT"), ("notes_read", "TEXT"), ("record_excluded", "INTEGER"),
                         ("person_at", "TEXT"), ("quo_contact_id", "TEXT"), ("quo_contact_sig", "TEXT"),
                         ("snooze_until", "TEXT"), ("last_contact", "TEXT"), ("sold_on", "TEXT"),
                         ("plan", "TEXT"), ("plan_resume", "TEXT"), ("plan_every", "INTEGER"), ("mine", "INTEGER"),
                         ("origin", "TEXT"), ("other_opps", "TEXT"), ("cdk_found", "TEXT")):
        if column not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {column} {kind}")
    # Leads you started by texting from Quo before CDK listed them, including ones from before this was tracked.
    con.execute("UPDATE leads SET origin='you' WHERE origin IS NULL AND coalesce(opp_id,'')='' AND (SELECT kind FROM messages m"
                " WHERE m.lead_id=leads.id ORDER BY m.id LIMIT 1)='manual'")
    con.execute("INSERT INTO audit(lead_id,kind,detail,created_at) SELECT id,'started_by_you','first text from you in Quo;"
                " waiting for CDK to list it',created_at FROM leads WHERE origin='you' AND id NOT IN (SELECT lead_id FROM audit)")
    con.commit()
    return con


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else ""


def first_name(name: str) -> str:
    """CDK shows 'Last, First'; companies have no comma and get no first name."""
    if "," not in name:
        return ""
    first = name.split(",", 1)[1].strip().split(" ")[0]
    return first.title() if first.isalpha() else ""


def rep_key(name: str) -> str:
    """'Rivera,A', 'RIVERA, ALEX' and 'Alex Rivera' all become 'torres:a'."""
    name = (name or "").strip().lower()
    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
    else:
        parts = name.split()
        first, last = (parts[0], parts[-1]) if len(parts) > 1 else ("", name)
    return f"{last}:{first[:1]}"


def upsert_lead(con: sqlite3.Connection, phone: str, **fields) -> int:
    """Insert or update by phone. Empty values never overwrite stored ones."""
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    stamp = now()
    row = con.execute("SELECT id FROM leads WHERE phone=?", (phone,)).fetchone()
    if row is None:
        columns = ["phone", "created_at", "updated_at", *fields]
        con.execute(
            f"INSERT INTO leads({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
            (phone, stamp, stamp, *fields.values()),
        )
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]
    if fields:
        assignments = ",".join(f"{column}=?" for column in fields)
        con.execute(f"UPDATE leads SET {assignments},updated_at=? WHERE id=?", (*fields.values(), stamp, row["id"]))
    return row["id"]


def log(con, lead_id: int, kind: str, detail: str = "") -> None:
    """Add to the lead's audit trail; the same entry twice in one day is kept once. No customer text goes here."""
    if con.execute("SELECT 1 FROM audit WHERE lead_id=? AND kind=? AND detail=? AND substr(created_at,1,10)=?",
                   (lead_id, kind, detail, now()[:10])).fetchone():
        return
    con.execute("INSERT INTO audit(lead_id,kind,detail,created_at) VALUES(?,?,?,?)", (lead_id, kind, detail[:120], now()))


def setting(con, key: str) -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def add_message(con, lead_id, direction, kind, body, status, *, needs_review=0, reason="", offer_id=None,
                provider_id=None) -> int:
    con.execute(
        "INSERT INTO messages(lead_id,direction,kind,body,status,needs_review,reason,offer_id,provider_id,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (lead_id, direction, kind, body, status, needs_review, reason, offer_id, provider_id or None, now()),
    )
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]
