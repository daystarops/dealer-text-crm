"""SMS senders. FakeSender delivers nowhere; TelnyxSender and QuoSender call those providers' APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SendError(RuntimeError):
    pass


class FakeSender:
    name = "fake"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> str:
        self.sent.append((to, body))
        return "fake-" + uuid.uuid4().hex[:12]


class TelnyxSender:
    name = "telnyx"

    def __init__(self, from_number: str, api_key: str | None = None):
        self.from_number = from_number
        self.api_key = api_key or os.environ.get("TELNYX_API_KEY", "")
        if not self.from_number or not self.api_key:
            raise SendError("Telnyx needs telnyx_from in private/textcrm.json and TELNYX_API_KEY in the environment")

    def send(self, to: str, body: str) -> str:
        request = urllib.request.Request(
            "https://api.telnyx.com/v2/messages",
            data=json.dumps({"from": self.from_number, "to": to, "text": body}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)["data"]["id"]
        except urllib.error.HTTPError as error:
            raise SendError(f"Telnyx {error.code}: {error.read().decode(errors='replace')[:300]}") from error
        except (urllib.error.URLError, KeyError, ValueError) as error:
            raise SendError(f"Telnyx send failed: {error}") from error


def parse_telnyx_webhook(payload: dict) -> tuple[str, str] | None:
    """Return (from_phone, text) for an inbound message event, else None."""
    data = payload.get("data") or {}
    if data.get("event_type") != "message.received":
        return None
    message = data.get("payload") or {}
    phone = (message.get("from") or {}).get("phone_number", "")
    return (phone, message.get("text") or "") if phone else None


class QuoSender:
    """Quo (formerly OpenPhone). The number must have carrier registration approved and prepaid credits."""
    name = "quo"

    def __init__(self, from_number: str, api_key: str | None = None):
        self.from_number = from_number
        self.api_key = api_key or os.environ.get("QUO_API_KEY", "")
        if not self.from_number or not self.api_key:
            raise SendError("Quo needs quo_from in private/textcrm.json and QUO_API_KEY in the environment")

    def last_message(self, to: str) -> dict | None:
        return quo_last_message(to, self.from_number, self.api_key)

    def send(self, to: str, body: str) -> str:
        request = urllib.request.Request(
            "https://api.quo.com/v1/messages",
            data=json.dumps({"content": body, "from": self.from_number, "to": [to]}).encode(),
            # Raw key, no "Bearer". Quo's edge rejects Python's default user agent (error 1010).
            headers={"Authorization": self.api_key, "Content-Type": "application/json", "User-Agent": "textcrm/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raise SendError(f"Quo {error.code}: {error.read().decode(errors='replace')[:300]}") from error
        except (urllib.error.URLError, ValueError) as error:
            raise SendError(f"Quo send failed: {error}") from error
        return str((reply.get("data") or {}).get("id") or "quo-" + uuid.uuid4().hex[:12])


def quo_last_message(phone: str, from_number: str, api_key: str | None = None) -> dict | None:
    """The newest message in Quo's thread with this customer, as Quo has it (the source of truth): who sent it,
    its ID and its text; {} for a thread with no messages; None if Quo cannot be asked."""
    api_key = api_key or os.environ.get("QUO_API_KEY", "")
    headers = {"Authorization": api_key, "User-Agent": "textcrm/1.0"}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.quo.com/v1/phone-numbers", headers=headers), timeout=30) as r:
            number_id = next((n["id"] for n in json.load(r)["data"] if n["number"] == from_number), None)
        if not number_id:
            return None
        query = urllib.parse.urlencode([("phoneNumberId", number_id), ("participants", phone), ("maxResults", "5")])
        with urllib.request.urlopen(urllib.request.Request(f"https://api.quo.com/v1/messages?{query}", headers=headers), timeout=30) as r:
            messages = json.load(r)["data"]
    except (urllib.error.URLError, ValueError, KeyError):
        return None
    if not messages:
        return {}  # a new thread
    last = max(messages, key=lambda m: m["createdAt"])
    return {"id": str(last.get("id") or ""), "direction": last.get("direction"), "text": last.get("text") or last.get("body") or ""}


def parse_quo_webhook(payload: dict) -> tuple[str, str] | None:
    """Return (from_phone, text) for an inbound message event, else None."""
    if payload.get("type") != "message.received":
        return None
    message = (payload.get("data") or {}).get("object") or {}
    phone = message.get("from") or ""
    return (phone, message.get("body") or message.get("text") or "") if phone else None


def parse_quo_outgoing(payload: dict) -> tuple[str, str, str] | None:
    """Return (to_phone, text, message_id) for a text sent from the Quo number (ours or yours), else None."""
    if payload.get("type") != "message.delivered":
        return None
    message = (payload.get("data") or {}).get("object") or {}
    if message.get("direction") not in (None, "outgoing"):
        return None
    to = message.get("to")
    phone = (to[0] if to else "") if isinstance(to, list) else (to or "")
    return (phone, message.get("body") or message.get("text") or "", str(message.get("id") or "")) if phone else None


def make_sender(cfg: dict):
    if cfg["sender"] == "telnyx":
        return TelnyxSender(cfg["telnyx_from"])
    if cfg["sender"] == "quo":
        return QuoSender(cfg["quo_from"])
    return FakeSender()
