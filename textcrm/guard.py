"""Guardrails: what an inbound text needs, and what an automatic reply may say."""
from __future__ import annotations

import re

OPT_OUT = re.compile(
    r"(?i)^\s*(?:stop|stopall|unsubscribe|cancel|end|quit|opt.?out)\s*[.!]*\s*$"
    r"|\b(?:do\s?n[o']t (?:text|contact|message)|stop (?:texting|messaging|contacting)|remove me|take me off|wrong number)\b"
)

WRONG_NUMBER = re.compile(r"(?i)\bwrong (?:number|person)\b")

# Anything about money, credit, trade value or stock is answered by a person.
REVIEW_TOPICS = {
    "payment or price": re.compile(
        r"(?i)\$|/\s?mo\b|\b(?:how much|pric\w*|costs?|payments?|per month|a month|monthly|otd|out the door|"
        r"best (?:deal|offer)|discount\w*|rebate\w*|incentive\w*|specials?|msrp|quote\w*|numbers|down ?payment|"
        r"money down|fees?|tax(?:es)?)\b"
    ),
    "financing or credit": re.compile(
        r"(?i)\b(?:financ\w*|apr|interest|rate|credit|approv\w*|pre.?qualif\w*|co.?sign\w*|loan|lender|"
        r"lease (?:deal|special|price|payment)|buy ?out|payoff|residual|miles? (?:over|allowance))\b"
    ),
    "trade-in value": re.compile(r"(?i)\b(?:trade\w*|apprais\w*|worth|value|equity|upside down)\b"),
    "availability": re.compile(r"(?i)\b(?:available|in stock|still (?:have|there|for sale)|do you have|on the lot|hold (?:it|the))\b"),
    "complaint or legal": re.compile(r"(?i)\b(?:lawyer|attorney|sue|lawsuit|report(?:ing)? you|harass\w*|scam|complain\w*|bbb|manager)\b"),
}

# Asking for a call, a visit or a time. The bot may still answer, but a person must see it.
WANTS_CONTACT = re.compile(
    r"(?i)\b(?:call me|give me a call|call (?:me )?back|(?:can|could|pls|please) (?:you |someone |u )?call|phone me|"
    r"reach me|my (?:cell|number) is|appointment|appt|schedule|book (?:a|an|me)|come (?:in|by|down|over)|stop by|"
    r"swing by|visit|test.?drive)\b"
)
# A day or time is usually an answer to "what day and time works?", unless they are asking about store hours.
NAMES_A_TIME = re.compile(
    r"(?i)\b(?:now|today|tonight|tomorrow|this (?:week|weekend)|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"\d{1,2}\s?(?:am|pm)|\d{1,2}:\d{2})\b"
)
HOURS_QUESTION = re.compile(r"(?i)\b(?:open|close|closing|hours)\b")

# Bought elsewhere, not interested, or a plain no: stop the daily texts for this lead.
DECLINED = re.compile(
    r"(?i)^\s*(?:no|nope|nah|no,? thanks?|no,? thank you|not interested|i'?m good|all set|pass)\s*[.!]*\s*$"
    r"|\b(?:not interested|no longer (?:looking|interested|in the market|need\w*)|not (?:looking|in the market|in need)"
    r"|already (?:bought|purchased|got|have|found|leased|picked)|i (?:just )?(?:bought|purchased|leased|picked up) (?:a|one|my|another|it)"
    r"|i (?:just )?got (?:a|one|my|another|it|mustang|bronco|explorer|f.?150|escape|maverick|ranger|expedition)\b"
    r"|went (?:with|elsewhere)|found (?:a|one|another) (?:car|truck|suv|elsewhere)?|decided (?:not|to wait))"
)

# What a salesperson writes in CDK when a lead is done: out of market, moved, bought, do not contact. Any of it
# anywhere in the record stops automatic texts; a wrong stop costs a text, a wrong send costs the customer.
NOTES_STOP = re.compile(
    r"(?i)\b(?:out of (?:the )?(?:market|area|state|town|country)|not in (?:the )?market|no longer in (?:the )?market"
    r"|moved (?:out|away|to)|relocat\w*|lives? (?:out of state|too far)|too far"
    r"|(?:bought|purchased|leased|got one) (?:elsewhere|somewhere else|at another|from another|already)"
    r"|already (?:bought|purchased|leased)|went (?:elsewhere|with another)|lost (?:the )?deal|lost to"
    r"|do not (?:contact|call|text)|don'?t (?:contact|call|text)|\bdnc\b|stop (?:texting|calling|contacting)"
    r"|wrong (?:number|person)|not interested|no longer interested|deceased)\b"
)
# Page furniture on every CDK record that would otherwise match: the status dropdown lists "Bought Elsewhere"
# and "Not In Market" as options side by side, and contact flags read "Do Not Call: False".
RECORD_LABELS = re.compile(r"Bought Elsewhere\s+Not In Market|(?i:do not (?:call|text|contact|email|mail)\s*:\s*(?:false|no)\b)")


def notes_say_stop(notes: str | None) -> bool:
    return bool(NOTES_STOP.search(RECORD_LABELS.sub(" ", notes or "")))


# A no for now, not for good: "not interested in buying one yet", "not in need at the moment but let me check
# with a few people". These stay on follow-up, after a short break.
SOFT_NO = re.compile(
    r"(?i)\b(?:yet|at the moment|at this (?:time|moment|point)|right now|for now|just now|currently|not now|not ready"
    r"|maybe later|later on|down the road|in the future|let me (?:check|think|see|talk|ask)|i'?ll (?:let you know|reach out))\b"
)


def soft_no(text: str) -> bool:
    return bool(SOFT_NO.search(text or ""))


# Bought (or leased) somewhere else. They get one short, warm close.
BOUGHT_ELSEWHERE = re.compile(
    r"(?i)\b(?:already (?:bought|purchased|got|leased|picked up)|i (?:just )?(?:bought|purchased|leased|picked up) (?:a|one|my|another|it)\b"
    r"|i (?:just )?got (?:a|one|my|another|it|mustang|bronco|explorer|f.?150|escape|maverick|ranger|expedition)\b"
    r"|went (?:with|elsewhere)|found (?:a|one|another) (?:car|truck|suv|elsewhere)?"
    r"|(?:already |just )?traded (?:it |mine |my \w+ )?in for|(?:got|picked up|bought|leased) (?:a|my) new (?:car|truck|suv|van|one|\w+))"
)

# "Get in touch with me in a year", "I have one more year", "maybe in 3 months": how long to leave them be.
LATER_YEAR = re.compile(r"(?i)\b(?:one more|another|in a|next|a|1) year\b")
LATER_MONTHS = re.compile(r"(?i)\b(?:in |another |for )?(\d{1,2}|a few|a couple(?: of)?|couple(?: of)?|few|six|three|two|four)\s+months?\b")
MONTH_WORDS = {"a few": 3, "few": 3, "a couple": 2, "a couple of": 2, "couple": 2, "couple of": 2, "two": 2, "three": 3,
               "four": 4, "six": 6}

# A tapback: '👍 to "..."' (as Quo shows it) or iPhone's 'Liked "..."'.
REACTION = re.compile(r"""(?i)^\W*(?:\S{1,8}\s+to\s+["“]|(?:liked|loved|disliked|laughed at|emphasized|questioned|reacted \S+ to)\s+["“])""")

# "Ok", "no problem", "thanks": they are signing off.
ACKNOWLEDGEMENT = re.compile(
    r"(?i)^\W*(?:ok(?:ay)?|k|kk|no problem|np|no worries|thanks?(?: you)?|thank you|thx|ty|cool|great|got it|"
    r"sounds good|will do|perfect|alright|all right|appreciate it)(?:\W+(?:thanks?(?: you)?|thx|ty|again))*\W*$"
)

# Bought from us: "got it from you guys", "bought it with you", "picked it up at Premier".
BOUGHT_HERE = re.compile(
    r"(?i)\b(?:bought|purchased|got|picked (?:it |one )?up|leased|took)\b[^.?!]{0,30}?\b(?:from|with|at|through|here)\b\s*"
    r"(?:you|u|y'?all|you guys|your (?:store|dealership|place|team)|premier|bay ridge|glenwood|$|\W)"
    r"|\byou (?:guys )?sold (?:me|it|one)\b"
)

# Money talk the BDC should hear about: payments, lease, down payment, credit, financing.
FINANCE_TALK = re.compile(r"(?i)\b(?:leas\w*|payments?|a month|/\s?mo|monthly|down|financ\w*|credit|apr|interest|loan)\b")

# A yes to "want me to send you a few options?".
AFFIRMATIVE = re.compile(
    r"(?i)^\W*(?:yes|yeah|yea|yep|ya|sure|ok|okay|k|please|absolutely|definitely|of course|why not|go ahead|sounds good"
    r"|send (?:them|it|me|over|please)?|lemme see|let me see|let'?s see|i'?d like (?:that|to see)|show me|interested)\b"
)

# An automatic reply may not make commitments, whatever the model was told.
OUTBOUND_BLOCK = re.compile(
    r"(?i)\$|\d\s?%|\b(?:guarantee\w*|approved|approval|payments?|per month|monthly|apr|interest rates?|rates?|credit|"
    r"financ\w*|trade (?:value|in value)|worth|price|discount|rebate|in stock|available|hold it for you)\b"
)

# A decided reply may say "price" or "finance manager" ("I'll have my finance manager take a look with you"), but
# never a figure, a rate, an approval, a discount, a trade value or a stock promise. You do not give prices by text.
DECIDED_BLOCK = re.compile(
    r"(?i)\$|\d\s?%|\d{1,3}\s?k\b|\b(?:msrp|sticker|guarantee\w*|approved|approval|per month|/mo|monthly|apr|"
    r"interest|rates?|trade.?in value|trade value|worth|discount\w*|rebate\w*|out the door|otd|"
    r"in stock|available|hold it for you)\b"
)

# A text of yours that puts numbers on the deal: "$575", "15k down", "about 420 a month". Not years, addresses or times.
GAVE_NUMBERS = re.compile(
    r"(?i)\$\s?\d|\b\d{1,3}(?:\.\d)?\s?k\b(?=\s*(?:down|cash|out|$|[.,!?]))"
    r"|(?<![\d:/])\b(?!(?:19|20)\d\d\b)\d{3,4}\b(?=\s*(?:a month|/\s?mo|per month|monthly|a mo\b))"
    r"|\b(?:payments?|down)\b[^.?!\n]{0,25}?\b(?:is|of|at|about|around|be)\s+(?!(?:19|20)\d\d\b)\d{3,5}\b"
)


def gave_numbers(text: str) -> bool:
    return bool(GAVE_NUMBERS.search(text or ""))


# Money-shaped figures: "$575", "15k", "1,200", "420". Not model names (F-150), years, or times ("at 1").
FIGURE = re.compile(r"(?i)(?<![\w\-:/])\$?\s?(\d{1,3}(?:,\d{3})+|\d{3,5}|\d{1,3}(?:\.\d)?\s?k)\b")


def figures(text: str) -> set[str]:
    found = set()
    for raw in FIGURE.findall(text or ""):
        value = raw.replace(",", "").replace(" ", "").lower()
        if re.fullmatch(r"(?:19|20)\d\d", value):
            continue
        found.add(str(int(float(value[:-1]) * 1000)) if value.endswith("k") else value)
    return found


# Repeating your own numbers is fine; these never are, whoever said what before.
NEVER_SAID = re.compile(
    r"(?i)\d\s?%|\b(?:guarantee\w*|approved|approval|apr|interest|rates?|discount\w*|rebate\w*|in stock|available|"
    r"hold it for you)\b"
)


SMS_LIMIT = 320


def buying_signal(text: str) -> bool:
    """A high-intent text: price, payment, financing or lease, trade, stock, or a time to come in or a call. (A plain
    "yes" or "ok" is not one; a recent reply counts on its own.) Lead-scoring research finds actions like these
    predict a purchase better than who the lead is."""
    text = text or ""
    return bool(any(pattern.search(text) for topic, pattern in REVIEW_TOPICS.items() if topic != "complaint or legal")
                or FINANCE_TALK.search(text) or WANTS_CONTACT.search(text) or NAMES_A_TIME.search(text))


def classify_inbound(text: str) -> tuple[str, str]:
    """Return ('opt_out' | 'review' | 'ok', reason)."""
    if OPT_OUT.search(text or ""):
        return "opt_out", "asked to stop"
    for topic, pattern in REVIEW_TOPICS.items():
        if pattern.search(text or ""):
            return "review", topic
    return "ok", ""


def wants_contact(text: str) -> bool:
    text = text or ""
    when = NAMES_A_TIME.search(text) and not HOURS_QUESTION.search(text) and not DECLINED.search(text)
    return bool(WANTS_CONTACT.search(text) or when)


def declined(text: str) -> bool:
    return bool(DECLINED.search(text or ""))


def bought_from_us(text: str) -> bool:
    return bool(BOUGHT_HERE.search(text or ""))


def bought_elsewhere(text: str) -> bool:
    return bool(BOUGHT_ELSEWHERE.search(text or ""))


def later_days(text: str) -> int:
    """Days to hold offers when they say when to come back, a little early so you reach them first; else 0."""
    if LATER_YEAR.search(text or ""):
        return 330
    months = LATER_MONTHS.search(text or "")
    if months:
        count = int(months[1]) if months[1].isdigit() else MONTH_WORDS.get(months[1].lower(), 0)
        return max(0, count * 30 - 14)
    return 0


def reaction(text: str) -> bool:
    """A tapback on one of our texts: '👍 to "..."', 'Liked "..."', 'Reacted ❤️ to "..."'."""
    return bool(REACTION.search(text or ""))


def acknowledgement(text: str) -> bool:
    return bool(ACKNOWLEDGEMENT.search(text or ""))


def affirmative(text: str) -> bool:
    return bool(AFFIRMATIVE.search(text or "")) and not declined(text)


# Repeating their feelings back ("Sounds like you want...") reads like a script, not a person texting.
MIRRORING = re.compile(r"(?i)\b(?:it )?sounds like\b|\bi hear you\b|\bi understand\b")
MIRROR_OPENER = re.compile(r"(?i)^\s*(?:(?:it )?sounds like|i hear you|i understand)\b[^.!?]*[.!?]+\s*")


def plain_voice(text: str) -> str:
    """Drop an opening "Sounds like..." / "I understand..." sentence; the rest of the text stands on its own."""
    rest = MIRROR_OPENER.sub("", text or "", count=1)
    return rest[:1].upper() + rest[1:] if rest.strip() else text


def check_decided_reply(text: str, quoted: set[str] | None = None) -> str:
    """Return why a reply Codex decided on must not go out, or ''. `quoted` holds the figures you already texted
    them: a reply may repeat those exactly, and no others."""
    if not text.strip():
        return "empty reply"
    if len(text) > SMS_LIMIT:
        return "reply too long"
    if MIRRORING.search(text):
        return "reply mirrors their feelings back"
    said = figures(text)
    if quoted and said and said <= quoted:
        return "reply makes a rate, approval or availability statement" if NEVER_SAID.search(text) else ""
    if DECIDED_BLOCK.search(text):
        return "reply makes a payment, finance or availability statement"
    return ""


def check_auto_reply(text: str) -> str:
    """Return why an automatic reply must be held, or '' if it may go out."""
    if not text.strip():
        return "empty reply"
    if len(text) > SMS_LIMIT:
        return "reply too long"
    if MIRRORING.search(text):
        return "reply mirrors their feelings back"
    if OUTBOUND_BLOCK.search(text):
        return "reply makes a pricing, finance or availability statement"
    return ""
