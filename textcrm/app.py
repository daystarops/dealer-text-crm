"""The engine's only doors: the texting provider's webhook and the PC's daily feed.

Conversations are worked in Quo itself (or Telnyx's portal); there are no pages here and no customer data
is served. Quo calls /webhooks/quo/<webhook_token> for texts in (message.received) and texts out
(message.delivered), so a reply you send from Quo is seen and the bot steps back.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import bdc, db, engine, feed
from .sender import parse_quo_outgoing, parse_quo_webhook, parse_telnyx_webhook


def make_handler(con_factory, sender, cfg):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status: int, body: bytes = b"", content_type="text/plain"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200, b"textcrm is running") if urlparse(self.path).path == "/" else self.reply(404, b"not found")

        def do_POST(self):
            path = urlparse(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hook = re.fullmatch(r"/webhooks/(telnyx|quo)/([\w-]+)", path)
            fed = re.fullmatch(r"/feed/([\w-]+)", path)
            if not hook and not fed:
                return self.reply(404, b"not found")
            given, token = (hook[2], cfg["webhook_token"]) if hook else (fed[1], cfg["feed_token"])
            if not token or not hmac.compare_digest(given, token):
                return self.reply(403, b"forbidden")
            payload = json.loads(raw or b"{}")
            if fed:
                con = con_factory()
                try:
                    return self.reply(200, json.dumps(feed.apply(con, cfg, payload)).encode(), "application/json")
                finally:
                    con.close()
            # Answer the provider at once and work in the background: a drafted reply can take a minute, and a
            # webhook that times out is sent again.
            self.reply(200, b"ok")
            threading.Thread(target=handle_event, args=(con_factory, sender, cfg, hook[1], payload), daemon=True).start()

    return Handler


_events = threading.Lock()  # one event at a time, in arrival order


def handle_event(con_factory, sender, cfg, provider: str, payload: dict) -> None:
    with _events:
        con = con_factory()
        try:
            inbound = (parse_quo_webhook if provider == "quo" else parse_telnyx_webhook)(payload)
            if inbound:
                data = payload.get("data") or {}
                message_id = str((data.get("object") or data.get("payload") or {}).get("id") or "")
                engine.handle_inbound(con, sender, cfg, *inbound, message_id=message_id)
            outgoing = parse_quo_outgoing(payload) if provider == "quo" else None
            if outgoing and not bdc.is_staff(db.normalize_phone(outgoing[0]), cfg):  # updates to the BDC are not lead texts
                engine.record_outgoing(con, *outgoing)
        except Exception as error:  # keep the server up; the event is in the log
            print(f"webhook event failed: {type(error).__name__}: {error}", flush=True)
        finally:
            con.close()


def serve(con_factory, sender, cfg) -> None:
    server = ThreadingHTTPServer((cfg["http_host"], cfg["http_port"]), make_handler(con_factory, sender, cfg))
    print(f"textcrm engine: http://{cfg['http_host']}:{cfg['http_port']}  (sender: {sender.name})", flush=True)
    server.serve_forever()
