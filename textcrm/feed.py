"""The daily refresh, split in two so the reading can happen on your PC and the rest in the cloud.

collect() reads CDK and both stores' websites through your Chrome. apply() stores what was read and
rewrites the automatic offers. With "cloud_url" set, the PC pushes what it collected to the cloud copy.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta

from . import cdk_sync, db, engine, guard, inventory, matching, replies
from .cdk_sync import SyncError
from .config import ROOT

NOTES_SEEN = ROOT / "private" / "notes-read.json"  # when this PC last read each lead's record


def collect(cfg: dict, browser, only: str | None = None) -> dict:
    payload: dict = {}
    if only != "inventory":
        tab = cdk_sync.Tab(browser)
        try:
            if only != "leases":
                payload["leads"] = cdk_sync.read_active_leads(tab, cfg["lead_window_days"])
                payload["sold"] = cdk_sync.read_sold(tab, cfg["lead_window_days"])  # who already bought from us
                try:  # who is booked to come in: today and the next desk_log_days
                    payload["desk"] = {"from": date.today().isoformat(), "days": cfg["desk_log_days"],
                                       "rows": cdk_sync.read_desk_log(tab, date.today(), cfg["desk_log_days"])}
                except SyncError as error:
                    print(f"desk log failed: {error}", flush=True)
            if only != "leads":
                today = date.today()
                before, after = cfg["lease_report_span"]
                payload["leases"] = cdk_sync.read_lease_report(tab, today + timedelta(days=before), today + timedelta(days=after))
        finally:
            tab.close()
    if only in (None, "inventory"):
        payload["inventory"] = {}
        for site, base in cfg["inventory_sites"].items():
            try:
                payload["inventory"][site] = inventory.read_site(browser, base.rstrip("/"))
            except SyncError as error:
                print(f"inventory {site} failed: {error}", flush=True)
            cdk_sync.pause(2, 4)
    return payload


def apply(con, cfg: dict, payload: dict, today: date | None = None) -> dict:
    today = today or date.today()
    result = {}
    if "leads" in payload:
        result["leads"] = cdk_sync.store_leads(con, payload["leads"], cfg["store"])
        result["leads"]["engaged"] = engine.engaged_opps(con, today)  # whose records the PC should read again
    if "sold" in payload:
        result["sold"] = cdk_sync.store_sold(con, payload["sold"], cfg["store"])
    if "notes" in payload:
        result["notes"] = matching.store_notes(con, payload["notes"])
    if "desk" in payload:
        result["desk"] = engine.store_desk(con, payload["desk"])
    if "plans" in payload:
        result["plans"] = engine.store_plans(con, payload["plans"], today)
    if "settings" in payload:
        result["settings"] = engine.store_settings(con, payload["settings"])
    if "checks" in payload:
        result["checks"] = checks(con, cfg, payload["checks"])
    if "do_not_text" in payload:
        result["do_not_text"] = engine.apply_do_not_text(con, dict(cfg, do_not_text=payload["do_not_text"]))
    if "leases" in payload:
        result["leases"] = cdk_sync.store_leases(con, payload["leases"], cfg["store"])
    if "inventory" in payload:
        result["inventory"] = {site: inventory.store_site(con, site, rows, today) for site, rows in payload["inventory"].items()}
        result["offers"] = inventory.build_offers(con, today)
    return result


CONFIRMED = re.compile(r"\bsee you\b|\byou'?re (all )?set\b|\bbooked\b|\bconfirmed\b|\bappointment (is )?set\b", re.I)


def stats(con, since: str) -> dict:
    """Counts since a date, no customer text: texts out by who sent them (the bot by kind, or you), texts in, who
    replied, buying signals, appointments asked for and confirmed by text, CDK showroom visits, and the endings."""
    msgs = con.execute("SELECT lead_id, direction, kind, status, body, created_at FROM messages WHERE created_at>=?"
                       " ORDER BY lead_id, id", (since,)).fetchall()
    out, days = {}, {}
    texted, replied, signals, asked, confirmed = set(), set(), set(), set(), set()
    recent_time = {}
    for m in msgs:
        day = days.setdefault(m["created_at"][:10], {"out": 0, "in": 0})
        if m["direction"] == "out":
            if m["status"] != "sent":
                out[f"not sent ({m['status']})"] = out.get(f"not sent ({m['status']})", 0) + 1
                continue
            who = "you" if m["kind"] == "manual" else f"bot: {m['kind']}"
            out[who] = out.get(who, 0) + 1
            day["out"] += 1
            texted.add(m["lead_id"])
            if recent_time.get(m["lead_id"]) and CONFIRMED.search(m["body"]):
                confirmed.add(m["lead_id"])
        else:
            day["in"] += 1
            if m["lead_id"] in texted or con.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='out' AND created_at<?",
                                                     (m["lead_id"], m["created_at"])).fetchone():
                replied.add(m["lead_id"])
            if guard.buying_signal(m["body"]):
                signals.add(m["lead_id"])
            named = bool(guard.NAMES_A_TIME.search(m["body"]))
            recent_time[m["lead_id"]] = named
            if named or guard.WANTS_CONTACT.search(m["body"]):
                asked.add(m["lead_id"])
    events = lambda kind: con.execute("SELECT count(DISTINCT lead_id) FROM events WHERE kind=? AND created_at>=?", (kind, since)).fetchone()[0]
    statuses = dict(con.execute("SELECT status, count(*) FROM leads WHERE updated_at>=? GROUP BY status", (since,)).fetchall())
    return {"since": since, "texts_out": sum(v for k, v in out.items() if not k.startswith("not sent")), "out_by": out,
            "texts_in": sum(d["in"] for d in days.values()), "leads_texted": len(texted), "leads_replied": len(replied),
            "reply_rate": f"{len(replied) / max(1, len(texted)):.1%}", "leads_with_buying_signals": len(signals),
            "asked_for_time_or_call": len(asked), "appointments_confirmed_by_text": len(confirmed),
            "showroom_visits_in_cdk": events("showroom"), "sold": events("sold"), "bought_elsewhere": events("bought_elsewhere"),
            "opted_out": con.execute("SELECT count(DISTINCT lead_id) FROM messages WHERE direction='in' AND reason!='' AND"
                                     " created_at>=? AND lead_id IN (SELECT id FROM leads WHERE status='opted_out')", (since,)).fetchone()[0],
            "leads_by_status_touched": statuses, "per_day": days,
            "failed": [dict(r) for r in con.execute(
                "SELECT substr(created_at,1,10) AS day, kind, substr(reason,1,80) AS reason, count(*) AS n, count(DISTINCT lead_id)"
                " AS leads, group_concat(DISTINCT substr((SELECT phone FROM leads l WHERE l.id=lead_id),-4)) AS last4"
                " FROM messages WHERE status='failed' AND created_at>=? GROUP BY 1,2,3 ORDER BY 1,4 DESC", (since,))],
            "active_leads": con.execute("SELECT count(*) FROM leads WHERE status='active'").fetchone()[0]}


CANARY = "The customer's dog is named Biscuit."


def checks(con, cfg: dict, asked: dict) -> dict:
    """Diagnostics for the PC, without customer text: per lead, its status, flags, workspace file sizes and how its
    last inbound text was handled; and one real Codex decision on made-up data with a planted fact, to prove Codex
    runs here and reads the expanded context."""
    out: dict = {"leads": {}}
    for phone in asked.get("leads", []):
        row = con.execute("SELECT * FROM leads WHERE phone=?", (db.normalize_phone(phone),)).fetchone()
        if not row:
            out["leads"][phone[-4:]] = "not found"
            continue
        lead = dict(row)
        last_in = con.execute("SELECT created_at, reason, needs_review FROM messages WHERE lead_id=? AND direction='in'"
                              " ORDER BY id DESC LIMIT 1", (lead["id"],)).fetchone()
        last = con.execute("SELECT direction, kind, status, created_at FROM messages WHERE lead_id=? ORDER BY id DESC LIMIT 1",
                           (lead["id"],)).fetchone()
        out["leads"][phone[-4:]] = {
            "status": lead["status"], "updated_at": lead["updated_at"], "snooze_until": lead["snooze_until"],
            "in_do_not_text": lead["phone"] in {db.normalize_phone(p) for p in cfg["do_not_text"]}, "blocked": engine.blocked(lead, cfg),
            "notes_read": lead["notes_read"], "has_plan": bool(lead.get("plan")),
            "workspace": {name: len(text) for name, text in engine.workspace(con, lead).items()},
            "last_inbound": dict(last_in) if last_in else None, "last_message": dict(last) if last else None,
            "origin": lead["origin"], "in_cdk": bool(lead["opp_id"]), "cdk_found": lead["cdk_found"],
            "cdk_records": 1 + len([o for o in (lead["other_opps"] or "").split(",") if o]) if lead["opp_id"] else 0,
            "audit": [f"{r['created_at']} {r['kind']}: {r['detail']}" for r in con.execute(
                "SELECT * FROM audit WHERE lead_id=? ORDER BY id DESC LIMIT 12", (lead["id"],))]}
    if asked.get("unverified"):
        # Leads you started from Quo that CDK's Active list has not shown since: not entered, or entered under
        # another phone number.
        out["unverified"] = [{"phone": r["phone"][-4:], "first_name": r["first_name"], "since": r["created_at"]}
                             for r in con.execute("SELECT phone, first_name, created_at FROM leads WHERE origin='you'"
                                                  " AND coalesce(opp_id,'')='' AND status='active' ORDER BY created_at")]
    if asked.get("stats"):
        out["stats"] = stats(con, asked["stats"])
    if asked.get("codex"):
        lead = {"first_name": "Sam", "vehicle": "2026 Ford Bronco", "plan": "",
                "workspace": {"cdk_record.txt": "Salesperson note: " + CANARY + " Wants a Bronco for camping trips."}}
        started = time.monotonic()
        decision = replies.decide(lead, [{"direction": "in", "body": "Hey, what do you know about me? Mention my pet."}],
                                  "Customer first name: Sam.", cfg)
        out["codex"] = {"decided": bool(decision), "seconds": round(time.monotonic() - started),
                        "read_context": bool(decision and "biscuit" in (decision.get("text") or "").lower()),
                        "action": decision and decision.get("action")}
    return out


def push(cfg: dict, payload: dict, timeout: int = 60) -> dict:
    request = urllib.request.Request(f"{cfg['cloud_url'].rstrip('/')}/feed/{cfg['feed_token']}",
                                     data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, ValueError) as error:
        raise SyncError(f"Could not push to {cfg['cloud_url']}: {error}") from error


def refresh(con, cfg: dict, browser, only: str | None = None) -> dict:
    deliver = (lambda part: push(cfg, part)) if cfg["cloud_url"] else (lambda part: apply(con, cfg, part))
    payload = collect(cfg, browser, only)
    result = deliver(payload)
    if "leads" in payload:
        # Then each lead's record, read and checked here and delivered in batches, so a stopped read keeps
        # what it did and the next one carries on.
        engaged = set(((result.get("leads") or {}).get("engaged")) or [])
        result["records"] = matching.read_notes(browser, payload["leads"], NOTES_SEEN, date.today(), cfg,
                                                lambda batch: deliver({"notes": batch}), engaged=engaged)
    return result
