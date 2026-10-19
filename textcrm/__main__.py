"""Command line: python -m textcrm <command>."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from . import app, bdc, cdk_sync, config, db, engine, feed, matching, quo
from .sender import SendError, make_sender


def single_instance(lock_path):
    """A lock held for as long as this process lives, so a second `run` (started twice, or by the restart loop
    while the first is still up) exits instead of reading CDK and texting alongside it. None if already held."""
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run_forever(cfg: dict, path) -> None:
    """Three ways to run, by settings:
    alone on the PC   - inbox, daily refresh through Chrome, daily texts;
    cloud copy        - ("cloud": true) inbox and texts only; data arrives from the PC at /feed;
    PC feeding cloud  - ("cloud_url" set) only the daily refresh, pushed to the cloud copy.
    Chrome is connected once and kept, so you click Allow once per start."""
    reads, texts = not cfg["cloud"], not cfg["cloud_url"]
    sender = make_sender(cfg)
    if texts:
        threading.Thread(target=app.serve, args=(lambda: db.connect(path), sender, cfg), daemon=True).start()
    browser, refreshed, retry_at = None, None, datetime.min
    if texts:
        con = db.connect(path)
        print("soft no's back on follow-up:", engine.revive_soft_nos(con, cfg), flush=True)
        con.close()
    while True:
        moment = datetime.now()
        con = db.connect(path)
        try:
            if texts:
                engine.apply_do_not_text(con, cfg)  # first, every minute
                if bdc.maybe_send(con, sender, cfg, moment):
                    print(f"{moment:%m-%d %H:%M} bdc update sent", flush=True)
            if reads and refreshed != moment.date() and moment.hour >= cfg["refresh_hour"] and moment >= retry_at:
                try:
                    browser = browser or cdk_sync.Browser(cfg)
                    print(f"{moment:%m-%d %H:%M} refresh:", feed.refresh(con, cfg, browser), flush=True)
                    refreshed = moment.date()
                except cdk_sync.SyncError as error:
                    print(f"{moment:%m-%d %H:%M} refresh failed, retrying in 15 minutes: {error}", flush=True)
                    if browser:
                        browser.close()
                    browser, retry_at = None, moment + timedelta(minutes=15)
            if texts and cfg["sender"] == "quo":
                quo.sync_contacts(con)  # Quo shows who each lead is
            if texts and engine.within_hours(cfg, moment):
                for line in matching.work(con, sender, cfg):  # a yes to options gets one vehicle
                    print(f"{moment:%m-%d %H:%M} match {line}", flush=True)
                for line in engine.sweep_unanswered(con, sender, cfg):  # nobody waits on you noticing them
                    print(f"{moment:%m-%d %H:%M} unanswered {line}", flush=True)
                if engine.alert_ownership(con, sender, cfg):  # someone else stepping in on your customer
                    print(f"{moment:%m-%d %H:%M} ownership alert sent", flush=True)
                if engine.send_report(con, sender, cfg):  # your morning: appointments and who needs a push
                    print(f"{moment:%m-%d %H:%M} daily report sent", flush=True)
                checked = engine.send_check_ins(con, sender, cfg)  # no live conversation goes quiet for long
                for line in engine.moments(con, sender, cfg):  # the cadence is a floor: text when now is the moment
                    print(f"{moment:%m-%d %H:%M} moment {line}", flush=True)
                if checked:
                    print(f"{moment:%m-%d %H:%M} check-ins sent: {checked}", flush=True)
                # Texts still go out on yesterday's data if today's refresh has not worked yet.
                queued = engine.queue_daily(con, cfg)
                sent = engine.send_queued(con, sender, cfg)
                if queued["queued"] or sent["sent"] or sent["failed"]:
                    print(f"{moment:%m-%d %H:%M} queue: {queued} send: {sent}", flush=True)
        except Exception as error:  # keep the loop alive; the next minute tries again
            print(f"{moment:%m-%d %H:%M} error: {type(error).__name__}: {error}", flush=True)
        finally:
            con.close()
        time.sleep(60)


def main() -> int:
    parser = argparse.ArgumentParser(prog="textcrm")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="run around the clock: inbox, daily refresh, daily texts")
    sync = commands.add_parser("sync", help="pull leads, leases and both stores' inventory once (uses your open Chrome)")
    sync.add_argument("--only", choices=["leads", "leases", "inventory"])
    daily = commands.add_parser("daily", help="queue today's text for every eligible lead, then send")
    daily.add_argument("--queue-only", action="store_true", help="queue without sending")
    commands.add_parser("match", help="send one vehicle to each lead who said yes to options (where the texting runs)")
    commands.add_parser("serve", help="run the inbox and inbound webhook")
    inbound = commands.add_parser("inbound", help="simulate a customer text (testing)")
    inbound.add_argument("phone")
    inbound.add_argument("text")
    commands.add_parser("status", help="show counts")
    check = commands.add_parser("check", help="diagnostics from where texting runs: leads' state (no text) and a Codex test")
    check.add_argument("phones", nargs="*")
    check.add_argument("--codex", action="store_true", help="also run one Codex decision on made-up data")
    check.add_argument("--stats", metavar="SINCE", help="also count texts, replies and appointments since a date (YYYY-MM-DD)")
    check.add_argument("--unverified", action="store_true", help="also list leads you started in Quo that CDK has not shown")
    credits = commands.add_parser("credits", help="after a Quo top-up: the new balance, so you get a text before a run would run it low")
    credits.add_argument("balance", type=float)
    credits.add_argument("--alert-below", type=float, help="text you when a run would leave less than this (default $5)")
    commands.add_parser("plans", help="send your lead plans and do-not-text numbers (private/textcrm.json) to where texting runs")
    args = parser.parse_args()

    cfg = config.load()
    path = config.db_path(cfg)
    con = db.connect(path)
    try:
        if args.command == "run":
            lock = single_instance(path.parent / "textcrm-run.lock")
            if lock is None:
                print("textcrm is already running", file=sys.stderr)
                return 3
            run_forever(cfg, path)
        elif args.command == "sync":
            browser = cdk_sync.Browser(cfg)
            try:
                print(feed.refresh(con, cfg, browser, args.only))
            finally:
                browser.close()
        elif args.command == "daily":
            print("queue:", engine.queue_daily(con, cfg))
            if not args.queue_only:
                print("send:", engine.send_queued(con, make_sender(cfg), cfg))
        elif args.command == "match":
            print("\n".join(matching.work(con, make_sender(cfg), cfg)) or "nobody waiting")
        elif args.command == "serve":
            app.serve(lambda: db.connect(path), make_sender(cfg), cfg)
        elif args.command == "inbound":
            print(engine.handle_inbound(con, make_sender(cfg), cfg, args.phone, args.text))
        elif args.command == "check":
            payload = {"checks": {"leads": args.phones, "codex": args.codex, "unverified": args.unverified, "stats": args.stats}}
            print(json.dumps(feed.push(cfg, payload, timeout=600) if cfg["cloud_url"] else feed.apply(con, cfg, payload), indent=1))
        elif args.command == "plans":
            payload = {"plans": cfg["lead_plans"], "do_not_text": cfg["do_not_text"],
                       "settings": {"rep_phone": cfg["rep_phone"], "my_leads": cfg["my_leads"]}}
            print(feed.push(cfg, payload) if cfg["cloud_url"] else feed.apply(con, cfg, payload))
        elif args.command == "credits":
            payload = {"settings": {"credits": args.balance, "credits_alert_below": args.alert_below}}
            print(feed.push(cfg, payload) if cfg["cloud_url"] else feed.apply(con, cfg, payload))
        elif args.command == "status":
            for label, query in [("active leads", "SELECT count(*) FROM leads WHERE status='active'"),
                                 ("lease customers", "SELECT count(*) FROM leads WHERE lease_end IS NOT NULL"),
                                 ("opted out", "SELECT count(*) FROM leads WHERE status='opted_out'"),
                                 ("active offers", "SELECT count(*) FROM offers WHERE active=1"),
                                 ("new vehicles", "SELECT count(*) FROM inventory"),
                                 ("queued", "SELECT count(*) FROM messages WHERE status='queued'"),
                                 ("sent", "SELECT count(*) FROM messages WHERE status='sent'"),
                                 ("needs review", "SELECT count(*) FROM messages WHERE needs_review=1")]:
                print(f"{label:16} {con.execute(query).fetchone()[0]}")
            print(f"{'sender':16} {cfg['sender']}")
    except (cdk_sync.SyncError, SendError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
