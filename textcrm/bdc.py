"""Lead updates for the BDC: what changed on their leads, texted at most a couple of times a day, and only when
something worth knowing happened. One line per lead, first name and last initial, never a phone number."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import db

# What the BDC hears about, most important first.
KINDS = {"match": "I sent them a car that fits", "appointment": "wants to come in", "showroom": "came into the showroom",
         "vehicle_change": "now looking at", "financing": "asked me about financing", "sold": "bought from us",
         "bought_elsewhere": "bought elsewhere"}


def record(con, lead_id: int, kind: str, detail: str = "") -> None:
    """Note something the BDC should hear about; once per lead and kind per day."""
    if con.execute("SELECT 1 FROM events WHERE lead_id=? AND kind=? AND substr(created_at,1,10)=?",
                   (lead_id, kind, date.today().isoformat())).fetchone():
        return
    con.execute("INSERT INTO events(lead_id,kind,detail,created_at) VALUES(?,?,?,?)", (lead_id, kind, detail[:80], db.now()))


def is_staff(phone: str, cfg: dict) -> bool:
    return phone in {db.normalize_phone(p) for p in cfg["bdc_numbers"]}


def digest_text(con, cfg: dict, limit: int = 10) -> tuple[str, list[int]]:
    """The update and the event IDs it covers; ('', []) when nothing is new."""
    order = {kind: n for n, kind in enumerate(KINDS)}
    rows = sorted(con.execute("SELECT e.*, l.name, l.first_name, l.vehicle FROM events e JOIN leads l ON l.id=e.lead_id"
                              " WHERE e.reported_at IS NULL ORDER BY e.id").fetchall(), key=lambda r: (order.get(r["kind"], 99), r["id"]))
    if not rows:
        return "", []
    lines = []
    for r in rows[:limit]:
        last = (r["name"] or "").split(",")[0].strip()[:1]
        who = f"{r['first_name'] or 'Unknown'} {last + '.' if last else ''}".strip()
        what = f"{KINDS.get(r['kind'], r['kind'])} {r['detail']}".strip()
        car = re.sub(r"^\s*(?:19|20)\d{2}\s+|\bFord\s+", "", r["vehicle"] or "").strip()
        lines.append(f"- {who}{f' ({car})' if car and r['kind'] != 'vehicle_change' else ''}: {what}")
    more = f"\n+{len(rows) - limit} more" if len(rows) > limit else ""
    return "Updates on my leads:\n" + "\n".join(lines) + more, [r["id"] for r in rows]


def maybe_send(con, sender, cfg: dict, now: datetime | None = None) -> int:
    """At each update time, if anything new happened since the last one, text it to the BDC. Returns texts sent."""
    now = now or datetime.now()
    due = [t for t in cfg["bdc_times"] if now.strftime("%H:%M") >= t]
    if not cfg.get("bdc_updates") or not cfg["bdc_numbers"] or not due:
        return 0
    slot = f"{now.date().isoformat()} {due[-1]}"
    if con.execute("SELECT 1 FROM digests WHERE slot=?", (slot,)).fetchone():
        return 0
    sent_today = con.execute("SELECT count(*) FROM digests WHERE substr(slot,1,10)=? AND sent>0", (now.date().isoformat(),)).fetchone()[0]
    text, ids = digest_text(con, cfg)
    if text and not con.execute("SELECT 1 FROM digests WHERE sent>0").fetchone():
        # The very first one says who this is: they will not have this number saved.
        text = (f"Hey, it's {cfg['rep_name']}. This is the number I text my leads from, and I'll send you quick updates "
                f"here when something changes on them.\n\n{text}")
    sent = 0
    if text and sent_today < cfg["bdc_max_per_day"]:
        for phone in cfg["bdc_numbers"]:
            try:
                sender.send(db.normalize_phone(phone), text)
                sent += 1
            except Exception as error:  # one staff number failing should not stop the other
                print(f"bdc update to ...{phone[-4:]} failed: {error}", flush=True)
        if sent:
            con.execute(f"UPDATE events SET reported_at=? WHERE id IN ({','.join('?' * len(ids))})", (db.now(), *ids))
    con.execute("INSERT INTO digests(slot,sent,created_at) VALUES(?,?,?)", (slot, sent, db.now()))
    con.commit()
    return sent


def showroom_visit(notes: str, today: date) -> bool:
    """A showroom line dated today or yesterday in a lead's CDK record."""
    for line in (notes or "").splitlines():
        if "showroom" not in line.lower():
            continue
        for month, day, year in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b", line):
            try:
                when = date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day))
            except ValueError:
                continue
            if today - timedelta(days=1) <= when <= today:
                return True
    return False
