import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from textcrm import bdc, cdk_sync, config, db, engine, feed, guard, inventory, matching, quo, replies
from textcrm.sender import FakeSender, parse_quo_outgoing, parse_quo_webhook, parse_telnyx_webhook

TODAY = date.today()
NOON = datetime.combine(TODAY, datetime.min.time()).replace(hour=12)


class TextCrmTest(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.cfg = dict(config.DEFAULTS, excluded_reps=["Lee, Morgan", "Baker, Tom", "Patel, Kim", "Rivera, Alex", "Moretti, Al"])
        self.sender = FakeSender()
        self.addCleanup(self.con.close)
        # No real Codex in tests: unless a test says otherwise, every decision leaves the text for you.
        self.decisions = []
        handoff = lambda lead, history, facts, cfg: self.decisions.append(history[-1]["body"]) or {
            "action": "handoff", "text": "", "status": "", "snooze_days": 0, "why": "test"}
        self.addCleanup(setattr, replies, "decide", replies.decide)
        replies.decide = handoff

    def lead(self, phone, **fields):
        fields.setdefault("date_in", (TODAY - timedelta(days=7)).isoformat())
        fields.setdefault("name", "Doe, Pat")
        fields.setdefault("first_name", "Pat")
        return db.upsert_lead(self.con, phone, **fields)

    def offer(self, body="Hi {first}, {rep} at {store} here about the {vehicle}.", audience="all"):
        self.con.execute("INSERT INTO offers(body,audience,created_at) VALUES(?,?,?)", (body, audience, db.now()))

    def test_excluded_reps_match_cdk_name_formats(self):
        for name in ("Rivera,A", "RIVERA, ALEX", "Baker,T", "Patel,K", "LEE, MORGAN", "Cole,D\nLee,M"):
            self.assertTrue(engine.excluded_rep(name, self.cfg), name)
        for name in ("Cole,D", "GRANT,J", "Rivera,M", "Wright,J", ""):
            self.assertFalse(engine.excluded_rep(name, self.cfg), name)

    def test_full_record_names_any_excluded_rep(self):
        for text in ("Sales Rep: Cole, D\nBD Agent: Lee, Morgan", "Assigned to Alex Rivera", "Manager: BAKER, TO"):
            self.assertTrue(engine.names_excluded_rep(text, self.cfg), text)
        for text in ("Sales Rep: Cole, D", "Customer asked about a Rivera trim", "Morgany Lees"):
            self.assertFalse(engine.names_excluded_rep(text, self.cfg), text)

    def test_nothing_goes_to_a_cdk_lead_whose_record_is_unread_or_whose_notes_say_stop(self):
        lead = {"salesperson": "Wright,J", "opp_id": "39245686", "opp_url": None}
        self.assertEqual(engine.blocked(dict(lead, notes_read=None), self.cfg), engine.UNCHECKED)  # no link, still unread
        for note in ("10/2/26 Spoke to customer, he is out of market", "Moved to Florida", "BOUGHT ELSEWHERE", "do not contact"):
            self.assertEqual(engine.blocked(dict(lead, notes_read="x", cdk_notes=note), self.cfg), engine.NOTES_SAY_STOP, note)
        self.assertEqual(engine.blocked(dict(lead, notes_read="x", cdk_notes="Wants a Bronco, call Saturday"), self.cfg), "")
        self.assertEqual(engine.blocked({"salesperson": "Wright,J"}, self.cfg), "")  # a lease customer with no CDK lead
        # A text already queued is dismissed at send time once the notes say stop.
        lead_id = self.lead("+17185550001", opp_id="1", notes_read="x", cdk_notes="ok")
        self.con.execute("INSERT INTO messages(lead_id,direction,kind,body,status,created_at) VALUES(?,'out','promo','hi','queued',?)",
                         (lead_id, db.now()))
        self.con.execute("UPDATE leads SET cdk_notes='customer is out of the market' WHERE id=?", (lead_id,))
        self.assertFalse(engine.deliver(self.con, self.sender, self.con.execute("SELECT max(id) FROM messages").fetchone()[0]))
        self.assertEqual(self.sender.sent, [])

    def test_silent_leads_get_offers_on_the_cadence_then_none(self):
        lead_id = self.lead("+17185550001")
        start = TODAY - timedelta(days=50)
        due = []
        for day in range(50):
            when = start + timedelta(days=day)
            if engine.offer_due(self.con, lead_id, self.cfg, when):
                due.append(day)
                self.con.execute("INSERT INTO messages(lead_id,direction,kind,body,status,created_at) VALUES(?,'out','promo','hi','sent',?)",
                                 (lead_id, f"{when.isoformat()}T12:00:00"))
        self.assertEqual(due, [0, 2, 4, 6, 9, 12, 15, 18, 22, 26, 30, 35, 40, 45])  # then nothing until they reply
        self.con.execute("INSERT INTO messages(lead_id,direction,kind,body,status,created_at) VALUES(?,'in','reply','hey','received',?)",
                         (lead_id, f"{start + timedelta(days=49)}T12:00:00"))
        self.assertTrue(engine.offer_due(self.con, lead_id, self.cfg, TODAY))  # a reply starts a fresh cadence

    def test_texts_read_like_a_person(self):
        self.assertEqual(engine.vehicle_name("2023 U Ford Explorer"), "2023 Ford Explorer")  # no CDK used flag
        self.assertEqual(guard.plain_voice("Sounds like you'd like to build it up over time. Which parts first?"), "Which parts first?")
        self.assertTrue(guard.check_decided_reply("Great, it sounds like you know what you want."))  # never "sounds like"
        self.assertTrue(engine.signed("You're welcome, Pat.", self.cfg).endswith("612 86th St, Brooklyn"))  # who and where
        lead_id = self.lead("+17185550001", status="paused")
        db.add_message(self.con, lead_id, "out", "decided", "No problem, I'm here if anything changes.", "sent")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Ok thanks", lambda *_: "Take care."),
                         "noted")  # one goodbye, not two
        self.assertEqual(self.sender.sent, [])

    def test_an_excluded_reps_lead_is_fair_game_after_a_quiet_month(self):
        def day(n):
            d = TODAY - timedelta(days=n)
            return f"{d.month}/{d.day}/{d.year}"
        record = (f"Sales Rep: Rivera, Alex\nCreated {day(80)}\n----\n{day(40)} Outbound Call - left message\n"
                  f"{day(35)} Text Message sent\n{day(2)} Lead status changed\nDate In: {day(80)}")
        self.assertEqual(engine.last_contact(record, TODAY), (TODAY - timedelta(days=35)).isoformat())
        self.assertIsNone(engine.last_contact("Sales Rep: Rivera, Alex\n----\nDate In: 1/2/2026", TODAY))
        lead = {"salesperson": "Rivera,A", "record_excluded": 1, "opp_url": None, "notes_read": "x"}
        self.assertEqual(engine.blocked(dict(lead, last_contact=(TODAY - timedelta(days=35)).isoformat()), self.cfg), "")
        self.assertEqual(engine.blocked(dict(lead, last_contact=(TODAY - timedelta(days=10)).isoformat()), self.cfg), engine.EXCLUDED)
        self.assertEqual(engine.blocked(dict(lead, last_contact=None), self.cfg), engine.EXCLUDED)  # no date: still theirs

    def test_leads_are_texted_only_after_their_record_is_checked(self):
        url = "https://crm.connectcdk.com/evo2/fresh/elead-v45/elead_track/newprospects/OpptyDetails.aspx?lDID=5&lPID=1"
        self.lead("+17185550001", opp_id="5", opp_url=url)
        self.lead("+17185550002", opp_id="6", opp_url=url.replace("=5", "=6"))
        self.lead("+17185550003")  # lease-report style: no CDK record to read
        names = lambda: sorted(l["phone"][-1] for l, _ in engine.audience(self.con, self.cfg, TODAY))
        self.assertEqual(names(), ["3"])
        matching.store_notes(self.con, {"5": {"text": "fine", "excluded": False}, "6": {"text": "x", "excluded": True}})
        self.assertEqual(names(), ["1", "3"])
        self.offer()
        engine.queue_daily(self.con, self.cfg, TODAY)
        out = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550002", "What time do you close?",
                                    lambda *_: "We close at 9.")
        self.assertEqual(out, "review")  # an excluded rep's lead is never answered automatically

    def test_audience_window_exclusions_and_lease(self):
        self.lead("+17185550001", salesperson="Cole,D")
        self.lead("+17185550002", salesperson="Rivera,A")
        self.lead("+17185550003", date_in=(TODAY - timedelta(days=100)).isoformat())
        self.lead("+17185550004", date_in="2023-11-01", lease_end=(TODAY + timedelta(days=42)).isoformat(), lease_still_owns=1, lease_renewed=0)
        self.lead("+17185550005", date_in=None, lease_end=(TODAY + timedelta(days=42)).isoformat(), lease_still_owns=1, lease_renewed=1)
        self.con.execute("UPDATE leads SET status='opted_out' WHERE phone='+17185550001'")
        self.lead("+17185550006")
        picked = {lead["phone"]: kind for lead, kind in engine.audience(self.con, self.cfg, TODAY)}
        self.assertEqual(picked, {"+17185550004": "lease", "+17185550006": "all"})

    def test_daily_queue_is_once_per_day_and_rotates_offers(self):
        self.lead("+17185550001", vehicle="2026 Ford Bronco")
        self.offer()
        self.offer("Second offer for {first}.")
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY)["queued"], 1)
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY)["queued"], 0)
        self.assertEqual(engine.send_queued(self.con, self.sender, self.cfg, NOON, pause=0)["sent"], 1)
        first = self.sender.sent[0][1]
        self.assertEqual(first, "Hi Pat, Jordan at Premier Ford of Bay Ridge here about the 2026 Ford Bronco.")
        self.assertNotIn("STOP", first)
        engine.queue_daily(self.con, self.cfg, TODAY + timedelta(days=2))
        engine.send_queued(self.con, self.sender, self.cfg, NOON, pause=0)
        self.assertEqual(self.sender.sent[1][1], "Second offer for Pat.")  # signed only where the thread starts
        self.assertEqual(engine.render("Third for {first}. - {rep}", {"first_name": "Pat", "vehicle": None, "lease_vehicle": None},
                                       self.cfg), "Third for Pat. - Jordan, Premier Ford")  # an opening text names the business

    def test_offers_about_other_models_go_only_to_leads_who_named_none(self):
        explorer = self.lead("+17185550001", vehicle="2022 U Ford Explorer")
        nothing = self.lead("+17185550002", vehicle=None)
        for model in ("Explorer", "Transit Commercial"):
            self.con.execute("INSERT INTO offers(body,audience,model,created_at) VALUES(?,?,?,?)",
                             (f"{{first}}, about the {model}.", "all", model, db.now()))
        self.offer("{first}, checking in.")
        picked = {}
        for lead_id in (explorer, nothing):
            lead = dict(self.con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
            picked[lead_id] = set()
            for _ in range(3):
                offer = engine.pick_offer(self.con, lead, "all")
                picked[lead_id].add(offer["model"] or "")
                db.add_message(self.con, lead_id, "out", "promo", offer["body"], "sent", offer_id=offer["id"])
        self.assertEqual(picked[explorer], {"Explorer", ""})
        self.assertEqual(picked[nothing], {"Explorer", "Transit Commercial", ""})

    def test_nothing_sends_outside_hours(self):
        self.lead("+17185550001")
        self.offer()
        engine.queue_daily(self.con, self.cfg, TODAY)
        result = engine.send_queued(self.con, self.sender, self.cfg, datetime(2026, 10, 8, 21, 0), pause=0)
        self.assertEqual((result["sent"], self.sender.sent), (0, []))

    def test_stop_opts_out_and_cancels_queued(self):
        self.lead("+17185550001")
        self.offer()
        engine.queue_daily(self.con, self.cfg, TODAY)
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "(718) 555-0001", "STOP"), "opt_out")
        self.assertEqual(engine.send_queued(self.con, self.sender, self.cfg, NOON, pause=0)["sent"], 0)
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY + timedelta(days=1))["queued"], 0)

    def test_money_questions_go_to_review_without_calling_the_model(self):
        self.lead("+17185550001")
        def drafter(*_):
            raise AssertionError("model must not be asked")
        for text in ("How much per month?", "what's the APR", "What is my trade worth", "is the bronco still available?",
                     "can you do $450", "any rebates?"):
            self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", text, drafter), "review", text)
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE needs_review=1").fetchone()[0], 6)

    def test_safe_reply_sends_and_risky_draft_is_held(self):
        self.lead("+17185550001")
        ok = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "What time do you close Saturday?",
                                   lambda *_: "We close at 6 on Saturday. Want to come by?")
        self.assertEqual((ok, len(self.sender.sent)), ("replied", 1))
        held = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "ok thanks, see you then",
                                     lambda *_: "Great, we can get you approved at 3.9% APR.")
        self.assertEqual((held, len(self.sender.sent)), ("review", 1))  # the risky draft is not sent; Codex decides, here: yours
        none = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "who do I ask for", lambda *_: None)
        self.assertEqual(none, "review")

    def test_contact_requests_are_flagged_and_the_bot_then_waits(self):
        self.lead("+17185550001")
        reply = lambda *_: "What day and time works best for a call?"
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Please call me", reply), "replied")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Now", reply), "review")
        self.assertEqual(len(self.sender.sent), 1)  # the bot answers once, then leaves it to a person
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE needs_review=1").fetchone()[0], 2)

    def test_contact_and_decline_classifier(self):
        for text in ("Please call me", "Can I come in tomorrow at 5?", "Saturday at 2 works", "can someone call me back"):
            self.assertTrue(guard.wants_contact(text), text)
        for text in ("What time do you close Saturday?", "Thanks!", "No", "who do I ask for", "I bought a car this weekend"):
            self.assertFalse(guard.wants_contact(text), text)
        for text in ("No", "No, thanks. I've already bought a car.", "Hey I got mustang thank you", "Hi - I am no longer looking for a car",
                     "Hi I'm not interested in buying one yet", "I'm ok thank you , I already got one for the price I was looking for."):
            self.assertTrue(guard.declined(text), text)
        for text in ("No budget wasn't as good as I thought", "Yes", "Sure. Let's see what you have", "I have one more year",
                     "I got your text"):
            self.assertFalse(guard.declined(text), text)

    def test_bought_elsewhere_gets_one_warm_close(self):
        self.lead("+17185550001")
        self.lead("+17185550002")
        no_model = lambda *_: (_ for _ in ()).throw(AssertionError("a close is never drafted"))
        out = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001",
                                    "No I'm good you can remove me. I bought a car this weekend. Thanks anyway", no_model)
        self.assertEqual(out, "opt_out")
        self.assertEqual(self.sender.sent[-1][1],
                         "Will do, congratulations! If you ever reconsider Ford, you can reach out to me at this number. - Jordan, Premier Ford of Bay Ridge, 612 86th St, Brooklyn")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I already bought one", no_model)
        self.assertEqual(len(self.sender.sent), 1)  # once per lead, and never after the opt-out
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550002", "Hey I got mustang thank you", no_model),
                         "declined")
        self.assertEqual(self.sender.sent[-1][1],
                         "Congratulations! If you ever reconsider Ford, you can reach out to me at this number. - Jordan, Premier Ford of Bay Ridge, 612 86th St, Brooklyn")
        self.assertEqual(self.con.execute("SELECT status FROM leads WHERE phone='+17185550002'").fetchone()[0], "paused")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "STOP"), "opt_out")
        self.assertEqual(len(self.sender.sent), 2)  # a plain STOP gets no close

    def test_customers_who_bought_from_us_get_a_congrats_not_a_goodbye(self):
        self.lead("+17185550001", vehicle="2026 Ford Transit-250 Cargo", date_in=(TODAY - timedelta(days=20)).isoformat())
        self.lead("+17185550002", vehicle="2026 Ford Bronco")
        self.lead("+17185550003", date_in=TODAY.isoformat())
        sold_on = (TODAY - timedelta(days=5)).strftime("%m/%d/%Y")
        counts = cdk_sync.store_sold(self.con, [{"Phone": "(718) 555-0001", "Date Sold": sold_on},
                                                {"Phone": "7185550003", "Date Sold": sold_on}], "Premier Ford of Bay Ridge")
        self.assertEqual(counts, {"sold": 1, "shopping_again": 1, "no_phone": 0})  # 3 came back as a newer lead
        self.offer()
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY)["queued"], 2)  # no offer to the sold customer
        no_model = lambda *_: (_ for _ in ()).throw(AssertionError("a close is never drafted"))
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I got it already", no_model)
        self.assertEqual(self.sender.sent[-1][1],
                         "Congrats on the new Transit-250 Cargo! Enjoy it, and if you ever need anything, just text me here. - Jordan, Premier Ford of Bay Ridge, 612 86th St, Brooklyn")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550002", "Already bought it from you guys last week", no_model)
        self.assertIn("Congrats on the new Bronco!", self.sender.sent[-1][1])
        self.assertEqual(self.con.execute("SELECT status FROM leads WHERE phone='+17185550002'").fetchone()[0], "sold")
        for text in ("I already got one for the price I was looking for", "I bought a car this weekend"):
            self.assertFalse(guard.bought_from_us(text), text)

    def test_a_no_pauses_daily_texts_without_a_reply(self):
        self.lead("+17185550001")
        self.offer()
        def drafter(*_):
            raise AssertionError("a no needs no answer")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "No thanks, not interested", drafter),
                         "declined")
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY + timedelta(days=1))["queued"], 0)
        self.assertEqual(self.sender.sent, [])

    def test_replies_read_as_the_rep_texting(self):
        cfg = dict(self.cfg, reply_provider="anthropic")
        history = [{"direction": "in", "body": "who is this?"}]
        self.addCleanup(setattr, replies, "_with_anthropic", replies._with_anthropic)
        for draft, sent in (("Thanks, Rivera! What day and time works for you to visit or have a call with Jordan?", None),
                            ("It’s Jordan from Premier Ford \u2014 you asked about the Bronco.",
                             "It's Jordan from Premier Ford - you asked about the Bronco."),
                            ("Sure, I can give you a call. What time works?", "Sure, I can give you a call. What time works?")):
            replies._with_anthropic = lambda *_, d=draft: d
            self.assertEqual(replies.draft_reply({"first_name": "Pat"}, history, cfg), sent, draft)

    def test_your_own_thread_is_answered_after_a_few_minutes(self):
        self.lead("+17185550001")
        self.offer()
        engine.queue_daily(self.con, self.cfg, TODAY)
        engine.send_queued(self.con, self.sender, self.cfg, NOON, pause=0)
        ours = self.con.execute("SELECT body FROM messages WHERE kind='promo'").fetchone()[0]
        self.assertEqual(engine.record_outgoing(self.con, "+17185550001", ours, "AC-unknown-yet"), "ours")
        self.assertEqual(engine.record_outgoing(self.con, "(718) 555-0001", "Hey it's me, want to swing by today?", "AC2"), "manual")
        out = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "What time do you close?", lambda *_: "We close at 9.")
        self.assertEqual(out, "later")  # your thread: not answered instantly, in case you are typing
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(engine.queue_daily(self.con, self.cfg, TODAY + timedelta(days=1))["queued"], 0)  # and sends no offers
        self.assertEqual(engine.record_outgoing(self.con, "+17185550001", "Hey it's me, want to swing by today?", "AC2"), "ours")
        answer = lambda *_: {"action": "reply", "text": "We close at 9 tonight.", "status": "", "snooze_days": 0, "why": "hours"}
        engine.sweep_unanswered(self.con, self.sender, self.cfg, answer, datetime.now() + timedelta(minutes=5))
        self.assertEqual(self.sender.sent[-1][1], "We close at 9 tonight.")  # then the sweep covers it

    def test_quo_contacts_follow_cdk(self):
        lead_id = self.lead("+17185550001", name="Doe, Pat", vehicle="2026 Ford Bronco", salesperson="Wright,J\nCole,D")
        calls = []
        def send(method, path, body):
            calls.append((method, path, body))
            return {"data": []} if method == "GET" else {"data": {"id": "CT1"}}
        self.assertEqual(quo.sync_contacts(self.con, send=send), 1)
        self.assertEqual(calls[0][0], "GET")  # looks for one made before a restart
        method, path, body = calls[1]
        self.assertEqual((method, path, body["externalId"]), ("POST", "/contacts", f"textcrm-{lead_id}"))
        self.assertEqual(body["defaultFields"]["firstName"], "Pat")
        self.assertEqual(body["defaultFields"]["lastName"], "Doe")
        self.assertEqual(body["defaultFields"]["company"], "2026 Ford Bronco")
        self.assertEqual(body["defaultFields"]["role"], "Wright,J, Cole,D")
        self.assertEqual(quo.sync_contacts(self.con, send=send), 0)  # unchanged: nothing written
        db.upsert_lead(self.con, "+17185550001", vehicle="2026 Ford Explorer")
        self.assertEqual(quo.sync_contacts(self.con, send=send), 1)
        self.assertEqual(calls[-1][:2], ("PATCH", "/contacts/CT1"))
        other = self.lead("+17185550002", name="Roe, Sam")
        found = lambda method, path, body: calls.append((method, path, body)) or (
            {"data": [{"id": "CT9", "externalId": f"textcrm-{other}"}]} if method == "GET" else {})
        self.assertEqual(quo.sync_contacts(self.con, send=found), 1)
        self.assertEqual(calls[-1][:2], ("PATCH", "/contacts/CT9"))  # updated, not created twice

    def test_a_thank_you_gets_a_warm_last_word_once(self):
        self.lead("+17185550001")
        warm = lambda *_: "You're welcome, Pat. If anything changes, I'm here."
        no_model = lambda *_: (_ for _ in ()).throw(AssertionError("not drafted as a reply"))
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Thanks", no_model, courtesy=warm), "closed")
        self.assertEqual(self.sender.sent[-1][1], "You're welcome, Pat. If anything changes, I'm here. - Jordan, Premier Ford of Bay Ridge, 612 86th St, Brooklyn")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "thanks again!", no_model, courtesy=warm), "noted")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001",
                                               "​\U0001F44D​ to “ You're welcome, Pat. ”", no_model, courtesy=warm), "noted")
        self.assertEqual(len(self.sender.sent), 1)  # one last word, nothing after it
        self.assertTrue(guard.reaction('Liked "See you Saturday"'))
        self.assertFalse(guard.reaction("Thanks"))
        replies._with_anthropic, real = (lambda *_: "Anytime! Want to come in Saturday?"), replies._with_anthropic
        self.addCleanup(setattr, replies, "_with_anthropic", real)
        self.assertEqual(replies.draft_courtesy({"first_name": "Pat"}, [{"direction": "in", "body": "thanks"}],
                                                dict(self.cfg, reply_provider="anthropic")), "You're welcome, Pat.")  # never a question

    def test_codex_decides_instead_of_going_silent(self):
        self.lead("+17185550001", vehicle="2027 Ford E-Transit-350 Cab Chassis")
        seen = {}
        def objection(lead, history, facts, cfg):
            seen["facts"] = facts
            return {"action": "reply", "status": "", "snooze_days": 0, "why": "budget",
                    "text": "Totally get it, budgets change. What number were you hoping to stay around?"}
        out = engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "No budget wasn't as good as I thought",
                                    lambda *_: None, decide=objection)
        self.assertEqual(out, "replied")
        self.assertIn("budgets change", self.sender.sent[-1][1])
        self.assertIn("E-Transit-350", seen["facts"])
        self.assertNotIn("555", seen["facts"])  # no phone numbers in what Codex sees
        promise = lambda *_: {"action": "reply", "text": "I can get you 0% APR on that one!", "status": "", "snooze_days": 0, "why": "x"}
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "can you do 0 apr", decide=promise), "review")
        self.assertEqual(len(self.sender.sent), 1)  # a money promise never goes out
        later = lambda *_: {"action": "reply", "text": "Sounds good, I'll reach out then.", "status": "paused", "snooze_days": 330, "why": "x"}
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "we can talk around next fall", lambda *_: None, decide=later)
        row = self.con.execute("SELECT status, snooze_until FROM leads WHERE phone='+17185550001'").fetchone()
        self.assertEqual((row[0], row[1]), ("paused", (TODAY + timedelta(days=330)).isoformat()))
        self.assertTrue(guard.check_decided_reply("New F-150s run about $52k to $118k MSRP depending on trim."))  # no prices
        self.assertTrue(guard.check_decided_reply("It's $399 a month"))

    def test_the_sweep_answers_anyone_left_waiting(self):
        self.lead("+17185550001")
        self.lead("+17185550002", salesperson="Rivera,A")
        for phone in ("+17185550001", "+17185550002"):
            lead_id = self.con.execute("SELECT id FROM leads WHERE phone=?", (phone,)).fetchone()[0]
            db.add_message(self.con, lead_id, "in", "inbound", "How much", "received", needs_review=1, reason="payment or price")
        self.con.execute("UPDATE messages SET created_at=?", ((datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"),))
        answer = lambda *_: {"action": "reply", "text": "Depends on the trim. What will you mostly use it for?",
                             "status": "", "snooze_days": 0, "why": "price"}
        self.assertEqual(len(engine.sweep_unanswered(self.con, self.sender, self.cfg, answer)), 1)
        self.assertEqual([to for to, _ in self.sender.sent], ["+17185550001"])  # not the excluded rep's lead
        self.assertEqual(engine.sweep_unanswered(self.con, self.sender, self.cfg, answer), [])  # answered once

    def test_the_bdc_hears_what_changed_twice_a_day_at_most(self):
        cfg = dict(self.cfg, bdc_numbers=["+13475550101", "+16465550102"])
        self.lead("+17185550001", name="Doe, Pat", vehicle="2026 Ford Bronco")
        self.lead("+17185550002", name="Roe, Sam", first_name="Sam", vehicle="2026 Ford F-150")
        at = lambda hm: datetime.combine(TODAY, datetime.min.time()).replace(hour=int(hm[:2]), minute=int(hm[3:]))
        self.assertEqual(bdc.maybe_send(self.con, self.sender, cfg, at("12:01")), 0)  # nothing new: nobody is texted
        engine.handle_inbound(self.con, self.sender, cfg, "+17185550001", "Can I come in Saturday at 2?", lambda *_: "Sure, see you then.")
        engine.handle_inbound(self.con, self.sender, cfg, "+17185550002", "I already bought one somewhere else", lambda *_: None)
        engine.handle_inbound(self.con, self.sender, cfg, "+13475550101", "thanks!", lambda *_: "no")  # the BDC replying
        self.sender.sent.clear()
        self.assertEqual(bdc.maybe_send(self.con, self.sender, cfg, at("17:00")), 0)  # off unless switched on
        cfg["bdc_updates"] = True
        self.assertEqual(bdc.maybe_send(self.con, self.sender, cfg, at("17:00")), 2)
        text = self.sender.sent[0][1]
        self.assertTrue(text.startswith("Hey, it's Jordan."))  # the first one says who this is
        self.assertIn("Updates on my leads:", text)  # in my own voice, never "Jordan's texting"
        self.assertLess(text.index("Pat D. (Bronco): wants to come in"), text.index("Sam R. (F-150): bought elsewhere"))
        self.assertNotIn("555", text)  # never a phone number
        self.assertEqual({to for to, _ in self.sender.sent}, {"+13475550101", "+16465550102"})
        engine.handle_inbound(self.con, self.sender, cfg, "+17185550001", "Actually can we do Sunday at 1?", lambda *_: "Sure.")
        self.assertEqual(bdc.maybe_send(self.con, self.sender, cfg, at("17:30")), 0)  # already sent at 17:00
        self.assertIsNone(self.con.execute("SELECT 1 FROM leads WHERE phone='+13475550101'").fetchone())  # never a lead

    def test_a_resent_webhook_is_answered_once(self):
        self.lead("+17185550001")
        draft = lambda *_: "Sounds good, let me know what your boss says."
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Hi, let me follow up my boss",
                                               draft, message_id="AC1"), "replied")
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Hi, let me follow up my boss",
                                               draft, message_id="AC1"), "duplicate")
        self.assertEqual(len(self.sender.sent), 1)

    def test_daily_offer_waits_for_45_quiet_days_and_their_timing(self):
        self.lead("+17185550001")
        self.lead("+17185550002")
        self.offer()
        no_reply = lambda *_: None
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "No problem", no_reply,
                                               courtesy=lambda *_: "Of course. Take care."), "closed")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550002", "But hey, get in touch with me in a year", no_reply)
        snooze = self.con.execute("SELECT snooze_until FROM leads WHERE phone='+17185550002'").fetchone()[0]
        self.assertEqual(snooze, (TODAY + timedelta(days=330)).isoformat())
        phones = lambda day: sorted(l["phone"][-1] for l, _ in engine.audience(self.con, self.cfg, day))
        self.assertEqual(phones(TODAY + timedelta(days=1)), [])  # both replied recently
        self.assertEqual(phones(TODAY + timedelta(days=46)), ["1"])  # 45 quiet days; 2 asked for a year
        self.con.execute("UPDATE leads SET date_in=?", ((TODAY + timedelta(days=300)).isoformat(),))  # still in the lead window
        self.assertEqual(phones(TODAY + timedelta(days=331)), ["1", "2"])
        self.assertEqual(guard.later_days("maybe in 3 months"), 76)
        self.assertEqual(guard.later_days("I have one more year"), 330)
        self.assertEqual(guard.later_days("What year is it?"), 0)
        for text in ("Ok thanks", "no worries!", "Thank you", "sounds good"):
            self.assertTrue(guard.acknowledgement(text), text)
        for text in ("ok what time are you open", "thanks, can you call me", "Okay send me the explorer"):
            self.assertFalse(guard.acknowledgement(text), text)

    def test_a_live_conversation_that_goes_quiet_gets_check_ins_for_45_days(self):
        self.lead("+17185550001")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I'm very busy this week. I will touch base next week",
                              lambda *_: "No problem, I'll be mindful of your schedule.")
        draft = lambda lead, history, cfg: "Hope the week's going ok. Still good to touch base next week?"
        at = lambda days: datetime.now() + timedelta(days=days, hours=1)
        sent = [day for day in range(60) if engine.send_check_ins(self.con, self.sender, self.cfg, draft, at(day))]
        self.assertEqual(sent, [1, 2, 3, 5, 7, 9, 12, 15, 19, 23, 27, 31, 35, 40, 45])  # 45 days, then we wait for them
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Sorry, crazy week", lambda *_: None)
        self.assertEqual(engine.send_check_ins(self.con, self.sender, self.cfg, draft, at(3)), 0)  # they wrote last: yours to answer

    def quiet_conversation(self, phone="+17185550001", hours_ago=30):
        """A real exchange that went quiet on our last text, a check-in, so the next one is not due yet."""
        lead_id = self.lead(phone, name="Doe, Pat", salesperson="Cole,D", vehicle="2026 Ford Mustang")
        for minutes, direction, body in ((-120, "out", "Want me to send you a Mustang?"), (-110, "in", "Yes I'm interested in it"),
                                         (-60, "out", "Would tomorrow at 5:30 or Saturday at 10 work?"), (-55, "in", "Let me check my schedule this week"),
                                         (0, "out", "Sounds good, let me know.")):
            at = (datetime.now() - timedelta(hours=hours_ago) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
            self.con.execute("INSERT INTO messages(lead_id,direction,kind,body,status,created_at,sent_at) VALUES(?,?,?,?,?,?,?)",
                             (lead_id, direction, ("check_in" if minutes == 0 else "auto") if direction == "out" else "inbound", body,
                              "sent" if direction == "out" else "received", at, at if direction == "out" else None))
        return lead_id

    def test_their_reply_rhythm_shows_how_long_this_silence_really_is(self):
        lead_id = self.quiet_conversation(hours_ago=30)
        lead = dict(self.con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        self.assertIn("They usually answer within about 10 minutes; this silence is 1 day, 180 times their usual.",
                      engine.scene(self.con, lead, self.cfg))

    def test_a_quiet_engaged_customer_is_texted_when_now_is_the_moment(self):
        lead_id = self.quiet_conversation(hours_ago=30)
        saturday_noon = datetime.now().replace(hour=12, minute=0)
        asked = []
        def decide(lead, history, facts, cfg):
            asked.append(facts)
            return {"action": "reply", "text": "Hope your week settled down. Still want to see the Mustang this weekend?",
                    "status": "", "snooze_days": 0, "why": "weekend after a busy week"}
        self.assertEqual(engine.moments(self.con, self.sender, self.cfg, decide, saturday_noon),
                         [f"{lead_id}: texted (weekend after a busy week)"])
        self.assertIn("No follow-up is scheduled for them today", asked[0])
        self.assertEqual(self.sender.sent[-1][1], "Hope your week settled down. Still want to see the Mustang this weekend?")
        self.assertEqual(engine.moments(self.con, self.sender, self.cfg, decide, saturday_noon), [])  # looked at: not again so soon
        self.assertEqual(engine.moments(self.con, self.sender, self.cfg, decide, saturday_noon.replace(hour=9, minute=30)), [])

    def test_desk_log_bookings_lead_the_report_and_tell_codex_not_to_ask_for_a_time(self):
        lead_id = self.quiet_conversation(hours_ago=30)
        engine.store_settings(self.con, {"rep_phone": "(917) 555-0100"})
        today = date.today()
        result = engine.store_desk(self.con, {"from": today.isoformat(), "days": 14, "rows": [
            {"day": today.isoformat(), "time": "4:00 PM", "name": "DOE, PAT", "salesperson": "GRANT, JULIA",
             "vehicle": "2026 Ford Ranger", "state": "Unconfirmed Appointment", "phones": ["(718) 555-0001"], "confirmed": False},
            {"day": (today + timedelta(days=3)).isoformat(), "time": "11:00 AM", "name": "Roe, Sam", "salesperson": "Cole, Dana",
             "vehicle": "", "state": "", "phones": ["(718) 555-0777"], "confirmed": True}]})
        self.assertEqual(result, {"appointments": 2, "matched_to_leads": 1})
        lead = dict(self.con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        self.assertIn(f"booked in CDK to come in {today:%a %m/%d} 4:00 PM with GRANT, JULIA (not confirmed yet)",
                      engine.scene(self.con, lead, self.cfg))
        at = datetime.now().replace(hour=11, minute=0)
        report = lambda leads, cfg: {"appointments": [{"lead": lead_id, "status": "loose", "note": "dup"}], "push": []}
        self.assertTrue(engine.send_report(self.con, self.sender, self.cfg, report, at))
        text = self.sender.sent[-1][1]
        self.assertIn(f"BOOKED IN CDK\n\nPat Doe\n(718) 555-0001\nRep: Cole,D\n2026 Ford Mustang\n{today:%a %m/%d} 4:00 PM, "
                      "not confirmed in CDK", text)
        self.assertIn("Sam Roe\n(718) 555-0777\nRep: Cole, Dana\n", text)  # not a lead of ours: from the Desk Log itself
        self.assertNotIn("dup", text)  # booked in CDK: not listed again from the texts
        engine.store_desk(self.con, {"from": today.isoformat(), "days": 14, "rows": []})  # a fresh read replaces the days
        self.assertEqual(engine.desk_bookings(self.con), [])

    def test_you_hear_when_cdk_hands_your_customer_to_someone_else(self):
        lead_id = self.quiet_conversation(hours_ago=30)  # a live conversation that began about a day ago
        engine.store_settings(self.con, {"rep_phone": "(917) 555-0100"})
        self.con.execute("UPDATE leads SET opp_id='77' WHERE id=?", (lead_id,))
        later, earlier = datetime.now() - timedelta(hours=2), datetime.now() - timedelta(days=20)
        audit = (f"{later:%m/%d/%Y %I:%M:%S %p} Premier Ford of Bay Ridge Alex Sample Add to Sales Team Added Alex Sample to "
                 "the sales team as a Primary Salesperson\n"
                 f"{later:%m/%d/%Y %I:%M:%S %p} Premier Ford of Bay Ridge Casey Agent Change Opportunity Status Set Opportunity "
                 "from Active/New to Active/Appointment Set\n"
                 f"{earlier:%m/%d/%Y %I:%M:%S %p} Premier Ford of Bay Ridge Old Rep Add to Sales Team Added Old Rep to the sales "
                 "team as a Primary Salesperson\n"
                 f"{later:%m/%d/%Y %I:%M:%S %p} Premier Ford of Bay Ridge Casey Agent Add to Sales Team Added Casey Agent to the "
                 "sales team as a BDC Agent")
        matching.store_notes(self.con, {"77": {"text": audit, "excluded": False}})
        matching.store_notes(self.con, {"77": {"text": audit, "excluded": False}})  # read again: nothing new
        self.assertEqual(engine.alert_ownership(self.con, self.sender, self.cfg), 1)
        text = self.sender.sent[-1][1]
        self.assertTrue(text.startswith("Heads up: CDK moved on your customer\n\nPat Doe\n(718) 555-0001\n"))
        self.assertIn("Alex Sample added as Primary Salesperson", text)
        self.assertIn("Casey Agent set an appointment", text)
        self.assertNotIn("Old Rep", text)  # before your conversation began
        self.assertNotIn("BDC Agent", text)  # the BDC working it is not someone taking it
        self.assertEqual(engine.alert_ownership(self.con, self.sender, self.cfg), 0)  # once

    def test_warm_conversations_cdk_knows_nothing_about_are_yours_to_claim(self):
        lead_id = self.quiet_conversation(hours_ago=30)
        self.con.execute("UPDATE leads SET notes_read='x', cdk_notes='Call #01 left message', last_contact=? WHERE id=?",
                         ((date.today() - timedelta(days=5)).isoformat(), lead_id))
        claims = engine.unclaimed(self.con, self.cfg, datetime.now())
        self.assertEqual([lead for lead, _ in claims], [lead_id])
        self.assertIn("CDK never names you", claims[0][1])  # Wright, Jordan is nowhere in the record
        self.con.execute("UPDATE leads SET cdk_notes='Call #01 left message WRIGHT, J' WHERE id=?", (lead_id,))
        self.assertEqual(engine.unclaimed(self.con, self.cfg, datetime.now()), [])  # you are on it
        unnamed = dict(self.cfg, my_cdk_names=[])  # without your name: any contact logged since they began
        self.assertIn("CDK shows no contact logged since then", engine.unclaimed(self.con, unnamed, datetime.now())[0][1])
        self.con.execute("UPDATE leads SET last_contact=? WHERE id=?", (date.today().isoformat(), lead_id))
        self.assertEqual(engine.unclaimed(self.con, unnamed, datetime.now()), [])

    def test_at_the_usage_limit_optional_work_waits_and_replies_keep_trying(self):
        self.addCleanup(setattr, replies, "LIMITED_UNTIL", None)
        replies.note_limit("ERROR: You've hit your usage limit. ... or try again at 2:06 PM.")
        self.assertEqual((replies.LIMITED_UNTIL.hour, replies.LIMITED_UNTIL.minute), (14, 6))
        lead_id = self.quiet_conversation(hours_ago=30)
        noon = datetime.now().replace(hour=12, minute=0)
        replies.LIMITED_UNTIL = noon + timedelta(hours=1)
        self.assertEqual(engine.moments(self.con, self.sender, self.cfg, lambda *_: 1 / 0, noon), [])  # not even asked
        replies.LIMITED_UNTIL = None
        self.assertEqual(engine.moments(self.con, self.sender, self.cfg, lambda *_: None, noon), [])  # no decision...
        self.assertFalse(self.con.execute("SELECT 1 FROM audit WHERE lead_id=? AND kind='moment'", (lead_id,)).fetchone())

    def test_the_daily_report_texts_you_names_numbers_and_notes_spaced_out(self):
        lead_id = self.quiet_conversation(hours_ago=30)
        engine.store_settings(self.con, {"rep_phone": "(917) 555-0100"})
        seen = []
        def report(leads, cfg):
            seen.append(leads)
            return {"appointments": [], "push": [{"lead": lead_id, "note": "Interested in the Mustang, never picked a time."}]}
        at = datetime.now().replace(hour=11, minute=0)
        self.assertTrue(engine.send_report(self.con, self.sender, self.cfg, report, at))
        self.assertNotIn("Doe", seen[0])  # Codex never sees last names or numbers
        self.assertNotIn("555-0001", seen[0])
        self.assertEqual(self.sender.sent[-1], ("+19175550100", f"Daily report, {at:%a %m/%d}\n\n\nNEEDS A PUSH\n\nPat Doe\n"
                                                "(718) 555-0001\nRep: Cole,D\n2026 Ford Mustang\nInterested in the Mustang, never picked a time."))
        self.assertFalse(engine.send_report(self.con, self.sender, self.cfg, report, at))  # once a day

    def test_credit_watcher_texts_you_once_before_a_run_would_go_under_your_line(self):
        self.assertEqual((engine.segments("a" * 160), engine.segments("a" * 161), engine.segments("é" * 71)), (1, 2, 2))
        engine.store_settings(self.con, {"rep_phone": "(718) 555-0199"})
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550199", "credits $6"), "staff")
        self.assertEqual(self.sender.sent[-1], ("+17185550199", "Got it: $6.00 in Quo credits."))
        self.assertAlmostEqual(engine.credits_left(self.con, self.cfg), 6.0)
        for n in range(3):
            self.lead(f"+1718555000{n}")
        self.offer("Hi {first}, checking in.")
        engine.queue_daily(self.con, self.cfg, TODAY)
        self.assertFalse(engine.watch_credits(self.con, self.sender, self.cfg, ["hi"] * 3))  # $5.97 left: over $5
        self.assertTrue(engine.watch_credits(self.con, self.sender, self.cfg, ["hi"] * 150))
        self.assertIn("Quo credits: about $6.00 left. The next run is 150 texts", self.sender.sent[-1][1])
        self.assertFalse(engine.watch_credits(self.con, self.sender, self.cfg, ["hi"] * 150))  # once per top-up
        engine.send_queued(self.con, self.sender, self.cfg, NOON, pause=0)
        self.assertAlmostEqual(engine.credits_left(self.con, self.cfg), 5.97)
        engine.store_settings(self.con, {"credits": 50})
        self.assertEqual(db.setting(self.con, "credits_alerted"), "")  # a top-up re-arms it

    def test_a_reply_quo_refused_for_credits_is_decided_again_once_texts_go_out(self):
        lead_id = self.lead("+17185550001")
        inbound = db.add_message(self.con, lead_id, "in", "inbound", "can you do tuesday afternoon", "received",
                                 reason="decided: reply failed to send", needs_review=1)
        failed = db.add_message(self.con, lead_id, "out", "decided", "Tuesday at 2 works?", "failed",
                                reason="Quo 402: not enough prepaid credits")
        self.con.execute("UPDATE messages SET created_at=? WHERE id IN (?,?)",
                         ((datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"), inbound, failed))
        self.assertTrue(engine.credits_paused(self.con))
        self.assertEqual(engine.sweep_unanswered(self.con, self.sender, self.cfg), [])  # Quo just refused: hold
        engine.set_credits(self.con, 20)  # your top-up
        self.assertFalse(engine.credits_paused(self.con))
        self.assertEqual(engine.sweep_unanswered(self.con, self.sender, self.cfg), [f"{inbound}: review"])
        self.assertEqual(self.decisions, ["can you do tuesday afternoon"])  # decided again, fresh

    def test_numbers_you_texted_are_recognized(self):
        for text in ("About $575. Come see me.", "Yes with 15k down.", "it'll be about 420 a month", "the payment would be about 389"):
            self.assertTrue(guard.gave_numbers(text), text)
        for text in ("We are at 612 86th St, Brooklyn", "the 2026 Ford Explorer lease", "I have 10:15AM and 3:30PM",
                     "Call (718) 555-0100", "How much down were you thinking?", "Is it the payment or the timing?"):
            self.assertFalse(guard.gave_numbers(text), text)

    def test_a_reply_may_repeat_your_figures_and_no_others(self):
        quoted = engine.quoted_figures([{"direction": "out", "body": "Yes with 15k down. With 10k down it's about $480."}])
        self.assertEqual(quoted, {"15000", "10000", "480"})
        self.assertEqual(guard.check_decided_reply("Yes, $480 with 10k down on the F-150. Today at 1 or tomorrow at noon?", quoted), "")
        self.assertTrue(guard.check_decided_reply("I can get you to $450 with 10k down.", quoted))  # a new number
        self.assertTrue(guard.check_decided_reply("$480 and you're approved.", quoted))
        self.assertTrue(guard.check_decided_reply("It's about $480.", set()))  # nothing of yours to repeat

    def test_codex_sees_the_scene_the_clock_and_your_standing_numbers(self):
        lead_id = self.lead("+17185550001")
        db.add_message(self.con, lead_id, "in", "inbound", "5k down", "received")
        db.add_message(self.con, lead_id, "out", "manual", "About $575 with 5k down.", "sent")
        lead = dict(self.con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        saturday = datetime(2026, 10, 17, 17, 30)
        self.assertIn("open right now until 6 PM today (0 hours 30 minutes left)", engine.opening_hours(self.cfg, saturday))
        self.assertIn("opens tomorrow (Sunday) at 11 AM", engine.opening_hours(self.cfg, saturday.replace(hour=19)))
        self.assertIn("opens today at 9 AM", engine.opening_hours(self.cfg, saturday.replace(hour=7)))
        scene = engine.scene(self.con, lead, self.cfg, datetime.now() + timedelta(hours=2))
        self.assertIn("typed by you by hand; they have not answered it", scene)
        self.assertIn("2 hours ago", scene)
        self.assertIn("They stand exactly as written", scene)
        self.assertIn("About $575 with 5k down.", scene)
        line = replies.said({"direction": "out", "kind": "manual", "body": "hi", "at": "2026-10-17T15:05:00"}, self.cfg)
        self.assertEqual(line, "[Sat 10/17 3:05 PM] Jordan (typed by hand): hi")

    def test_after_your_numbers_go_quiet_the_follow_ups_work_toward_a_visit(self):
        lead_id = self.lead("+17185550001")
        db.add_message(self.con, lead_id, "in", "inbound", "what would it be with 5k down", "received")
        db.add_message(self.con, lead_id, "out", "manual", "About $575 with 5k down.", "sent")
        steps = []
        def draft(lead, history, cfg):
            steps.append(lead["quote_step"])
            return "Is it the payment, or just the timing?" if lead["quote_step"] == 1 else "Want to come in tomorrow at 11 or 3?"
        at = lambda days: datetime.now() + timedelta(days=days, hours=1)
        for day in range(1, 6):
            engine.send_check_ins(self.con, self.sender, self.cfg, draft, at(day))
        self.assertEqual(steps, [0, 1, 2, None])  # then ordinary check-ins
        self.assertIn("Is it the payment, or just the timing?", [body for _, body in self.sender.sent])  # "payment" may be said
        history = [{"direction": "out", "body": "About $575 with 5k down."}, {"direction": "in", "body": "Ok thanks"}]
        self.assertEqual(engine.quote_step(history), 0)  # an "ok" is not an answer to the numbers
        self.assertIsNone(engine.quote_step(history + [{"direction": "in", "body": "Too high for me"}]))

    def test_a_thread_you_ran_by_hand_still_gets_check_ins(self):
        self.lead("+17185550001")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Can you send me the Bronco info?", lambda *_: None)
        engine.record_outgoing(self.con, "+17185550001", "Here it is, let me know what you think", "QUO1")  # you, from Quo
        draft = lambda lead, history, cfg: "Did you get a chance to look at the Bronco?"
        self.assertEqual(engine.send_check_ins(self.con, self.sender, self.cfg, draft, datetime.now() + timedelta(hours=12)), 0)
        self.assertEqual(engine.send_check_ins(self.con, self.sender, self.cfg, draft, datetime.now() + timedelta(days=1, hours=1)), 1)

    def test_not_yet_is_a_break_not_a_no(self):
        for text, soft in (("Hi I'm not interested in buying one yet", True),
                           ("I am not in need at the moment but let me check with a few people", True),
                           ("Not interested", False), ("No thanks I already bought a car", False)):
            self.lead("+17185550001", status="active", snooze_until=None)
            engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", text, lambda *_: None)
            lead = self.con.execute("SELECT status, snooze_until FROM leads WHERE phone='+17185550001'").fetchone()
            self.assertEqual(lead["status"] == "active", soft, text)
            if soft:
                self.assertEqual(lead["snooze_until"], (date.today() + timedelta(days=7)).isoformat())
            self.con.execute("DELETE FROM messages")
            self.con.execute("UPDATE leads SET status='active', snooze_until=NULL")

    def test_leads_paused_for_a_not_yet_go_back_on_follow_up(self):
        for phone, text in (("+17185550001", "Hi I'm not interested in buying one yet"), ("+17185550002", "Not interested"),
                            ("+17185550003", "I already bought one, not looking right now")):
            lead_id = self.lead(phone, status="paused")
            db.add_message(self.con, lead_id, "in", "inbound", text, "received")
        self.assertEqual(engine.revive_soft_nos(self.con, self.cfg), 1)
        self.assertEqual(self.con.execute("SELECT phone FROM leads WHERE status='active'").fetchall()[0][0], "+17185550001")

    def test_a_plan_sets_the_pace_and_rides_along_with_every_answer(self):
        lead_id = self.lead("+17185550001")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I'm very busy this week", lambda *_: "No rush.")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Ok thanks", lambda *_: None)
        resume = TODAY + timedelta(days=4)
        engine.store_plans(self.con, {"(718) 555-0001": {"goal": "Get her in to place the order; no numbers by text",
                                                         "resume": resume.isoformat(), "every_days": 3}})
        seen = []
        draft = lambda lead, history, cfg: seen.append(lead) or "Still good to swing by this week?"
        at = lambda day: datetime.combine(resume, datetime.min.time()) + timedelta(days=day, hours=12)
        self.sender.sent.clear()
        self.assertEqual(engine.send_check_ins(self.con, self.sender, self.cfg, draft, at(-1)), 0)  # not before resume
        sent = []
        for day in range(50):
            if engine.send_check_ins(self.con, self.sender, self.cfg, draft, at(day)):
                sent.append(day)
                self.con.execute("UPDATE messages SET created_at=?, sent_at=? WHERE id=(SELECT max(id) FROM messages)",
                                 (at(day).isoformat(timespec="seconds"),) * 2)
        self.assertEqual(sent[:3], [0, 3, 6])  # after her "Ok thanks", every 3 days from the resume date
        self.assertEqual(sent[-1], 45)  # for 45 days
        self.assertIn("plan.txt", seen[0]["workspace"])  # Codex reads the plan with the whole thread
        self.assertIn("no numbers by text", engine.lead_facts(self.con, dict(self.con.execute(
            "SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())))

    def test_only_new_and_engaged_records_are_read_again(self):
        old = (TODAY - timedelta(days=30)).isoformat()
        quiet = self.lead("+17185550001", opp_id="1", date_in=old)
        replied = self.lead("+17185550002", opp_id="2", date_in=old)
        asked_price = self.lead("+17185550003", opp_id="3", date_in=old)
        self.lead("+17185550004", opp_id="4", date_in=TODAY.isoformat())  # fresh in CDK
        self.lead("+17185550005", opp_id="5", date_in=old, status="paused")
        db.add_message(self.con, replied, "in", "inbound", "ok thanks", "received")
        db.add_message(self.con, asked_price, "in", "inbound", "How much a month on the Bronco?", "received")
        db.add_message(self.con, quiet, "in", "inbound", "ok", "received")
        self.con.execute("UPDATE messages SET created_at=? WHERE lead_id IN (?,?)",
                         ((TODAY - timedelta(days=20)).isoformat(), quiet, asked_price))
        self.assertEqual(engine.engaged_opps(self.con, TODAY), ["2", "3", "4"])
        # The PC then reads new records, and engaged ones once a day.
        read = []
        rows = [{"_opp": str(n), "_url": f"https://crm.connectcdk.com/x/NewProspects/OpptyDetails.aspx?lDID={n}", "Name": "Doe, Pat"}
                for n in (1, 2, 3, 6)]
        seen = Path(tempfile.mkdtemp()) / "seen.json"
        seen.write_text(json.dumps({"1": "2026-01-01", "2": "2026-01-01", "3": TODAY.isoformat()}), encoding="utf-8")
        original = matching.read_lead_record
        matching.read_lead_record = lambda browser, url: read.append(url[-1]) or "record"
        try:
            matching.read_notes(None, rows, seen, TODAY, self.cfg, lambda batch: None, engaged={"2", "3"})
        finally:
            matching.read_lead_record = original
        self.assertEqual(sorted(read), ["2", "6"])  # 6 is new; 3 was read today; 1 is quiet

    def test_you_hear_about_a_booking_and_your_leads_stay_yours(self):
        self.lead("+17185550001", name="Doe, Pat", vehicle="2026 Ford Mustang", salesperson="Rivera,A")
        engine.store_settings(self.con, {"rep_phone": "(917) 555-0100", "my_leads": ["+17185550001"]})
        lead = dict(self.con.execute("SELECT * FROM leads WHERE phone='+17185550001'").fetchone())
        self.assertEqual(engine.blocked(lead, self.cfg), "")  # an excluded rep's lead, but yours
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "Can I come in Saturday at 10?", lambda *_: "Sure, see you then.")
        alerts = [body for to, body in self.sender.sent if to == "+19175550100"]
        self.assertEqual(alerts, ['Appointment/call\n\nPat Doe\n(718) 555-0001\nRep: Rivera,A\n2026 Ford Mustang\n'
                                  'Said: "Can I come in Saturday at 10?"\n\nCheck Quo.'])
        self.assertEqual(engine.handle_inbound(self.con, self.sender, self.cfg, "+19175550100", "got it"), "staff")

    def test_checks_report_state_without_customer_text(self):
        lead_id = self.lead("+17185550001", cdk_notes="Wants a Bronco", opp_id="7", notes_read="x")
        db.add_message(self.con, lead_id, "in", "inbound", "secret words here", "received", reason="decided: left alone")
        out = feed.checks(self.con, self.cfg, {"leads": ["+17185550001", "+17185550009"]})
        self.assertEqual(out["leads"]["0001"]["last_inbound"]["reason"], "decided: left alone")
        self.assertEqual(out["leads"]["0001"]["workspace"]["cdk_record.txt"], len("Wants a Bronco"))
        self.assertEqual(out["leads"]["0009"], "not found")
        self.assertNotIn("secret words", json.dumps(out))

    def test_record_page_labels_are_not_notes(self):
        page = ("Status Active Sold Delivered Sold Inactive Bought Elsewhere Not In Market Unable to Obtain Financing\n"
                "Do Not Email: False|Do Not Call: False|Primary Email: True\nWants a Bronco, call Saturday")
        self.assertFalse(guard.notes_say_stop(page))
        self.assertTrue(guard.notes_say_stop(page + "\nPurchased elsewhere, out of market as of 10/02/26"))
        self.assertTrue(guard.notes_say_stop(page + "\n10/2/26 9:14 AM Inactive - Not In Market"))
        self.assertTrue(guard.notes_say_stop(page.replace("Do Not Call: False", "Do Not Call: True")))

    def test_every_stop_gets_one_confirmation(self):
        self.lead("+17185550001")
        self.lead("+17185550002")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I will reach out when I'm ready please stop texting me.")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550002", "Wrong number")
        self.assertTrue(self.sender.sent[0][1].startswith("Understood, I'll stop texting you."))
        self.assertTrue(self.sender.sent[1][1].startswith("Sorry about that"))
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "STOP")
        self.assertEqual(len(self.sender.sent), 2)  # once, then nothing

    def test_a_trade_in_elsewhere_gets_the_open_door(self):
        self.lead("+17185550001")
        engine.handle_inbound(self.con, self.sender, self.cfg, "+17185550001", "I already traded in for a new truck thank you")
        self.assertIn("reconsider Ford", self.sender.sent[-1][1])

    def test_nobody_is_left_hanging(self):
        lead_id = self.lead("+17185550001")
        db.add_message(self.con, lead_id, "in", "inbound", "Depending on the value I might get big van high roof", "received",
                       needs_review=1, reason="decided: yours to answer (trade value)")
        self.con.execute("UPDATE messages SET created_at=?", ((datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"),))
        facts = []
        hold = lambda lead, history, f, cfg: facts.append(f) or {
            "action": "reply", "text": "Values depend on miles and condition. Could you bring the Sprinter by this week?",
            "status": "", "snooze_days": 0, "why": "trade"}
        engine.sweep_unanswered(self.con, self.sender, self.cfg, hold)
        self.assertIn("Do not hand off again", facts[0])
        self.assertIn("Sprinter", self.sender.sent[-1][1])
        engine.sweep_unanswered(self.con, self.sender, self.cfg, hold)
        self.assertEqual(len(self.sender.sent), 1)  # once

    def test_guard_classifier(self):
        self.assertEqual(guard.classify_inbound("please stop texting me")[0], "opt_out")
        self.assertEqual(guard.classify_inbound("Can I come in tomorrow at 5?")[0], "ok")
        self.assertEqual(guard.classify_inbound("Thanks!")[0], "ok")

    def test_cdk_rows_are_stored_and_merged_by_phone(self):
        rows = [{"Name": "Stone, Riley", "Date In": "10/05/2026", "Phone": "(540) 555-0100", "Vehicle": "2025 N Ford Bronco Sport",
                 "Salesperson": "Cole,D", "Source": "LVDS", "_opp": "123"},
                {"Name": "No, Phone", "Date In": "10/05/2026", "Phone": "", "Vehicle": "", "Salesperson": "", "Source": ""}]
        self.assertEqual(cdk_sync.store_leads(self.con, rows, "Bay Ridge"),
                         {"saved": 1, "no_phone": 1, "verified": 0, "duplicates": 0})
        leases = [{"Customer": "STONE, RILEY", "Phone Number": "(540) 555-0100", "VIN": "1FM", "Vehicle": "2023 Escape",
                   "Estimated Last Payment": "11/18/2026 12:00:00 AM", "Term": "36", "Still Owns": "YES", "Renewal Date": "",
                   "Payment": "$470.15", "New Sales Person": "LEE, MORGAN", "Original Sales Person": "LEE, MORGAN"}]
        cdk_sync.store_leases(self.con, leases, "Bay Ridge")
        row = self.con.execute("SELECT * FROM leads").fetchone()
        self.assertEqual((row["first_name"], row["vehicle"], row["date_in"], row["lease_end"], row["salesperson"]),
                         ("Riley", "2025 Ford Bronco Sport", "2026-10-05", "2026-11-18", "Cole,D"))

    def test_a_lead_you_took_is_merged_with_cdk_and_logged_not_duplicated(self):
        engine.record_outgoing(self.con, "+17185550001", "Hi Ed, I'll call you shortly", "Q1")  # you, before CDK has it
        row = lambda: self.con.execute("SELECT * FROM leads").fetchone()
        self.assertEqual((row()["origin"], row()["opp_id"]), ("you", None))
        unverified = feed.checks(self.con, self.cfg, {"unverified": True})["unverified"]
        self.assertEqual([u["phone"] for u in unverified], ["0001"])
        cdk = {"Name": "Smith, Ed", "Date In": "10/15/2026", "Phone": "(718) 555-0001", "Vehicle": "2026 N Ford Transit",
               "Salesperson": "Me", "Source": "Walk-in", "_opp": "500"}
        self.assertEqual(cdk_sync.store_leads(self.con, [cdk], "Bay Ridge")["verified"], 1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM leads").fetchone()[0], 1)
        self.assertEqual((row()["first_name"], row()["opp_id"], row()["origin"]), ("Ed", "500", "you"))
        self.assertEqual(cdk_sync.store_leads(self.con, [cdk], "Bay Ridge")["verified"], 0)  # once
        self.assertEqual(feed.checks(self.con, self.cfg, {"unverified": True})["unverified"], [])
        # The same customer entered in CDK a second time, earlier: one lead, both records read, the newest is main.
        older = dict(cdk, _opp="400", **{"Date In": "10/01/2026"})
        self.assertEqual(cdk_sync.store_leads(self.con, [older, cdk], "Bay Ridge")["duplicates"], 1)
        self.assertEqual(cdk_sync.store_leads(self.con, [cdk, older], "Bay Ridge")["duplicates"], 0)
        self.assertEqual((row()["opp_id"], row()["other_opps"]), ("500", "400"))
        matching.store_notes(self.con, {"400": {"text": "old visit", "excluded": False},
                                        "500": {"text": "new inquiry", "excluded": False}})
        self.assertTrue(row()["cdk_notes"].startswith("new inquiry"))
        self.assertIn("old visit", row()["cdk_notes"])
        check = feed.checks(self.con, self.cfg, {"leads": ["+17185550001"]})["leads"]["0001"]
        self.assertEqual((check["in_cdk"], check["cdk_records"], check["origin"]), (True, 2, "you"))
        kinds = " ".join(check["audit"])
        for kind in ("started_by_you", "verified_in_cdk", "second_cdk_record", "record_read"):
            self.assertIn(kind, kinds)
        self.assertNotIn("Hi Ed", kinds)  # no customer text in the log

    def test_stats_count_texts_by_sender_and_confirmed_appointments(self):
        lead = self.lead("+17185550001")
        db.add_message(self.con, lead, "out", "promo", "Want options?", "sent")
        db.add_message(self.con, lead, "in", "inbound", "Can I come Saturday at 10?", "received")
        engine.record_outgoing(self.con, "+17185550001", "Great, see you Saturday at 10", "Q1")
        db.add_message(self.con, self.lead("+17185550002"), "out", "promo", "Want options?", "sent")
        got = feed.checks(self.con, self.cfg, {"stats": "2000-01-01"})["stats"]
        self.assertEqual((got["texts_out"], got["texts_in"], got["out_by"]), (3, 1, {"bot: promo": 2, "you": 1}))
        self.assertEqual((got["leads_texted"], got["leads_replied"], got["asked_for_time_or_call"],
                          got["appointments_confirmed_by_text"]), (2, 1, 1, 1))

    def test_a_number_quo_refuses_is_tried_once_then_left_alone(self):
        from textcrm.sender import SendError

        class Refuses(FakeSender):
            def send(self, to, body):
                raise SendError('Quo 400: {"message":"At least one of the phone numbers in the `to` array is invalid"}')
        lead = self.lead("+17185550001", opp_id="5", notes_read="x", cdk_notes="fine")
        self.offer()
        moment = datetime.combine(TODAY, datetime.min.time()).replace(hour=12)
        for _ in range(3):  # the loop runs every minute
            engine.queue_daily(self.con, self.cfg, TODAY)
            engine.send_queued(self.con, Refuses(), self.cfg, moment, pause=0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE status='failed'").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT status FROM leads WHERE id=?", (lead,)).fetchone()[0], "bad_number")

    def test_telnyx_webhook_parse(self):
        payload = {"data": {"event_type": "message.received", "payload": {"from": {"phone_number": "+17185550001"}, "text": "hi"}}}
        self.assertEqual(parse_telnyx_webhook(payload), ("+17185550001", "hi"))
        self.assertIsNone(parse_telnyx_webhook({"data": {"event_type": "message.sent"}}))

    def test_quo_webhook_parse(self):
        payload = {"type": "message.received", "data": {"object": {"from": "+17185550001", "to": "+19295550002", "body": "hi"}}}
        self.assertEqual(parse_quo_webhook(payload), ("+17185550001", "hi"))
        self.assertIsNone(parse_quo_webhook({"type": "message.delivered", "data": {"object": {"from": "+17185550001"}}}))
        sent = {"type": "message.delivered", "data": {"object": {"id": "AC9", "from": "+19295550002", "to": ["+17185550001"],
                                                                  "direction": "outgoing", "body": "on my way"}}}
        self.assertEqual(parse_quo_outgoing(sent), ("+17185550001", "on my way", "AC9"))
        self.assertIsNone(parse_quo_outgoing(payload))


class InventoryOffers(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.cfg = dict(config.DEFAULTS, excluded_reps=["Lee, Morgan", "Baker, Tom", "Patel, Kim", "Rivera, Alex", "Moretti, Al"])
        self.addCleanup(setattr, replies, "decide", replies.decide)
        replies.decide = lambda *_: {"action": "handoff", "text": "", "status": "", "snooze_days": 0, "why": "test"}
        rows = [{"vin": f"V{i}", "model": model, "msrp": msrp, "arrived": "10/05/2026", "kind": "new"}
                for i, (model, msrp) in enumerate([("Bronco", "$41,000"), ("Bronco", "$39,500"), ("Bronco Sport", "$31,000"),
                                                   ("Escape", "$30,100")])]
        self.assertEqual(inventory.store_site(self.con, "Bay Ridge", rows, TODAY), 4)
        inventory.build_offers(self.con, TODAY)

    def lead(self, **fields):
        lead_id = db.upsert_lead(self.con, "+17185550009", name="Doe, Sam", first_name="Sam", date_in=TODAY.isoformat(), **fields)
        return dict(self.con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    def test_lead_gets_their_own_model_first_then_something_new_each_day(self):
        lead = self.lead(vehicle="2026 Ford Bronco Sport Big Bend")
        bodies = []
        for day in (0, 2, 4):  # the first three offer days
            engine.queue_daily(self.con, self.cfg, TODAY + timedelta(days=day))
            bodies.append(self.con.execute("SELECT body FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertIn("new Ford Bronco Sport options", bodies[0])
        self.assertIn("the 2026 Ford Bronco Sport Big Bend", bodies[1])  # then a follow-up about their own vehicle
        self.assertEqual(len(set(bodies)), 3)
        self.assertTrue(all(len(body) <= 160 and body.isascii() for body in bodies), bodies)
        self.assertEqual(lead["first_name"], "Sam")

    def test_rescan_keeps_switched_off_offers_and_drops_gone_models(self):
        self.con.execute("UPDATE offers SET active=0 WHERE auto_key='model:Bronco'")
        rows = [{"vin": "V9", "model": "Bronco", "msrp": "$38,000", "arrived": "", "kind": "new"}]
        inventory.store_site(self.con, "Bay Ridge", rows, TODAY)
        inventory.build_offers(self.con, TODAY)
        bronco = self.con.execute("SELECT body,active FROM offers WHERE auto_key='model:Bronco'").fetchone()
        self.assertIn("new Ford Bronco options", bronco["body"])
        self.assertFalse(any(ch.isdigit() for ch in bronco["body"]))  # no stock counts
        self.assertEqual(bronco["active"], 0)
        self.assertIsNone(self.con.execute("SELECT 1 FROM offers WHERE auto_key='model:Escape'").fetchone())

    def yes_to_options(self, reply="Sure. Let's see what you have"):
        self.lead(vehicle="2026 Ford Bronco Sport")
        sender = FakeSender()
        engine.queue_daily(self.con, self.cfg, TODAY)
        engine.send_queued(self.con, sender, self.cfg, NOON, pause=0)
        self.assertIn("options", sender.sent[0][1])
        return sender, engine.handle_inbound(self.con, sender, self.cfg, "7185550009", reply,
                                             lambda *_: (_ for _ in ()).throw(AssertionError("no generic reply")))

    def test_yes_to_options_sends_one_matched_vehicle_with_its_link(self):
        self.con.execute("UPDATE inventory SET status='in_transit', trim='Badlands&reg;' WHERE vin='V2'")
        sender, outcome = self.yes_to_options()
        self.assertEqual(outcome, "match")
        self.con.execute("UPDATE leads SET opp_id='77'")
        self.assertEqual(matching.store_notes(self.con, {"77": "Comment: wants a black one", "999": "someone else"}), 1)
        waiting = matching.waiting(self.con, self.cfg)
        self.assertEqual([c["model"] for c in waiting[0]["candidates"]], ["Bronco Sport"])  # the model they were offered
        prompts = []
        lines = matching.work(self.con, sender, self.cfg, lambda prompt: prompts.append(prompt) or '{"vin": "V2", "why": "fits"}')
        self.assertIn("sent", lines[0])
        self.assertIn("wants a black one", prompts[0])  # the notes the morning read brought in
        text = sender.sent[-1][1]
        self.assertIn("Ford Bronco Sport Badlands. http", text)
        self.assertNotIn("Glenwood", text)
        self.assertNotIn("- Jordan", text)  # mid-thread: no signature
        self.assertNotIn("$", text)
        self.assertNotIn("31,000", text)
        self.assertEqual(matching.waiting(self.con, self.cfg), [])
        self.assertEqual(self.con.execute("SELECT reason FROM messages WHERE needs_review=1").fetchone()[0],
                         "vehicle sent: follow up")  # still in the inbox for a personal follow-up

    def test_how_much_is_deflected_never_priced(self):
        redirect = lambda *_: {"action": "reply", "status": "", "snooze_days": 0, "why": "price ask",
                               "text": "Depends a lot on the trim and how you'd use it. What matters most to you in it?"}
        sender, _ = self.yes_to_options("How much")
        sender.sent.clear()
        self.assertEqual(engine.handle_inbound(self.con, sender, self.cfg, "+17185550009", "How much", decide=redirect), "replied")
        self.assertEqual(sender.sent[-1][1], "Depends a lot on the trim and how you'd use it. What matters most to you in it?")
        quote = lambda *_: {"action": "reply", "status": "", "snooze_days": 0, "why": "x",
                            "text": "New Broncos run about 40k to 96k depending on trim."}
        self.assertEqual(engine.handle_inbound(self.con, sender, self.cfg, "+17185550009", "how much tho", decide=quote), "review")
        self.assertEqual(len(sender.sent), 1)  # a figure never goes out, MSRP or not

    def test_a_pick_outside_the_list_is_left_for_a_person(self):
        sender, _ = self.yes_to_options("Yes")
        matching.work(self.con, sender, self.cfg, lambda _: '{"vin": "NOT-IN-STOCK"}')
        self.assertEqual(len(sender.sent), 1)  # only the original offer
        self.assertEqual(matching.waiting(self.con, self.cfg), [])
        reason = self.con.execute("SELECT reason FROM messages WHERE needs_review=1").fetchone()[0]
        self.assertTrue(reason.startswith("no match sent"), reason)

    def test_morning_read_takes_new_records_first_then_week_old_ones(self):
        base = "https://crm.connectcdk.com/evo2/fresh/elead-v45/elead_track/newprospects/OpptyDetails.aspx?lDID="
        rows = [{"_opp": str(n), "_url": f"{base}{n}&lPID=1", "Name": f"Doe{n}, Pat"} for n in (1, 2, 3)]
        rows.append({"_opp": "4", "_url": "https://evil.example/x", "Name": "Bad, Link"})
        opened = []
        real = matching.read_lead_record
        matching.read_lead_record = lambda _, url: opened.append(url) or "Cell #: 718 555 0101\nComment: likes red"
        matching.pause = lambda *_: None
        self.addCleanup(setattr, matching, "read_lead_record", real)
        self.addCleanup(setattr, matching, "pause", cdk_sync.pause)
        with tempfile.TemporaryDirectory() as folder:
            seen = Path(folder) / "seen.json"
            seen.write_text(json.dumps({"1": (TODAY - timedelta(days=2)).isoformat(), "2": (TODAY - timedelta(days=9)).isoformat()}))
            batches = []
            self.assertEqual(matching.read_notes(None, rows, seen, TODAY, self.cfg, batches.append, batch=1), 2)
            notes = {k: v for b in batches for k, v in b.items()}
            self.assertEqual(sorted(notes), ["2", "3"])  # 1 was read two days ago; 4 is not a CDK record
            self.assertEqual(len(batches), 2)  # delivered as it goes
            self.assertEqual(notes["3"], {"text": "Comment: likes red", "excluded": False, "last_contact": None})  # scrubbed before it leaves the PC
            self.assertEqual(matching.read_notes(None, rows, seen, TODAY, self.cfg, batches.append), 0)  # nothing due again today
        self.assertEqual(len(opened), 2)

    def test_a_stopped_morning_read_keeps_what_it_delivered(self):
        base = "https://crm.connectcdk.com/evo2/fresh/elead-v45/elead_track/newprospects/OpptyDetails.aspx?lDID="
        rows = [{"_opp": str(n), "_url": f"{base}{n}&lPID=1", "Name": "Doe, Pat"} for n in range(1, 6)]
        real = matching.read_lead_record
        calls = []
        def record(_, url):
            calls.append(url)
            if len(calls) == 4:
                raise KeyboardInterrupt  # stopped partway through
            return "Comment: fine"
        matching.read_lead_record, matching.pause = record, (lambda *_: None)
        self.addCleanup(setattr, matching, "read_lead_record", real)
        self.addCleanup(setattr, matching, "pause", cdk_sync.pause)
        with tempfile.TemporaryDirectory() as folder:
            seen = Path(folder) / "seen.json"
            delivered = []
            with self.assertRaises(KeyboardInterrupt):
                matching.read_notes(None, rows, seen, TODAY, self.cfg, delivered.append, batch=2)
            self.assertEqual(sum(map(len, delivered)), 2)  # the first full batch made it
            calls.clear()
            self.assertEqual(matching.read_notes(None, rows, seen, TODAY, self.cfg, delivered.append, batch=2), 3)
            self.assertEqual(len(calls), 3)  # carried on with the three not yet delivered

    def test_cdk_record_is_scrubbed_and_only_lead_records_open(self):
        record = ("Cell #: (718) 555-0101\nAddress: 12 Main St\nBirthday: 1/2/1980\nPreferred Email: pat@example.com\n"
                  "Comment: wants a black Bronco Sport under 35k, call 718 555 0102 after 6\nTrade: 2018 Escape")
        clean = matching.scrub(record + "\nLast contact: Doe, Pat", "Doe")
        for secret in ("555-0101", "Main St", "1980", "example.com", "555 0102", "Doe"):
            self.assertNotIn(secret, clean)
        self.assertIn("black Bronco Sport", clean)
        self.assertIn("2018 Escape", clean)
        base = "https://crm.connectcdk.com/evo2/fresh/elead-v45/elead_track/newprospects/OpptyDetails.aspx"
        self.assertTrue(cdk_sync.lead_record_url(base + "?lDID=123&lPID=456&Loc=1"))
        for bad in (base + "?lDID=123&delete=1", "https://evil.example/newprospects/OpptyDetails.aspx?lDID=1",
                    "http://crm.connectcdk.com/evo2/newprospects/OpptyDetails.aspx?lDID=1", base.replace("OpptyDetails", "Save") + "?lDID=1"):
            self.assertFalse(cdk_sync.lead_record_url(bad), bad)

    def test_empty_read_keeps_yesterdays_inventory(self):
        self.assertEqual(inventory.store_site(self.con, "Bay Ridge", [], TODAY), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM inventory").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
