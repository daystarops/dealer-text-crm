"""Quo contacts kept in step with CDK leads, so a conversation in Quo shows who it is: name, the vehicle
they asked about (as company), their salesperson (as role), and a link to their CDK record."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.quo.com/v1"


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": os.environ.get("QUO_API_KEY", ""), "User-Agent": "textcrm/1.0",
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def contact_for(lead: dict) -> dict:
    name = " ".join((lead["name"] or "").split())
    last, comma, first = name.partition(",")
    reps = [line.strip() for line in (lead["salesperson"] or "").splitlines() if line.strip()]
    fields = {"firstName": lead["first_name"] or (first.strip().title() if comma else name) or "Lead",
              "lastName": last.strip().title() if comma else None,
              "company": lead["vehicle"] or lead["lease_vehicle"] or None,
              "role": ", ".join(reps) or None,
              "phoneNumbers": [{"name": "mobile", "value": lead["phone"]}]}
    body = {"defaultFields": fields, "externalId": f"textcrm-{lead['id']}", "source": "textcrm"}
    if lead["opp_url"]:
        body["sourceUrl"] = lead["opp_url"]
    return body


def existing(external_id: str, send=call) -> str | None:
    """A contact already made for this lead (say, created just before a restart lost its ID)."""
    found = send("GET", "/contacts?" + urllib.parse.urlencode([("externalIds", external_id), ("maxResults", "1")]), None)
    return next((c["id"] for c in found.get("data") or [] if c.get("externalId") == external_id), None)


def sync_contacts(con, limit: int = 30, send=call) -> int:
    """Create or update up to `limit` Quo contacts whose CDK details changed. Returns how many were written."""
    written = 0
    for row in con.execute("SELECT * FROM leads WHERE name!='' ORDER BY id").fetchall():
        if written >= limit:
            break
        lead = dict(row)
        body = contact_for(lead)
        sig = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
        if lead["quo_contact_sig"] == sig:
            continue
        try:
            contact_id = lead["quo_contact_id"] or existing(body["externalId"], send)
            if contact_id:
                send("PATCH", f"/contacts/{contact_id}", body)
            else:
                contact_id = send("POST", "/contacts", body)["data"]["id"]
        except urllib.error.HTTPError as error:
            print(f"quo contact for lead {lead['id']} failed: {error}", flush=True)
            continue  # this one only; the next change to the lead tries again
        except (urllib.error.URLError, KeyError, ValueError) as error:
            print(f"quo contact sync paused: {error}", flush=True)
            break  # Quo unreachable: the rest wait for next minute
        con.execute("UPDATE leads SET quo_contact_id=?, quo_contact_sig=? WHERE id=?", (contact_id, sig, lead["id"]))
        con.commit()
        written += 1
    return written
