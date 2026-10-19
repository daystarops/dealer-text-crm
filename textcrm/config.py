"""Settings: defaults here, local overrides in private/textcrm.json (git-ignored)."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "private" / "textcrm.json"

DEFAULTS = {
    "store": "Premier Ford of Bay Ridge",
    "rep_name": "Jordan",
    "store_short": "Premier Ford",  # every promo names the business, as carriers expect; kept short for one segment
    "quiet_days": 45,               # the daily offer only goes to people who have not replied for this long
    # Offers to someone who stays silent: these days after the first one, at least two days apart (stops ran
    # 3.4% at one-day gaps, 10/15-10/17), then nothing more until they reply.
    "offer_days": [0, 2, 4, 6, 9, 12, 15, 18, 22, 26, 30, 35, 40, 45],
    # Someone who replied, then went quiet on our last text: a check-in this many days after that text, one per
    # entry, for 45 days, then we wait for them. Starts at 24 hours; two nudges beat one (AJMC 2018 RCT).
    "check_in_days": [1, 2, 3, 5, 7, 9, 12, 15, 19, 23, 27, 31, 35, 40, 45],
    "soft_no_days": 7,              # "not yet" / "not right now": follow-up resumes after this many days
    "person_days": 7,               # after you text someone from Quo yourself, the bot leaves them alone this long
    # Sent once to anyone who says they bought elsewhere; "Will do, " goes in front if they also asked to stop.
    # Sent once to a customer who bought from us and says so (or CDK has them as sold).
    "bought_here_reply": "Congrats on the new {vehicle}! Enjoy it, and if you ever need anything, just text me here.",
    # The one text after someone asks us to stop (or says wrong number); nothing goes to them after it.
    "opt_out_reply": "Understood, I'll stop texting you. Whenever you're ready, just text me here.",
    "wrong_number_reply": "Sorry about that, I'll take this number off my list.",
    "bought_elsewhere_reply": "Congratulations! If you ever reconsider Ford, you can reach out to me at this number.",
    # Added to every last word (thanks, goodbyes, congratulations), so someone who finds it later, without your
    # number saved, knows who and where.
    "final_signoff": "- {rep}, Premier Ford of Bay Ridge, 612 86th St, Brooklyn",
    # Facts the auto-responder may state. Anything not listed here goes to review.
    "store_facts": (
        "Premier Ford of Bay Ridge: 612 86th Street, Brooklyn, NY 11228. Phone (347) 578-7900.\n"
        "Premier Ford Inc (our Glenwood Road store): 5001 Glenwood Rd, Brooklyn, NY 11234.\n"
        "Sales hours at both stores: Monday-Thursday 9am-9pm, Friday 9am-7:30pm, Saturday 9am-6pm, Sunday 11am-5pm.\n"
        "Service hours: Monday-Friday 7am-6pm; Saturday 7:30am-3:30pm at Bay Ridge, 7am-3:30pm at Glenwood Road; closed Sunday."
    ),
    # The same sales hours, Monday first, as [open, close] in hours: what "today" and "tomorrow" can mean in a text.
    "sales_hours": [[9, 21], [9, 21], [9, 21], [9, 21], [9, 19.5], [9, 18], [11, 17]],
    # Leads assigned to these salespeople are not texted until nobody has logged contact for fair_game_days.
    "excluded_reps": [],             # e.g. ["Lee, Morgan"]: set in private/textcrm.json
    # When not empty, only leads assigned to these salespeople are texted, e.g. ["Wright, Jordan"].
    "only_reps": [],
    "fair_game_days": 30,
    "do_not_text": [],              # numbers never to text again: set in private/textcrm.json, sent with `plans`
    # The BDC gets lead updates (matches, appointments, showroom visits, vehicle changes, financing, purchases) at
    # these times, at most bdc_max_per_day, and only when something changed. Their numbers are never treated as leads.
    "bdc_updates": False,           # off: no updates are texted to the BDC
    "bdc_numbers": [],              # set in private/textcrm.json
    "bdc_times": ["12:00", "17:00"],
    "bdc_max_per_day": 2,              # numbers never to text again, e.g. ["+19175550123"]
    "send_hours": [9, 20],          # local time, start inclusive / end exclusive
    "lead_window_days": 90,        # must be one of CDK's Days options: 7, 30, 60, 90
    "lease_days_before": 75,        # text lease customers this many days before maturity...
    "lease_days_after": 30,         # ...and up to this many days after
    "lease_report_span": [-120, 240],  # report date range, in days relative to today
    "seconds_between_sends": 1.2,
    # New-vehicle listings are read from these sites each day and turned into offers.
    "inventory_sites": {"Bay Ridge": "https://www.premierfordofbayridge.com",
                        "Glenwood Rd": "https://www.premierfordinc.com"},
    # How CDK writes your name: "Wright,J" in lists, "Wright, Jordan" on the sales team, "Jordan Wright" in the Audit
    # Trail (matched ignoring case, so "WRIGHT, J" in activity rows counts too): who "you" are in a record.
    "my_cdk_names": ["Wright,J", "Wright, J", "Jordan Wright"],
    "desk_log_days": 14,            # the CDK Desk Log's appointments are read from today through this many days out
    "refresh_hour": 8,              # `run` refreshes CDK and inventory once a day at or after this hour
    "sender": "fake",               # "fake", "telnyx" or "quo"
    "telnyx_from": "",              # E.164; API key comes from TELNYX_API_KEY
    "quo_from": "",                 # E.164; API key comes from QUO_API_KEY
    "webhook_token": "",            # required path segment for inbound webhooks
    "auto_reply": True,
    "codex_effort": "high",         # Codex thinks hard on every customer text; tokens are not the constraint
    "codex_timeout": 420,           # seconds per Codex answer
    "hanging_hours": 2,             # a text handed to you and still unanswered after this gets a holding reply
    # Your plan per customer, by phone: {"+1917...": {"goal": "...", "resume": "YYYY-MM-DD", "every_days": 3}}.
    # Customer details: set it in private/textcrm.json only, then `python -m textcrm plans` sends it to the cloud.
    "lead_plans": {},
    "quo_segment_cost": 0.01,       # Quo bills API texts per segment (US/Canada); texts you type in Quo are on your plan
    "credits_alert_below": 5.0,
    "moment_hours": 24,             # a quiet, engaged customer is looked at this often for a reason to text now
    "moments_per_day": 25,          # ...and no more looks than this a day: replies to customers come first on the plan
    "moment_effort": "medium",      # Codex effort for those looks
    "report_hour": 10,              # your daily report (appointments, who needs a push) goes after this hour
    "report_text_limit": 1500,      # one report text stays under Quo's 1600 characters     # text you when a run would take the Quo credit balance under this
    "rep_phone": "",                # your own phone: a text the moment a customer books a time or asks for a call
    "my_leads": [],                 # phones that are yours even if CDK names an excluded rep (you're texting them)
    "reply_provider": "codex",      # "codex" (Codex CLI, uses your ChatGPT login) or "anthropic" (API key)
    "reply_model": "claude-opus-5-5",  # used by the "anthropic" provider only
    "chrome_user_data": "",         # blank = the default Chrome profile folder
    "http_host": "127.0.0.1",       # "0.0.0.0" on a cloud host, so Quo and the PC can reach the webhook and feed
    "http_port": 8787,
    "cloud": False,                 # True on the cloud copy: no Chrome, data arrives at /feed/<feed_token>
    "cloud_url": "",                # on the PC: push each refresh to this cloud copy instead of storing locally
    "feed_token": "",
    "db": "private/textcrm.db",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    cfg.update(json.loads(os.environ.get("TEXTCRM_JSON") or "{}"))  # how a cloud host passes settings
    return cfg


def db_path(cfg: dict) -> Path:
    path = Path(cfg["db"])
    return path if path.is_absolute() else ROOT / path
