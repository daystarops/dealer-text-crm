"""Core behavior: who gets today's text, sending the queue, and handling inbound texts."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

from . import bdc, config, db, guard, replies
from .sender import SendError


def excluded_rep(salesperson: str, cfg: dict) -> bool:
    """A lead is excluded if any of its salespeople is on the list (CDK may list several, one per line)."""
    excluded = {db.rep_key(name) for name in cfg["excluded_reps"]}
    keys = [db.rep_key(name) for name in salesperson.splitlines() if name.strip()]
    if any(key in excluded for key in keys):
        return True
    only = {db.rep_key(name) for name in cfg["only_reps"]}
    return bool(only) and not any(key in only for key in keys)


CONTACT_LOGGED = re.compile(r"(?i)\b(?:call\w*|phone|text\w*|sms|e-?mail\w*|message\w*|note\w*|appointment\w*|appt|showroom|"
                            r"visit\w*|spoke|left (?:a )?(?:message|vm|voicemail)|contact\w*|follow.?up)\b")
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b")


def last_contact(record: str, today: date) -> str | None:
    """The most recent logged contact in a lead's CDK record (its history frame when the record has several), as
    an ISO date, or None if none is found."""
    frames = record.split("\n----\n")
    found = []
    for line in "\n".join(frames[1:] or frames).splitlines():
        if not CONTACT_LOGGED.search(line):
            continue
        for month, day, year in DATE.findall(line):
            try:
                when = date(int(year) + (2000 if len(year) == 2 else 0), int(month), int(day))
            except ValueError:
                continue
            if when <= today:
                found.append(when)
    return max(found).isoformat() if found else None


def names_excluded_rep(text: str, cfg: dict) -> bool:
    """True if a lead's full CDK record names anyone on the excluded list ('Lee, Morgan', 'Morgan Lee' or 'Lee, M')."""
    for name in cfg["excluded_reps"]:
        last, _, first = (part.strip() for part in name.partition(","))
        if not last or not first:
            continue
        last, first = re.escape(last), re.escape(first)
        if re.search(rf"(?i)\b{last}\s*,\s*(?:{first}\b|{first[:2]}\b)|\b{first}\s+{last}\b", text or ""):
            return True
    return False


EXCLUDED, PERSON, UNCHECKED = "assigned to an excluded salesperson", "you're texting them yourself", "CDK record not checked yet"
NOTES_SAY_STOP = "CDK notes say not to text (out of market, bought, do not contact)"
# Nothing automatic goes to these: an excluded rep's lead, a lead whose CDK record (and so its salespeople and
# notes) has not been checked yet, or one whose notes say they are done. A thread you are running (PERSON) is
# still answered, just not instantly: the sweep answers it a few minutes later unless Quo shows you did.
OFF_LIMITS = (EXCLUDED, UNCHECKED, NOTES_SAY_STOP)


def record_unread(lead) -> bool:
    """A CDK lead (it has a lead ID or record link) whose record has not been read: nothing goes to it."""
    lead = dict(lead)
    return bool(lead.get("opp_id") or lead.get("opp_url")) and not lead.get("notes_read")


def blocked(lead: dict, cfg: dict) -> str:
    """Why this lead must not get an automatic text, or ''."""
    if (excluded_rep(lead["salesperson"], cfg) or lead.get("record_excluded")) and not lead.get("mine"):
        # Fair game once nobody has logged contact for fair_game_days; with no date found, it stays theirs.
        cutoff = (date.today() - timedelta(days=cfg["fair_game_days"])).isoformat()
        if not lead.get("last_contact") or lead["last_contact"] > cutoff:
            return EXCLUDED
    if record_unread(lead):
        return UNCHECKED
    if guard.notes_say_stop(lead.get("cdk_notes")):
        return NOTES_SAY_STOP
    if (lead.get("person_at") or "") >= (datetime.now() - timedelta(days=cfg["person_days"])).isoformat(timespec="seconds"):
        return PERSON
    return ""


def record_outgoing(con, phone: str, text: str, message_id: str) -> str:
    """A text sent from the Quo number. Ours is already on file; one you sent from Quo yourself means you have
    taken over that conversation, and the bot stays out of it for `person_days`."""
    phone = db.normalize_phone(phone)
    if not phone:
        return "ignored"
    if message_id and con.execute("SELECT 1 FROM messages WHERE provider_id=?", (message_id,)).fetchone():
        return "ours"
    lead = con.execute("SELECT id FROM leads WHERE phone=?", (phone,)).fetchone()
    recent = (datetime.now() - timedelta(minutes=15)).isoformat(timespec="seconds")
    if lead and con.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='out' AND body=? AND created_at>=?",
                            (lead["id"], text, recent)).fetchone():
        return "ours"  # Quo's event can arrive before we have stored its message ID
    if lead:
        lead_id = lead["id"]
    else:  # you took this lead yourself; CDK's list confirms it on the PC's next pass
        lead_id = db.upsert_lead(con, phone, origin="you")
        db.log(con, lead_id, "started_by_you", "first text from you in Quo; waiting for CDK to list it")
    message_id_row = db.add_message(con, lead_id, "out", "manual", text, "sent")
    con.execute("UPDATE messages SET provider_id=?, sent_at=? WHERE id=?", (message_id or None, db.now(), message_id_row))
    con.execute("UPDATE leads SET person_at=? WHERE id=?", (db.now(), lead_id))
    con.execute("UPDATE messages SET needs_review=0 WHERE lead_id=?", (lead_id,))
    con.commit()
    return "manual"


def offer_due(con, lead_id: int, cfg: dict, today: date) -> bool:
    """On the offer_days cadence, counted from the first offer since they last texted us: offer n goes out
    offer_days[n] days after that first one, and after the last there are no more until they reply."""
    last_in = con.execute("SELECT max(created_at) FROM messages WHERE lead_id=? AND direction='in'", (lead_id,)).fetchone()[0]
    sent = [date.fromisoformat(row[0][:10]) for row in con.execute(
        "SELECT created_at FROM messages WHERE lead_id=? AND kind='promo' AND status IN ('queued','sent') AND created_at>?"
        " ORDER BY created_at", (lead_id, last_in or ""))]
    days = cfg["offer_days"]
    if not sent:
        return True
    if len(sent) >= len(days):
        return False
    return today >= sent[0] + timedelta(days=days[len(sent)]) and today > sent[-1]


def audience(con, cfg: dict, today: date) -> list[tuple[dict, str]]:
    """Return (lead, 'lease' | 'all') for everyone who should get a text today."""
    lead_cutoff = (today - timedelta(days=cfg["lead_window_days"])).isoformat()
    lease_from = (today - timedelta(days=cfg["lease_days_after"])).isoformat()
    lease_to = (today + timedelta(days=cfg["lease_days_before"])).isoformat()
    # The daily offer is for people who have gone quiet: anyone who replied in the last quiet_days is left to
    # the conversation, and anyone who said when to come back is left until then.
    replied = {row[0] for row in con.execute("SELECT DISTINCT lead_id FROM messages WHERE direction='in' AND created_at>=?",
                                             ((today - timedelta(days=cfg["quiet_days"])).isoformat(),))}
    selected = []
    for row in con.execute("SELECT * FROM leads WHERE status='active' ORDER BY id"):
        lead = dict(row)
        if blocked(lead, cfg) or lead["id"] in replied or (lead["snooze_until"] or "") > today.isoformat():
            continue
        if not offer_due(con, lead["id"], cfg, today):
            continue
        lease_due = (lead["lease_end"] and lease_from <= lead["lease_end"] <= lease_to
                     and lead["lease_still_owns"] != 0 and not lead["lease_renewed"])
        if lease_due:
            selected.append((lead, "lease"))
        elif lead["date_in"] and lead["date_in"] >= lead_cutoff:
            selected.append((lead, "all"))
    return selected


def check_ins_due(con, cfg: dict, now: datetime | None = None) -> list[dict]:
    """People in a conversation who went quiet on us: check-in n goes check_in_days[n] days after the first of our
    texts they left unanswered, and after the last one we wait. A lead with a plan of yours goes on its own pace
    instead: every plan_every days from plan_resume, for 45 days. A thread that ended on their "ok thanks" counts
    as quiet; one where they asked something is yours to answer. Threads you are running by hand get them too
    (the net for the ones you forget), but nothing goes within a day of anyone's last text, while snoozed, or
    off limits."""
    now = now or datetime.now()
    window = max(cfg["quiet_days"], cfg["check_in_days"][-1] + 1)  # long enough for the whole cadence
    since = (now - timedelta(days=window)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=1)).isoformat(timespec="seconds")
    due = []
    for row in con.execute(
            "SELECT l.* FROM leads l WHERE l.status='active' AND (l.plan_every IS NOT NULL"
            " OR EXISTS(SELECT 1 FROM messages WHERE lead_id=l.id AND direction='in' AND created_at>=?))", (since,)).fetchall():
        lead = dict(row)
        if blocked(lead, cfg) in (EXCLUDED, UNCHECKED, NOTES_SAY_STOP) or (lead["snooze_until"] or "") > now.date().isoformat():
            continue
        last = con.execute("SELECT * FROM messages WHERE lead_id=? AND status IN ('sent','received') ORDER BY id DESC LIMIT 1",
                           (lead["id"],)).fetchone()
        if not last:
            continue
        signed_off = last["direction"] == "in" and (guard.acknowledgement(last["body"]) or guard.reaction(last["body"]))
        if (last["direction"] != "out" and not signed_off) or (last["sent_at"] or last["created_at"]) > recent:
            continue  # they asked something (yours to answer), or someone texted in the last day
        if con.execute("SELECT 1 FROM messages WHERE lead_id=? AND kind='check_in' AND status='dismissed' AND id>? AND created_at>?",
                       (lead["id"], last["id"], recent)).fetchone():
            continue  # tried in the last day and had nothing safe to say
        last_at = datetime.fromisoformat(last["sent_at"] or last["created_at"])
        if lead["plan_every"]:
            start = datetime.fromisoformat(lead["plan_resume"] or last_at.date().isoformat())
            if start <= now < start + timedelta(days=46) and (last_at < start or last_at + timedelta(days=lead["plan_every"]) <= now):
                due.append(lead)
            continue
        # The last thing they asked or said, not a sign-off: our cadence counts from our first text after it.
        last_in = con.execute("SELECT max(id) FROM messages WHERE lead_id=? AND direction='in' AND id<?",
                              (lead["id"], last["id"] if signed_off else last["id"] + 1)).fetchone()[0]
        ignored = con.execute("SELECT coalesce(sent_at,created_at) FROM messages WHERE lead_id=? AND direction='out' AND status='sent'"
                              " AND id>? ORDER BY id LIMIT 1", (lead["id"], last_in or 0)).fetchone()
        unanswered = con.execute("SELECT count(*) FROM messages WHERE lead_id=? AND kind='check_in' AND status='sent' AND id>?",
                                 (lead["id"], last_in or 0)).fetchone()[0]
        days = cfg["check_in_days"]
        if not ignored or unanswered >= len(days):
            continue
        # On schedule from the text they went quiet on, and never sooner than its gap after the last text (no bursts).
        gap = days[unanswered] - (days[unanswered - 1] if unanswered else 0)
        if datetime.fromisoformat(ignored[0]) + timedelta(days=days[unanswered]) <= now and last_at + timedelta(days=gap) <= now:
            due.append(lead)
    return due


def send_check_ins(con, sender, cfg: dict, drafter=replies.draft_check_in, now: datetime | None = None) -> int:
    """A live conversation that went quiet gets a word from us on the check_in_days cadence."""
    sent = 0
    if credits_paused(con, now):
        return 0  # every draft is a Codex call, and Quo would refuse the text
    for lead in check_ins_due(con, cfg, now):
        history = thread(con, lead["id"])
        lead["workspace"], lead["scene"] = workspace(con, lead), scene(con, lead, cfg, now)
        lead["quote_step"] = quote_step(history)
        draft = drafter(lead, history, cfg)
        # A follow-up on your numbers may talk about the payment ("is it the payment or the timing?") and repeat
        # your figures, never new ones.
        if lead["quote_step"] is not None:
            problem = guard.check_decided_reply(draft, quoted_figures(history)) if draft else "nothing drafted"
        else:
            problem = guard.check_auto_reply(draft) if draft else "nothing drafted"
        if problem:
            # Nothing safe to say: mark it so the same thread is not drafted again every minute.
            db.add_message(con, lead["id"], "out", "check_in", draft or "", "dismissed", reason="no check-in drafted")
            con.commit()
            continue
        sent += deliver(con, sender, db.add_message(con, lead["id"], "out", "check_in", draft, "queued"))
    return sent


def signature_end(cfg: dict):
    """A trailing "- Jordan" or "- Jordan, Premier Ford"."""
    short = re.escape(cfg["store_short"] or cfg["store"])
    return re.compile(rf"\s*-\s*{re.escape(cfg['rep_name'])}(?:,\s*{short})?\s*$")


def vehicle_name(vehicle: str | None) -> str:
    """CDK's vehicle without its new/used flag: "2023 U Ford Explorer" -> "2023 Ford Explorer"."""
    return re.sub(r"^((?:19|20)\d{2}) [UN] ", r"\1 ", (vehicle or "").strip())


def render(template: str, lead: dict, cfg: dict, opening: bool = True) -> str:
    """An offer as sent. Your signature goes only on the text that opens a thread (and on last words, see
    signed()); in the middle of a thread it is dropped."""
    vehicle, leased = vehicle_name(lead["vehicle"]), vehicle_name(lead["lease_vehicle"])
    values = {"first": lead["first_name"] or "there", "vehicle": vehicle or "your next Ford",
              "the_vehicle": f"the {vehicle}" if vehicle else "a new Ford",
              "lease_vehicle": leased or vehicle or "your Ford",
              "rep": cfg["rep_name"], "store": cfg["store"]}
    if not lead["first_name"] and template.startswith("{first}, "):
        # No name on file: "there, want me to..." reads like a typo, so the text just starts with the question.
        template = template[len("{first}, "):][:1].upper() + template[len("{first}, ") + 1:]
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    text = " ".join(template.split())
    short = cfg["store_short"] or cfg["store"]
    if not opening:
        return signature_end(cfg).sub("", text)
    if short.lower() not in text.lower():  # name the business: "- Jordan" becomes "- Jordan, Premier Ford"
        sign = f"- {cfg['rep_name']}"
        text = f"{text}, {short}" if text.endswith(sign) else f"{text} {sign}, {short}"
    return text


def squash(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def pick_offer(con, lead: dict, kind: str):
    """The active offer this lead has gone longest without. Ties go to an offer about the
    vehicle they asked about (or lease), then to general offers. Offers about other models go only to leads
    who named no vehicle: an Explorer lease is not pitched a Transit van."""
    audiences = ("lease", "all") if kind == "lease" else ("all",)
    marks = ",".join("?" * len(audiences))
    rows = con.execute(
        "SELECT o.*, (SELECT max(m.id) FROM messages m WHERE m.lead_id=? AND m.offer_id=o.id"
        f" AND m.status IN ('queued','sent')) AS last_message FROM offers o WHERE o.active=1 AND o.audience IN ({marks})",
        (lead["id"], *audiences)).fetchall()
    wanted = squash(f"{lead['vehicle']} {lead['lease_vehicle'] if kind == 'lease' else ''}")

    # Only the closest model counts as theirs: a Bronco Sport shopper is not a Bronco shopper.
    theirs = max((squash(o["model"]) for o in rows if o["model"] and squash(o["model"]) in wanted), key=len, default=None)
    if vehicle_name(lead["vehicle"]) or (kind == "lease" and vehicle_name(lead["lease_vehicle"])):
        rows = [o for o in rows if not o["model"] or squash(o["model"]) == theirs]

    def rank(offer):
        model = squash(offer["model"])
        fit = -1 if model and model == theirs else (0 if not model else 1)
        return (offer["last_message"] or 0, offer["audience"] != kind, fit, offer["id"])

    return min(rows, key=rank) if rows else None


def queue_daily(con, cfg: dict, today: date | None = None) -> dict:
    """Queue one promo per eligible lead for today. Safe to run more than once a day."""
    today = today or date.today()
    counts = {"queued": 0, "already_today": 0, "no_offer": 0}
    for lead, kind in audience(con, cfg, today):
        # One try a day: a send Quo refused is not retried every minute (only "couldn't reach Quo" is).
        if con.execute("SELECT 1 FROM messages WHERE lead_id=? AND kind='promo' AND substr(created_at,1,10)=?"
                       " AND (status IN ('queued','sent') OR (status='failed' AND reason!='could not check the Quo thread'))",
                       (lead["id"], today.isoformat())).fetchone():
            counts["already_today"] += 1
            continue
        offer = pick_offer(con, lead, kind)
        if offer is None:
            counts["no_offer"] += 1
            continue
        opening = not con.execute("SELECT 1 FROM messages WHERE lead_id=? AND status IN ('sent','received')", (lead["id"],)).fetchone()
        db.add_message(con, lead["id"], "out", "promo", render(offer["body"], lead, cfg, opening), "queued", offer_id=offer["id"])
        con.execute("UPDATE offers SET last_used=? WHERE id=?", (today.isoformat(), offer["id"]))
        counts["queued"] += 1
    con.commit()
    return counts


def within_hours(cfg: dict, moment: datetime) -> bool:
    start, end = cfg["send_hours"]
    return start <= moment.hour < end


def store_plans(con, plans: dict, today: date | None = None) -> int:
    """Your plan per customer (phone -> {"goal", "resume", "every_days"}): what every reply and check-in follows.
    Until `resume` the lead gets nothing automatic; from then on, a check-in every `every_days` for 45 days. An
    empty goal clears the plan."""
    today = today or date.today()
    saved = 0
    for phone, plan in (plans or {}).items():
        phone = db.normalize_phone(phone)
        if not phone:
            continue
        lead_id = db.upsert_lead(con, phone)
        goal, resume = str(plan.get("goal") or "").strip(), plan.get("resume") or today.isoformat()
        every = int(plan.get("every_days") or 0) or None
        con.execute("UPDATE leads SET plan=?, plan_resume=?, plan_every=? WHERE id=?",
                    (goal or None, resume if goal else None, every if goal else None, lead_id))
        if goal and resume > today.isoformat():
            con.execute("UPDATE leads SET snooze_until=? WHERE id=? AND coalesce(snooze_until,'')<?", (resume, lead_id, resume))
        saved += 1
    con.commit()
    return saved


def pretty_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")[-10:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else phone


def person(con, lead_id: int) -> str:
    """A customer as you get them on your phone: full name, number, the CDK rep and the vehicle, one per line."""
    lead = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    last, _, first = (lead["name"] or "").partition(",")
    name = f"{first.strip() or lead['first_name'] or ''} {last.strip()}".strip().title() or "Name not in CDK"
    lines = [name, pretty_phone(lead["phone"])]
    if lead["salesperson"]:
        lines.append(f"Rep: {lead['salesperson']}")
    if vehicle_name(lead["vehicle"]):
        lines.append(vehicle_name(lead["vehicle"]))
    return "\n".join(lines)


def alert_rep(con, sender, lead_id: int, text: str) -> bool:
    """Text you (not the BDC) the moment a customer names a time to come in or asks for a call, so nothing booked
    by text goes unseen. Your phone comes from the PC's private settings."""
    phone = db.normalize_phone(db.setting(con, "rep_phone"))
    if not phone:
        return False
    try:
        sender.send(phone, f"Appointment/call\n\n{person(con, lead_id)}\nSaid: \"{text.strip()[:100]}\"\n\nCheck Quo.")
        return True
    except Exception as error:  # an alert failing must not stop the customer's reply
        print(f"rep alert failed: {error}", flush=True)
        return False


ENGAGED_REPLY_DAYS, SIGNAL_DAYS, FRESH_LEAD_DAYS = 14, 45, 3


def engaged_opps(con, today: date | None = None) -> list[str]:
    """CDK lead IDs whose records are worth reading again (reading takes ~20 seconds a lead): fresh in CDK (bought
    within days of inquiring, most buyers do), texted us lately, a buying signal in their texts, or a plan of yours.
    Never opted out, paused or sold. New leads are always read; that is the PC's side."""
    today = today or date.today()
    since = lambda days: (today - timedelta(days=days)).isoformat()
    engaged = set()
    for lead in con.execute("SELECT id, opp_id, date_in, plan FROM leads WHERE status='active' AND coalesce(opp_id,'')!=''"):
        if lead["plan"] or (lead["date_in"] or "") >= since(FRESH_LEAD_DAYS):
            engaged.add(lead["opp_id"])
            continue
        texts = [r["body"] for r in con.execute("SELECT body, created_at FROM messages WHERE lead_id=? AND direction='in'"
                                                " AND created_at>=? ORDER BY id DESC", (lead["id"], since(SIGNAL_DAYS)))]
        recent = con.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='in' AND created_at>=?",
                             (lead["id"], since(ENGAGED_REPLY_DAYS))).fetchone()
        if recent or any(guard.buying_signal(text) for text in texts):
            engaged.add(lead["opp_id"])
    return sorted(engaged)


def store_settings(con, settings: dict) -> dict:
    """From the PC's private config: your phone for alerts, and leads that are yours whatever CDK says."""
    if "rep_phone" in settings:
        con.execute("INSERT INTO settings(key,value) VALUES('rep_phone',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (db.normalize_phone(settings["rep_phone"] or ""),))
    if settings.get("credits") is not None:
        set_credits(con, float(settings["credits"]))
    if settings.get("credits_alert_below") is not None:
        con.execute("INSERT INTO settings(key,value) VALUES('credits_alert_below',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"{float(settings['credits_alert_below']):.2f}",))
    if "my_leads" not in settings:
        con.commit()
        return {"rep_phone": bool(db.setting(con, "rep_phone")), "credits_left": credits_left(con, config.DEFAULTS)}
    mine = [p for p in (db.normalize_phone(p) for p in settings.get("my_leads", [])) if p]
    con.execute(f"UPDATE leads SET mine=CASE WHEN phone IN ({','.join('?' * len(mine)) or 'NULL'}) THEN 1 ELSE NULL END", mine)
    con.commit()
    return {"rep_phone": bool(db.setting(con, "rep_phone")), "my_leads": len(mine)}


def revive_soft_nos(con, cfg: dict) -> int:
    """Leads paused for a "not yet" (before that counted as a break, not a no) go back on follow-up after a short
    break. Only when their last text was the soft no, nothing says they bought, and they never asked to stop."""
    revived = 0
    for lead in con.execute("SELECT id FROM leads WHERE status='paused'").fetchall():
        last_in = con.execute("SELECT body FROM messages WHERE lead_id=? AND direction='in' ORDER BY id DESC LIMIT 1",
                              (lead["id"],)).fetchone()
        text = last_in["body"] if last_in else ""
        if guard.soft_no(text) and not guard.bought_elsewhere(text) and not guard.bought_from_us(text):
            revived += con.execute("UPDATE leads SET status='active', snooze_until=?, updated_at=? WHERE id=? AND status='paused'",
                                   ((date.today() + timedelta(days=cfg["soft_no_days"])).isoformat(), db.now(), lead["id"])).rowcount
    con.commit()
    return revived


def apply_do_not_text(con, cfg: dict) -> int:
    """Numbers you said never to text again become opted out, whatever CDK or a reply says later."""
    phones = [p for p in (db.normalize_phone(p) for p in cfg["do_not_text"]) if p]
    for phone in phones:
        if not con.execute("SELECT 1 FROM leads WHERE phone=?", (phone,)).fetchone():
            db.upsert_lead(con, phone)
    changed = con.execute(f"UPDATE leads SET status='opted_out', updated_at=? WHERE status!='opted_out' AND phone IN "
                          f"({','.join('?' * len(phones)) or 'NULL'})", (db.now(), *phones)).rowcount
    con.execute("UPDATE messages SET status='dismissed', reason='do not text' WHERE status='queued' AND lead_id IN "
                f"(SELECT id FROM leads WHERE phone IN ({','.join('?' * len(phones)) or 'NULL'}))", phones)
    con.commit()
    return changed


OUT_OF_CREDITS = re.compile(r"Quo 402|prepaid credits", re.I)
SET_CREDITS = re.compile(r"(?i)^\s*credits?\s*:?\s*\$?\s*(\d+(?:\.\d{1,2})?)\s*$")


def segments(text: str) -> int:
    """SMS segments Quo bills for: 160 plain characters (153 each when split), 70 (67) once any other appears."""
    single, multi = (160, 153) if all(ord(ch) < 128 for ch in text or "") else (70, 67)
    length = len(text or "")
    return 1 if length <= single else -(-length // multi)


def credits_left(con, cfg: dict) -> float | None:
    """Quo has no balance API, so: the balance you gave at your last top-up, less every text sent through the
    API since (texts you type in Quo are on your plan). None until you have given one."""
    balance, since = db.setting(con, "credits_balance"), db.setting(con, "credits_at")
    if not balance:
        return None
    sent = con.execute("SELECT body FROM messages WHERE direction='out' AND status='sent' AND kind!='manual'"
                       " AND coalesce(sent_at, created_at)>=?", (since,)).fetchall()
    return float(balance) - sum(segments(r["body"]) for r in sent) * cfg["quo_segment_cost"]


def set_credits(con, balance: float) -> None:
    for key, value in (("credits_balance", f"{balance:.2f}"), ("credits_at", db.now()), ("credits_alerted", "")):
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()


def credits_paused(con, now: datetime | None = None) -> bool:
    """Quo refused a send for credits in the last 15 minutes and nothing has gone out or been topped up since:
    hold anything that costs a Codex call. After 15 minutes the next try is the test of whether they are back."""
    now = now or datetime.now()
    failed = con.execute("SELECT max(created_at) FROM messages WHERE status='failed' AND reason LIKE 'Quo 402%'").fetchone()[0]
    if not failed or failed < (now - timedelta(minutes=15)).isoformat(timespec="seconds"):
        return False
    since = con.execute("SELECT max(coalesce(sent_at, created_at)) FROM messages WHERE status='sent' AND kind!='manual'").fetchone()[0]
    return max(since or "", db.setting(con, "credits_at")) < failed


def watch_credits(con, sender, cfg: dict, texts: list[str]) -> bool:
    """Before a run: if these texts would take the balance under your line, text you, once per top-up."""
    left = credits_left(con, cfg)
    phone = db.normalize_phone(db.setting(con, "rep_phone"))
    if left is None or not phone or not texts or db.setting(con, "credits_alerted"):
        return False
    cost = sum(segments(text) for text in texts) * cfg["quo_segment_cost"]
    line = float(db.setting(con, "credits_alert_below") or cfg["credits_alert_below"])
    if left - cost >= line:
        return False
    try:
        sender.send(phone, f"Quo credits: about ${max(left, 0):.2f} left. The next run is {len(texts)} texts (about "
                           f"${cost:.2f}), which leaves about ${max(left - cost, 0):.2f}, under your ${line:.2f} line. "
                           f"Top up in Quo, then text me \"credits\" and the new balance, like \"credits 50\".")
    except Exception as error:  # the alert failing must not stop the run
        print(f"credit alert failed: {error}", flush=True)
        return False
    con.execute("INSERT INTO settings(key,value) VALUES('credits_alerted',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (db.now(),))
    con.commit()
    return True


BAD_NUMBER = re.compile(r"phone numbers? in the `?to`? array is invalid|not a valid (mobile|phone)|invalid (phone|to) number", re.I)


def deliver(con, sender, message_id: int) -> bool:
    """Send one queued message. The lead's status is rechecked at send time: nothing goes to someone who opted out,
    and offers and check-ins go only to active leads (a sold or paused customer who texts us still gets answered)."""
    row = con.execute("SELECT m.id,m.body,m.kind,l.phone,l.status,l.opp_id,l.opp_url,l.notes_read,l.cdk_notes FROM messages m"
                      " JOIN leads l ON l.id=m.lead_id WHERE m.id=? AND m.status='queued'", (message_id,)).fetchone()
    if row is None:
        return False
    if row["status"] == "opted_out" or (row["status"] != "active" and row["kind"] in ("promo", "check_in")):
        con.execute("UPDATE messages SET status='dismissed',reason='lead not active' WHERE id=?", (message_id,))
        con.commit()
        return False
    # The CDK record is rechecked too: never sent unread, or after the notes say they are out of market or done.
    why = UNCHECKED if record_unread(row) else NOTES_SAY_STOP if guard.notes_say_stop(row["cdk_notes"]) else ""
    if why and row["kind"] not in ("opt_out_confirm", "closing"):
        con.execute("UPDATE messages SET status='dismissed',reason=? WHERE id=?", (why, message_id))
        con.commit()
        return False
    if hasattr(sender, "last_message"):
        # Quo is the source of truth: if the newest text in the thread is one we did not send, you are in this
        # conversation. Record it, step back, and send nothing. If Quo cannot be asked, send nothing either.
        last = sender.last_message(row["phone"])
        if last is None:
            con.execute("UPDATE messages SET status='failed',reason='could not check the Quo thread' WHERE id=?", (message_id,))
            con.commit()
            return False
        if row["kind"] == "check_in" and last.get("direction") == "incoming":
            # They wrote last and we never saw it: they are waiting on you, not on a nudge.
            con.execute("UPDATE messages SET status='dismissed',reason='they texted last in Quo' WHERE id=?", (message_id,))
            con.commit()
            return False
        if last.get("direction") == "outgoing" and last.get("id") and \
                not con.execute("SELECT 1 FROM messages WHERE provider_id=?", (last["id"],)).fetchone():
            record_outgoing(con, row["phone"], last["text"], last["id"])
            con.execute("UPDATE messages SET status='dismissed',reason='you texted them in Quo' WHERE id=?", (message_id,))
            con.commit()
            return False
    try:
        provider_id = sender.send(row["phone"], row["body"])
    except SendError as error:
        con.execute("UPDATE messages SET status='failed',reason=? WHERE id=?", (str(error)[:300], message_id))
        if BAD_NUMBER.search(str(error)):  # Quo says the number can't take texts: stop trying it
            lead_id = con.execute("SELECT lead_id FROM messages WHERE id=?", (message_id,)).fetchone()[0]
            con.execute("UPDATE leads SET status='bad_number', updated_at=? WHERE id=? AND status='active'", (db.now(), lead_id))
            db.log(con, lead_id, "bad_number", "Quo refused the number as invalid; no more texts to it")
        con.commit()
        return False
    con.execute("UPDATE messages SET status='sent',provider_id=?,sent_at=? WHERE id=?", (provider_id, db.now(), message_id))
    con.commit()
    return True


def send_queued(con, sender, cfg: dict, moment: datetime | None = None, pause: float | None = None) -> dict:
    moment = moment or datetime.now()
    if not within_hours(cfg, moment):
        return {"sent": 0, "failed": 0, "skipped": "outside send hours"}
    pause = cfg["seconds_between_sends"] if pause is None else pause
    counts = {"sent": 0, "failed": 0}
    queued = con.execute("SELECT id, body FROM messages WHERE status='queued' ORDER BY id").fetchall()
    watch_credits(con, sender, cfg, [r["body"] for r in queued])
    ids = [r["id"] for r in queued]
    for index, message_id in enumerate(ids):
        counts["sent" if deliver(con, sender, message_id) else "failed"] += 1
        if pause and index < len(ids) - 1:
            time.sleep(pause)
    return counts


WANTS_MATCH = "wants a match"
OFFERED = re.compile(r"(?i)\bsend (?:you|it)\b|\boptions\b")


def offered_options(con, lead_id: int, before_id: int):
    """The last text we sent before this reply, if it was an offer to send vehicles."""
    last = con.execute("SELECT * FROM messages WHERE lead_id=? AND direction='out' AND status='sent' AND id<? ORDER BY id DESC LIMIT 1",
                       (lead_id, before_id)).fetchone()
    return last if last and last["kind"] == "promo" and OFFERED.search(last["body"]) else None


def lead_model(con, lead_id: int) -> tuple[str, int | None]:
    """(model, offer_id) the lead is talking about: the model of the last offer we sent them, else the
    stocked model that best matches the vehicle they asked about."""
    offer = con.execute("SELECT o.id, o.model FROM messages m JOIN offers o ON o.id=m.offer_id WHERE m.lead_id=?"
                        " AND m.direction='out' AND m.status='sent' ORDER BY m.id DESC LIMIT 1", (lead_id,)).fetchone()
    if offer and offer["model"]:
        return offer["model"], offer["id"]
    lead = con.execute("SELECT vehicle, lease_vehicle FROM leads WHERE id=?", (lead_id,)).fetchone()
    asked = squash(f"{lead['vehicle']} {lead['lease_vehicle'] or ''}")
    models = [row["model"] for row in con.execute("SELECT DISTINCT model FROM inventory")]
    best = max((m for m in models if m and squash(m) in asked), key=lambda m: len(squash(m)), default="")
    return best, offer["id"] if offer else None



def workspace(con, lead: dict) -> dict:
    """Files Codex reads before it answers this lead: the whole thread, the full CDK record (already scrubbed of
    personal details), your plan, and what is in stock (no prices: those are never texted)."""
    thread = "\n".join(f"{r['created_at'][:16]} {'Customer' if r['direction'] == 'in' else 'Us'}: {r['body']}" for r in con.execute(
        "SELECT direction, body, created_at FROM messages WHERE lead_id=? AND status IN ('sent','received') ORDER BY id",
        (lead["id"],)))
    stock = "year,model,trim,store,status,arrived,link\n" + "\n".join(
        ",".join(str(r[k] or "").replace(",", " ") for k in ("year", "model", "trim", "site", "status", "arrived", "link"))
        for r in con.execute("SELECT * FROM inventory ORDER BY model, year, trim"))
    files = {"thread.txt": thread or "(no texts yet)", "cdk_record.txt": lead.get("cdk_notes") or "(not read yet)",
             "inventory.csv": stock}
    if lead.get("plan"):
        files["plan.txt"] = f"Your plan for this customer (follow it):\n{lead['plan']}\n"
    return files


def thread(con, lead_id: int, limit: int = 40) -> list[dict]:
    """The last `limit` texts, oldest first, with when each went ("at") and its kind ("manual" is you, by hand)."""
    return [dict(r) for r in con.execute(
        "SELECT direction, body, kind, coalesce(sent_at, created_at) AS at FROM messages WHERE lead_id=?"
        " AND status IN ('sent','received') ORDER BY id DESC LIMIT ?", (lead_id, limit))][::-1]


def ago(then: datetime, now: datetime) -> str:
    minutes = max(0, int((now - then).total_seconds() // 60))
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size:
            n = minutes // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def opening_hours(cfg: dict, now: datetime) -> str:
    """Whether the store is open now and until when, or when it next opens: what "today" can mean."""
    def hour(value: float) -> str:
        whole = int(value)
        return f"{whole % 12 or 12}{':30' if value != whole else ''} {'PM' if whole >= 12 else 'AM'}"
    hours = cfg["sales_hours"]
    open_at, close_at = hours[now.weekday()]
    clock = now.hour + now.minute / 60
    if open_at <= clock < close_at:
        left = close_at - clock
        return (f"The store is open right now until {hour(close_at)} today "
                f"({int(left)} hours {int(left % 1 * 60)} minutes left).")
    if clock < open_at:
        return f"The store is closed right now and opens today at {hour(open_at)}."
    tomorrow = now + timedelta(days=1)
    return f"The store is closed for the day; it opens tomorrow ({tomorrow:%A}) at {hour(hours[tomorrow.weekday()][0])}."


def store_desk(con, desk: dict) -> dict:
    """The Desk Log as the PC just read it, from desk["from"] through desk["days"] days out: it replaces what was
    there for those days, matched to leads by phone."""
    start = desk["from"]
    end = (date.fromisoformat(start) + timedelta(days=desk["days"])).isoformat()
    con.execute("DELETE FROM desk_appointments WHERE day BETWEEN ? AND ?", (start, end))
    matched = 0
    for row in desk["rows"]:
        phones = [p for p in (db.normalize_phone(p) for p in row.get("phones", [])) if p]
        lead = next((r for p in phones for r in [con.execute("SELECT id FROM leads WHERE phone=?", (p,)).fetchone()] if r), None)
        matched += bool(lead)
        con.execute("INSERT INTO desk_appointments(day,time,name,salesperson,vehicle,phone,lead_id,confirmed,state,read_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["day"], row["time"], row["name"], row.get("salesperson", ""), row.get("vehicle", ""),
                     phones[0] if phones else None, lead["id"] if lead else None, int(bool(row.get("confirmed"))),
                     row.get("state", ""), db.now()))
    con.commit()
    return {"appointments": len(desk["rows"]), "matched_to_leads": matched}


def desk_bookings(con, lead_id: int | None = None, today: date | None = None) -> list[dict]:
    """Desk Log appointments from today on, for one lead or for everyone."""
    today = (today or date.today()).isoformat()
    query = "SELECT * FROM desk_appointments WHERE day>=?" + (" AND lead_id=?" if lead_id else "") + " ORDER BY day, id"
    return [dict(r) for r in con.execute(query, (today, lead_id) if lead_id else (today,))]


def booked_when(row: dict) -> str:
    return f"{date.fromisoformat(row['day']):%a %m/%d} {row['time']}"


def reply_rhythm(texts: list[dict]) -> float | None:
    """Minutes they usually take to answer a text of ours (the median), or None before two answers."""
    waits = sorted((datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])).total_seconds() / 60
                   for a, b in zip(texts, texts[1:]) if a["direction"] == "out" and b["direction"] == "in")
    return max(1.0, waits[len(waits) // 2]) if len(waits) >= 2 else None


def span(minutes: float) -> str:
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size:
            n = round(minutes / size)
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return "a minute"


def scene(con, lead: dict, cfg: dict, now: datetime | None = None) -> str:
    """Where things stand right now, the way a person would sense it: the time and day, whether the store is
    open, how long each side has been waiting and how fast they answered, and what you have already put in
    writing. A model given this context reads the situation; one without it reads only words."""
    now = now or datetime.now()
    texts = thread(con, lead["id"], 200)
    lines = [f"Right now it is {replies.stamp(now.isoformat())} in Brooklyn. {opening_hours(cfg, now)}"]
    last_in = next((i for i in range(len(texts) - 1, -1, -1) if texts[i]["direction"] == "in"), None)
    last_out = next((i for i in range(len(texts) - 1, -1, -1) if texts[i]["direction"] == "out"), None)
    if last_in is not None:
        at = datetime.fromisoformat(texts[last_in]["at"])
        line = f"Their last text came {replies.stamp(texts[last_in]['at'])} ({ago(at, now)})."
        before = next((t for t in reversed(texts[:last_in]) if t["direction"] == "out"), None)
        if before:
            line += f" They sent it {ago(datetime.fromisoformat(before['at']), at).replace(' ago', '')} after your text before it."
        lines.append(line)
    if last_out is not None:
        out = texts[last_out]
        lines.append(f"Your last text went {replies.stamp(out['at'])} ({ago(datetime.fromisoformat(out['at']), now)})"
                     + (", typed by you by hand" if out["kind"] == "manual" else "")
                     + ("; they have not answered it." if last_in is None or last_out > last_in else "."))
    for booking in desk_bookings(con, lead["id"], now.date()):
        lines.append(f"They are booked in CDK to come in {booked_when(booking)} with {booking['salesperson'] or 'us'}"
                     f" ({'confirmed' if booking['confirmed'] else 'not confirmed yet'}). Do not ask them for a time; "
                     "build on it.")
    rhythm = reply_rhythm(texts)
    if rhythm and last_out is not None and (last_in is None or last_out > last_in):
        waited = (now - datetime.fromisoformat(texts[last_out]["at"])).total_seconds() / 60
        lines.append(f"They usually answer within about {span(rhythm)}; this silence is {span(waited)}, "
                     f"{waited / rhythm:.0f} times their usual.")
    elif rhythm:
        lines.append(f"They usually answer within about {span(rhythm)}.")
    quotes = [t for t in texts if t["direction"] == "out" and guard.gave_numbers(t["body"])]
    if quotes:
        lines.append("You already gave them numbers in this thread. They stand exactly as written: never correct, "
                     "revise, walk back or apologize for them, and never give different ones. Build on them:\n"
                     + "\n".join(f"- {replies.stamp(t['at'])}: {t['body']}" for t in quotes[-4:]))
    return "\n".join(lines)


def quoted_figures(history: list[dict]) -> set[str]:
    """The figures you have already texted them: a reply may repeat these, never others."""
    return {f for t in history if t["direction"] == "out" and guard.gave_numbers(t["body"]) for f in guard.figures(t["body"])}


def quote_step(history: list[dict]) -> int | None:
    """After you send numbers and they go quiet, which follow-up this is (0, 1, 2), or None when it is not that."""
    marks = [i for i, t in enumerate(history) if t["direction"] == "out" and guard.gave_numbers(t["body"])]
    if not marks:
        return None
    since = history[marks[-1] + 1:]
    if any(t["direction"] == "in" and not (guard.acknowledgement(t["body"]) or guard.reaction(t["body"])) for t in since):
        return None  # they answered your numbers with more than an "ok": an ordinary check-in
    step = sum(1 for t in since if t.get("kind") == "check_in")
    return step if step < len(replies.QUOTED_STEPS) else None


def lead_facts(con, lead: dict, cfg: dict | None = None) -> str:
    """What Codex knows about a lead when it decides: never phone numbers or last names."""
    lines = [scene(con, lead, cfg) if cfg else f"Today is {date.today():%A, %B %d, %Y}.",
             f"Customer first name: {lead['first_name'] or 'unknown'}.",
             f"Vehicle they asked about: {lead['vehicle'] or 'unknown'}."]
    if lead["lease_end"]:
        lines.append(f"Their lease on a {lead['lease_vehicle'] or 'Ford'} ends {lead['lease_end']}.")
    if lead["status"] == "sold":
        lines.append(f"CDK shows they bought from us{' on ' + lead['sold_on'] if lead['sold_on'] else ''}.")
    if lead.get("plan"):
        lines.append(f"Your plan for this customer (follow it): {lead['plan']}")
    if lead["cdk_notes"]:
        lines.append("Their CDK record, personal details removed (all of it is in cdk_record.txt):\n" + lead["cdk_notes"][-2500:])
    return "\n".join(lines)


HOLDING = ("\n\nNobody has answered their last text for hours. Do not hand off again unless they are upset or "
           "complaining: write a short holding reply in your own voice that keeps them warm and moves toward a visit "
           "or a call, with no numbers (no prices, payments, trade values or rates). For a trade: values depend on "
           "miles and condition, so you would want to see it in person.")


def think(con, sender, cfg: dict, lead_id: int, inbound_id: int, decide=None, holding: bool = False) -> str:
    """Codex reads the thread and the lead's facts and decides; code checks and carries it out. Anything it
    will not or cannot say well is left for you in Quo."""
    def mark(why: str, review: int, outcome: str) -> str:
        con.execute("UPDATE messages SET needs_review=?,reason=? WHERE id=?", (review, why[:120], inbound_id))
        con.commit()
        return outcome

    lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    history = thread(con, lead_id)
    lead["workspace"] = workspace(con, lead)
    decision = (decide or replies.decide)(lead, history, lead_facts(con, lead, cfg) + (HOLDING if holding else ""), cfg)
    if not decision:
        return mark("decided: no decision, yours to answer", 1, "review")
    if decision["snooze_days"]:
        con.execute("UPDATE leads SET snooze_until=? WHERE id=?",
                    ((date.today() + timedelta(days=decision["snooze_days"])).isoformat(), lead_id))
    outcome = "review"
    if decision["action"] == "match":
        outcome = mark(WANTS_MATCH, 1, "match")  # the matcher sends one vehicle within a minute
    elif decision["action"] == "none":
        outcome = mark(f"decided: nothing to say ({decision['why']})", 0, "noted")
    elif decision["action"] == "reply":
        text = guard.plain_voice(decision["text"])
        problem = guard.check_decided_reply(text, quoted_figures(history)) or ("names you" if replies.names_rep(text, cfg) else "")
        if problem:
            outcome = mark(f"decided: yours to answer ({problem})", 1, "review")
        elif deliver(con, sender, db.add_message(con, lead_id, "out", "decided",
                                                 signed(text, cfg) if decision["status"] else text, "queued")):  # a goodbye
            outcome = mark(f"decided: replied ({decision['why']})", 0, "replied")
        else:
            outcome = mark("decided: reply failed to send", 1, "review")
    else:
        outcome = mark(f"decided: yours to answer ({decision['why']})", 1, "review")
    if decision["status"]:  # after any reply, so "Will do" still reaches someone who asked to be removed
        con.execute("UPDATE leads SET status=?,updated_at=? WHERE id=? AND status!='opted_out'", (decision["status"], db.now(), lead_id))
        con.commit()
    return outcome


def sweep_unanswered(con, sender, cfg: dict, decide=None, now: datetime | None = None, limit: int = 5) -> list[str]:
    """Every customer whose last text has sat unanswered (from a few minutes to three days) gets a decision, so
    nothing waits on you noticing it. Excluded reps' leads, threads you are running, and anything already decided
    are left alone."""
    now = now or datetime.now()
    if credits_paused(con, now):
        return []
    # A reply Quo refused for credits: once texts can go out, their text is decided again, fresh (what would
    # have fit at 10am may not at 2pm), unless anyone has texted them since.
    con.execute(
        "UPDATE messages SET reason='', needs_review=0 WHERE direction='in' AND status='received' AND created_at>=?"
        " AND reason IN ('decided: reply failed to send', 'automatic reply failed to send')"
        " AND id=(SELECT max(id) FROM messages m2 WHERE m2.lead_id=messages.lead_id AND m2.status IN ('sent','received'))"
        " AND EXISTS(SELECT 1 FROM messages f WHERE f.lead_id=messages.lead_id AND f.id>messages.id"
        " AND f.status='failed' AND f.reason LIKE 'Quo 402%')", ((now - timedelta(days=3)).isoformat(timespec="seconds"),))
    con.commit()
    rows = con.execute(
        "SELECT m.id, m.lead_id, m.body FROM messages m WHERE m.direction='in' AND m.status='received'"
        " AND m.created_at BETWEEN ? AND ?"
        " AND m.id=(SELECT max(id) FROM messages WHERE lead_id=m.lead_id AND status IN ('sent','received'))"
        " AND m.reason NOT LIKE 'decided%' AND m.reason NOT IN ('complaint or legal', 'asked to stop', ?, 'vehicle sent: follow up')"
        " ORDER BY m.id", ((now - timedelta(days=3)).isoformat(timespec="seconds"),
                           (now - timedelta(minutes=3)).isoformat(timespec="seconds"), WANTS_MATCH)).fetchall()
    done = []
    for row in rows:
        if len(done) >= limit:
            break
        lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone())
        if lead["status"] == "opted_out" or blocked(lead, cfg) in OFF_LIMITS or guard.reaction(row["body"]):
            con.execute("UPDATE messages SET reason=? WHERE id=?", ("decided: left alone", row["id"]))
            con.commit()
            continue
        done.append(f"{row['id']}: {think(con, sender, cfg, row['lead_id'], row['id'], decide)}")
    # The net: a text handed to you (or left alone) that is still the last word after hanging_hours gets a holding
    # reply, so nobody is left waiting because you did not see it. Complaints and opt-outs never.
    if len(done) < limit:
        hanging = con.execute(
            "SELECT m.id, m.lead_id, m.body FROM messages m WHERE m.direction='in' AND m.status='received'"
            " AND m.created_at BETWEEN ? AND ?"
            " AND m.id=(SELECT max(id) FROM messages WHERE lead_id=m.lead_id AND status IN ('sent','received'))"
            " AND (m.reason LIKE 'decided: yours to answer%' OR m.reason LIKE 'decided: no decision%'"
            " OR m.reason='decided: left alone' OR (m.needs_review=1 AND m.reason NOT LIKE 'decided%'))"
            " AND m.reason NOT IN ('complaint or legal', 'asked to stop', ?, 'vehicle sent: follow up') ORDER BY m.id",
            ((now - timedelta(days=3)).isoformat(timespec="seconds"),
             (now - timedelta(hours=cfg["hanging_hours"])).isoformat(timespec="seconds"), WANTS_MATCH)).fetchall()
        for row in hanging:
            if len(done) >= limit:
                break
            lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (row["lead_id"],)).fetchone())
            if lead["status"] == "opted_out" or blocked(lead, cfg) in OFF_LIMITS or guard.reaction(row["body"]) \
                    or "complain" in (row["body"] or "").lower():
                con.execute("UPDATE messages SET reason='decided: held for you: left alone' WHERE id=?", (row["id"],))
                con.commit()
                continue
            outcome = think(con, sender, cfg, row["lead_id"], row["id"], decide, holding=True)
            # Tried once: whatever it decided, it is not tried again.
            con.execute("UPDATE messages SET reason='decided: held for you: ' || coalesce(reason,'') WHERE id=?"
                        " AND (reason LIKE 'decided: yours%' OR reason LIKE 'decided: no decision%')", (row["id"],))
            con.commit()
            done.append(f"{row['id']}: holding {outcome}")
    return done


def engaged_quiet(con, cfg: dict, now: datetime) -> list[dict]:
    """Customers who really talked to us in the last quiet_days (more than "ok", "no" or a stop) and are active,
    texteable and quiet now: our text was the last word, or their sign-off was, at least 20 hours ago."""
    since = (now - timedelta(days=cfg["quiet_days"])).isoformat(timespec="seconds")
    recent = (now - timedelta(hours=20)).isoformat(timespec="seconds")
    found = []
    for row in con.execute("SELECT l.* FROM leads l WHERE l.status='active' AND EXISTS(SELECT 1 FROM messages"
                           " WHERE lead_id=l.id AND direction='in' AND created_at>=?)", (since,)).fetchall():
        lead = dict(row)
        if blocked(lead, cfg) in OFF_LIMITS or (lead["snooze_until"] or "") > now.date().isoformat():
            continue
        history = thread(con, lead["id"])
        said = [t["body"] for t in history if t["direction"] == "in"]
        if not any(len(body.split()) > 2 and not guard.acknowledgement(body) and not guard.reaction(body) for body in said):
            continue  # only "ok", "no", a thumbs-up: not a conversation
        last = history[-1]
        if last["direction"] == "in" and not (guard.acknowledgement(last["body"]) or guard.reaction(last["body"])):
            continue  # their question: the sweep answers it
        if last["at"] > recent:
            continue
        lead["history"] = history
        found.append(lead)
    return found


def moments(con, sender, cfg: dict, decide=None, now: datetime | None = None, limit: int = 3) -> list[str]:
    """The cadence is a floor, not a script: a quiet, engaged customer whose check-in is not due is looked at
    every moment_hours anyway, and texted when a person would see that now is the moment (the time they gave
    has come, a fresh start, silence past their own rhythm). Never in the store's first or last hour."""
    now = now or datetime.now()
    open_at, close_at = cfg["sales_hours"][now.weekday()]
    clock = now.hour + now.minute / 60
    if not within_hours(cfg, now) or not open_at + 1 <= clock <= close_at - 1 or credits_paused(con, now) \
            or replies.limited(now):
        return []  # replies to customers come first on the ChatGPT plan's usage
    today = now.date().isoformat()
    spent = con.execute("SELECT count(*) FROM audit WHERE kind='moment' AND substr(created_at,1,10)=?", (today,)).fetchone()[0]
    limit = min(limit, cfg["moments_per_day"] - spent)
    if limit <= 0:
        return []
    light = dict(cfg, codex_effort=cfg["moment_effort"])  # a judgment call per customer, not a deep read
    due = {lead["id"] for lead in check_ins_due(con, cfg, now)}
    looked = (now - timedelta(hours=cfg["moment_hours"])).isoformat(timespec="seconds")
    texted = (now - timedelta(days=3)).isoformat(timespec="seconds")
    done = []
    for lead in engaged_quiet(con, cfg, now):
        if len(done) >= limit:
            break
        if lead["id"] in due or con.execute(
                "SELECT 1 FROM audit WHERE lead_id=? AND kind='moment' AND created_at>=?", (lead["id"], looked)).fetchone() \
                or con.execute("SELECT 1 FROM messages WHERE lead_id=? AND kind='moment' AND status='sent' AND created_at>=?",
                               (lead["id"], texted)).fetchone():
            continue
        history = lead.pop("history")
        lead["workspace"] = workspace(con, lead)
        decision = (decide or replies.decide)(lead, history, lead_facts(con, lead, cfg) + replies.MOMENT, light)
        if decision is None:
            break  # Codex gave nothing (the plan's limit, a timeout): look again later, not tomorrow
        why = decision["why"]
        outcome = "waited"
        if decision and decision["action"] == "reply":
            text = guard.plain_voice(decision["text"])
            problem = guard.check_decided_reply(text, quoted_figures(history)) or ("names you" if replies.names_rep(text, cfg) else "")
            if problem:
                why, outcome = f"held: {problem}", "held"
            elif deliver(con, sender, db.add_message(con, lead["id"], "out", "moment", text, "queued")):
                outcome = "texted"
            else:
                outcome = "not sent"
        db.log(con, lead["id"], "moment", f"{outcome}: {why}")
        con.commit()
        done.append(f"{lead['id']}: {outcome} ({why})")
    return done


def alert_ownership(con, sender, cfg: dict) -> int:
    """Text you, once, when CDK shows someone else stepping in on a customer you are talking to (a salesperson
    added, the BDC setting an appointment): the BDC does not see your texts, so this is your cue to log your
    contact in CDK or talk to the desk before the customer is handed off."""
    phone = db.normalize_phone(db.setting(con, "rep_phone"))
    if not phone:
        return 0
    rows = con.execute("SELECT a.id, a.lead_id, a.detail FROM audit a WHERE a.kind='ownership' AND NOT EXISTS("
                       "SELECT 1 FROM audit t WHERE t.kind='ownership_texted' AND t.detail=CAST(a.id AS TEXT)) ORDER BY a.id").fetchall()
    sent = 0
    for lead_id in dict.fromkeys(r["lead_id"] for r in rows):
        mine = [r for r in rows if r["lead_id"] == lead_id]
        try:
            sender.send(phone, "Heads up: CDK moved on your customer\n\n" + person(con, lead_id) + "\n\n"
                        + "\n".join(r["detail"] for r in mine)
                        + "\n\nThe BDC doesn't see your texts. Log your contact in CDK or talk to the desk.")
        except Exception as error:  # an alert failing must not stop the loop; it is tried again next minute
            print(f"ownership alert failed: {error}", flush=True)
            continue
        for r in mine:
            con.execute("INSERT INTO audit(lead_id,kind,detail,created_at) VALUES(?,?,?,?)",
                        (lead_id, "ownership_texted", str(r["id"]), db.now()))
        con.commit()
        sent += 1
    return sent


def unclaimed(con, cfg: dict, now: datetime, limit: int = 6) -> list[tuple[int, str]]:
    """Warm conversations of yours that CDK knows nothing about: they answered with substance in the last 14 days,
    the record has been read, no excluded rep is on it, and the record shows no contact logged since the
    conversation began (or, when you have set my_cdk_names, never names you). (lead_id, note), newest first."""
    since = (now - timedelta(days=14)).isoformat(timespec="seconds")
    found = []
    for lead in con.execute("SELECT l.* FROM leads l WHERE l.status='active' AND l.notes_read IS NOT NULL AND EXISTS("
                            "SELECT 1 FROM messages WHERE lead_id=l.id AND direction='in' AND created_at>=?)", (since,)).fetchall():
        lead = dict(lead)
        if blocked(lead, cfg) in OFF_LIMITS:
            continue
        said = [t for t in thread(con, lead["id"], 60) if t["direction"] == "in" and t["at"] >= since]
        warm = [t for t in said if len(t["body"].split()) > 2 and not guard.acknowledgement(t["body"]) and not guard.declined(t["body"])]
        if not warm:
            continue
        began = warm[0]["at"][:10]
        names = [n for n in cfg["my_cdk_names"] if n]
        if names:
            if any(n.lower() in (lead["cdk_notes"] or "").lower() for n in names):
                continue
        elif lead["last_contact"] and lead["last_contact"] >= began:
            continue
        found.append((warm[-1]["at"], lead["id"], f"Texting since {date.fromisoformat(began):%m/%d}; "
                      + ("CDK never names you." if names else "CDK shows no contact logged since then.")
                      + " Log it so the BDC doesn't hand them off."))
    return [(lead_id, note) for _, lead_id, note in sorted(found, reverse=True)[:limit]]


def report_input(con, cfg: dict, now: datetime) -> tuple[str, dict]:
    """What the morning report reads: everyone who texted us in the last 14 days and is not opted out, by id
    (Codex never sees last names or numbers; the report adds them back)."""
    since = (now - timedelta(days=14)).isoformat(timespec="seconds")
    blocks, ids = [], {}
    for row in con.execute("SELECT l.* FROM leads l WHERE l.status NOT IN ('opted_out','bad_number') AND EXISTS(SELECT 1"
                           " FROM messages WHERE lead_id=l.id AND direction='in' AND created_at>=?)", (since,)).fetchall():
        lead = dict(row)
        if blocked(lead, cfg) == EXCLUDED:
            continue
        ids[lead["id"]] = lead
        lines = [f"<customer id=\"{lead['id']}\" first_name=\"{lead['first_name'] or 'unknown'}\" status=\"{lead['status']}\""
                 f" vehicle=\"{vehicle_name(lead['vehicle']) or 'unknown'}\">", scene(con, lead, cfg, now)]
        lines += [replies.said(t, cfg) for t in thread(con, lead["id"], 12)]
        blocks.append("\n".join(lines) + "\n</customer>")
    return "\n\n".join(blocks), ids


def send_report(con, sender, cfg: dict, report=None, now: datetime | None = None) -> bool:
    """Once a day after report_hour: a text to you with today's appointments and who needs a push, each with
    their full name, number and CDK rep, spaced out to read on a phone."""
    now = now or datetime.now()
    phone = db.normalize_phone(db.setting(con, "rep_phone"))
    if not phone or now.hour < cfg["report_hour"] or not within_hours(cfg, now) \
            or db.setting(con, "report_sent") == now.date().isoformat() or credits_paused(con, now) or replies.limited(now):
        return False
    leads, ids = report_input(con, cfg, now)
    found = (report or replies.daily_report)(leads, cfg) if ids else {"appointments": [], "push": []}
    if found is None:
        return False
    # The Desk Log first: what CDK says is booked, whoever booked it, through desk_log_days.
    desk, booked = [], set()
    for row in desk_bookings(con, today=now.date()):
        booked.add(row["lead_id"])
        who = person(con, row["lead_id"]) if row["lead_id"] else "\n".join(
            [" ".join(reversed([part.strip() for part in row["name"].split(",", 1)])).title(),
             pretty_phone(row["phone"] or "") or "No number on the Desk Log", f"Rep: {row['salesperson']}"]
            + ([row["vehicle"]] if row["vehicle"] else []))
        desk.append(f"{who}\n{booked_when(row)}, {'confirmed' if row['confirmed'] else 'not confirmed'} in CDK")
    sections = [["BOOKED IN CDK"] + desk] if desk else []
    for title, items in (("CONFIRMED BY TEXT", [a for a in found.get("appointments", []) if a.get("status") == "confirmed"]),
                         ("LOOSE", [a for a in found.get("appointments", []) if a.get("status") == "loose"]),
                         ("NEEDS A PUSH", found.get("push", [])),
                         ("CLAIM IT IN CDK", [{"lead": lead_id, "note": note} for lead_id, note in unclaimed(con, cfg, now)])):
        entries = [f"{person(con, item['lead'])}\n{item['note'].strip()}" for item in items
                   if (item.get("lead") in ids or title == "CLAIM IT IN CDK")
                   and not (title not in ("NEEDS A PUSH", "CLAIM IT IN CDK") and item["lead"] in booked)]
        if entries:
            sections.append([title] + entries)
    texts, current = [], f"Daily report, {now:%a %m/%d}"
    for section in sections or [["Nothing needs you today."]]:
        for index, entry in enumerate(section):
            piece = ("\n\n\n" if index == 0 else "\n\n") + entry
            if len(current) + len(piece) > cfg["report_text_limit"]:  # the next text picks up where this one ends
                texts.append(current)
                current = f"{section[0]} (cont.)\n\n{entry}" if index else entry
                continue
            current += piece
    texts.append(current)
    try:
        for text in texts:
            sender.send(phone, text)
    except Exception as error:  # the report failing must not stop texting
        print(f"daily report failed: {error}", flush=True)
        return False
    con.execute("INSERT INTO settings(key,value) VALUES('report_sent',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now.date().isoformat(),))
    con.commit()
    return True


def signed(text: str, cfg: dict) -> str:
    """A last word ends with who and where, for someone who finds it later without the number saved."""
    signoff = cfg["final_signoff"].replace("{rep}", cfg["rep_name"])
    return text if not signoff or signoff in text else f"{text.rstrip()} {signoff}"


def close_loop(con, sender, cfg: dict, lead_id: int, asked_to_stop: bool, ours: bool = False) -> bool:
    """They bought: one short, warm close in the rep's own words, at most once per lead. From us (CDK has them as
    sold, or they said so) it is a congratulations on their new car; elsewhere, an open door back to Ford."""
    lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    if blocked(lead, cfg) in OFF_LIMITS or \
            con.execute("SELECT 1 FROM messages WHERE lead_id=? AND kind='closing'", (lead_id,)).fetchone():
        return False
    if ours:
        car = re.sub(r"^\s*(?:19|20)\d{2}\s+|\bFord\s+", "", lead["vehicle"] or "").strip()
        body = cfg["bought_here_reply"].replace("{vehicle}", car or "car")
        return deliver(con, sender, db.add_message(con, lead_id, "out", "closing", signed(body, cfg), "queued"))
    body = cfg["bought_elsewhere_reply"].replace("{rep}", cfg["rep_name"])
    if asked_to_stop:
        body = "Will do, " + body[:1].lower() + body[1:]
    return deliver(con, sender, db.add_message(con, lead_id, "out", "closing", signed(body, cfg), "queued"))


def handle_inbound(con, sender, cfg: dict, phone: str, text: str, drafter=replies.draft_reply, message_id: str = "",
                   courtesy=replies.draft_courtesy, decide=None) -> str:
    """Record an inbound text and act on it.
    Returns staff | duplicate | opt_out | declined | match | noted | closed | review | replied."""
    phone = db.normalize_phone(phone)
    if not phone:
        return "ignored"
    mine = phone == db.normalize_phone(db.setting(con, "rep_phone"))
    if mine and SET_CREDITS.match(text or ""):
        # You, after a top-up: "credits 50" sets the balance the watcher counts down from.
        set_credits(con, float(SET_CREDITS.match(text)[1]))
        try:
            sender.send(phone, f"Got it: ${float(SET_CREDITS.match(text)[1]):.2f} in Quo credits.")
        except Exception as error:
            print(f"credit confirm failed: {error}", flush=True)
        return "staff"
    if bdc.is_staff(phone, cfg) or mine:
        return "staff"  # the BDC replying to an update, or you: yours to read in Quo, never a lead
    if message_id and con.execute("SELECT 1 FROM messages WHERE provider_id=?", (message_id,)).fetchone():
        return "duplicate"  # the provider sent the same text again
    lead_id = db.upsert_lead(con, phone)
    verdict, reason = guard.classify_inbound(text)
    bought = guard.bought_elsewhere(text) or guard.bought_from_us(text)
    # From us if CDK has them in a sold folder or they say so ("got it from you guys"); otherwise elsewhere.
    bought_here = bought and (guard.bought_from_us(text) or
                              con.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()["status"] == "sold")
    if verdict == "opt_out":
        db.add_message(con, lead_id, "in", "inbound", text, "received", reason=reason, provider_id=message_id)
        if bought:
            bdc.record(con, lead_id, "sold" if bought_here else "bought_elsewhere")
        if bought:  # sent before the opt-out takes effect: "Will do, congratulations! ..."
            close_loop(con, sender, cfg, lead_id, asked_to_stop=True, ours=bought_here)
        elif not con.execute("SELECT 1 FROM messages WHERE lead_id=? AND kind='opt_out_confirm'", (lead_id,)).fetchone():
            # Never leave a "please stop" unanswered: one plain confirmation, then nothing.
            body = cfg["wrong_number_reply"] if guard.WRONG_NUMBER.search(text) else cfg["opt_out_reply"]
            deliver(con, sender, db.add_message(con, lead_id, "out", "opt_out_confirm", signed(body, cfg), "queued"))
        con.execute("UPDATE leads SET status='opted_out',updated_at=? WHERE id=?", (db.now(), lead_id))
        con.execute("UPDATE messages SET status='dismissed',reason='opted out' WHERE lead_id=? AND status IN ('queued','held')", (lead_id,))
        con.commit()
        return "opt_out"
    # Waiting on a person already: the bot stays out of that conversation.
    waiting = con.execute("SELECT 1 FROM messages WHERE lead_id=? AND direction='in' AND needs_review=1", (lead_id,)).fetchone()
    contact = guard.wants_contact(text)
    inbound_id = db.add_message(con, lead_id, "in", "inbound", text, "received", provider_id=message_id)
    # What the BDC should hear about.
    if bought:
        bdc.record(con, lead_id, "sold" if bought_here else "bought_elsewhere")
    elif contact:
        bdc.record(con, lead_id, "appointment", f'"{text.strip()[:60]}"')
        alert_rep(con, sender, lead_id, text)
    elif reason == "financing or credit" or guard.FINANCE_TALK.search(text):
        bdc.record(con, lead_id, "financing", f'"{text.strip()[:60]}"')
    later = guard.later_days(text)
    if later:  # "get in touch with me in a year": no offers until then
        con.execute("UPDATE leads SET snooze_until=? WHERE id=?", ((date.today() + timedelta(days=later)).isoformat(), lead_id))
    declined = (guard.declined(text) or bought) and not contact
    if not bought and not contact and guard.soft_no(text):
        # "Not yet" / "not at this moment": a break, then follow-up again; never a pause for good.
        declined = False
        if not later:
            con.execute("UPDATE leads SET snooze_until=? WHERE id=?",
                        ((date.today() + timedelta(days=cfg["soft_no_days"])).isoformat(), lead_id))
    if declined:
        if bought:
            close_loop(con, sender, cfg, lead_id, asked_to_stop=False, ours=bought_here)
        con.execute("UPDATE leads SET status=?,updated_at=? WHERE id=? AND status='active'",
                    ("sold" if bought_here else "paused", db.now(), lead_id))
        con.execute("UPDATE messages SET status='dismissed',reason='lead declined' WHERE lead_id=? AND status IN ('queued','held')", (lead_id,))
    con.commit()

    def flag(why: str, outcome: str = "review") -> str:
        con.execute("UPDATE messages SET needs_review=1,reason=? WHERE id=?", (why, inbound_id))
        con.commit()
        return outcome

    why = blocked(dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()), cfg)
    if why in OFF_LIMITS:
        return flag(why)  # never answered automatically: yours in Quo
    if why == PERSON:
        return flag(why, "later")  # your thread: the sweep answers in a few minutes unless you already did
    if declined:  # texts paused (and a purchase closed above); a "no" needs no answer
        return "declined"
    if verdict == "review" and reason == "complaint or legal":
        return flag(reason)
    if verdict == "review" or waiting:
        # Money, credit, trade or stock, or a thread already waiting: Codex reads all of it and decides what is
        # safe to say, and anything only you can answer stays with you.
        return think(con, sender, cfg, lead_id, inbound_id, decide)
    if guard.affirmative(text) and offered_options(con, lead_id, inbound_id):
        return flag(WANTS_MATCH, "match")  # the PC reads their CDK record and sends one vehicle
    if guard.reaction(text):
        return "noted"  # a thumbs-up on our text
    if guard.acknowledgement(text):
        # "Thanks": they get a short, warm last word, unless that is what we just sent.
        last_out = con.execute("SELECT kind FROM messages WHERE lead_id=? AND direction='out' AND status='sent' ORDER BY id DESC LIMIT 1",
                               (lead_id,)).fetchone()
        status = con.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()["status"]
        if last_out and (last_out["kind"] in ("courtesy", "closing") or status != "active"):
            return "noted"  # they already got our last word: a second goodbye is one too many
        lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        history = thread(con, lead_id, 12)
        lead["scene"] = scene(con, lead, cfg)
        deliver(con, sender, db.add_message(con, lead_id, "out", "courtesy", signed(courtesy(lead, history, cfg), cfg), "queued"))
        return "closed"
    if not cfg["auto_reply"]:
        return flag("auto-reply is off")
    lead = dict(con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
    history = thread(con, lead_id, 12)
    lead["scene"] = scene(con, lead, cfg)
    draft = drafter(lead, history, cfg)
    if not draft or guard.check_auto_reply(draft):
        return think(con, sender, cfg, lead_id, inbound_id, decide)  # the quick draft would not do: decide properly
    message_id = db.add_message(con, lead_id, "out", "auto", draft, "queued")
    con.commit()
    if not deliver(con, sender, message_id):
        return flag("automatic reply failed to send")
    return flag("asked for a call or visit", "replied") if contact else "replied"
