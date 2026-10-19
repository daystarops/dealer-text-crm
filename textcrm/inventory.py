"""Daily new-vehicle scan of both stores' websites, turned into offers.

The sites refuse plain scripts, so pages are read in a tab of your own Chrome, the way you would
browse them: one listing page at a time, at human pace.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import db
from .cdk_sync import Browser, SyncError, Tab, pause

LISTING = "/new-inventory/index.htm"

# The listing page publishes its vehicles in DDC.dataLayer; the visible count tells us when to stop.
# %s is the expected ?start= value, so a slow navigation cannot hand back the previous page.
JS_PAGE = r"""(()=>{const dl=(window.DDC||{}).dataLayer;if(!dl||!dl.vehicles||document.readyState!=='complete')return null;
  if((new URLSearchParams(location.search).get('start')||'0')!==%s)return null;
  const total=(document.body.innerText.match(/(\d+)\s+Vehicles/i)||[])[1];if(total===undefined)return null;
  return {path:location.pathname,total:+total,rows:dl.vehicles.map(v=>({vin:v.vin,stock:v.stockNumber,
    year:String(v.year||v.modelYear||''),make:v.make,model:v.model,trim:v.trim,msrp:(v.pricing||{}).msrp||'',status:v.status,
    arrived:v.inventoryDate,link:v.link,kind:v.inventoryType||v.newOrUsed}))}})()"""


def read_site(browser: Browser, base: str) -> list[dict]:
    tab = Tab(browser, base + LISTING, settle=4)
    try:
        rows: dict[str, dict] = {}
        start = 0
        for _ in range(40):
            page = tab.wait_for(JS_PAGE % json.dumps(str(start)), f"the inventory page at {base}", timeout=60)
            fresh = [row for row in page["rows"] if row.get("vin") and row["vin"] not in rows]
            rows.update((row["vin"], row) for row in fresh)
            start += len(page["rows"])
            if not fresh or start >= page["total"]:
                # A short read must not replace yesterday's full listing.
                if len(rows) < 0.9 * page["total"]:
                    raise SyncError(f"{base} lists {page['total']} vehicles but only {len(rows)} were read")
                break
            pause(2.5, 5)
            tab.call("Page.navigate", url=f"{base}{page['path']}?start={start}")
            pause(2.5, 4)
        return list(rows.values())
    finally:
        tab.close()


def money(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", (text or "").split(".")[0])
    return int(digits) if digits else None


def store_site(con, site: str, rows: list[dict], today: date) -> int:
    """Replace one site's listing. An empty read keeps yesterday's rows rather than wiping them."""
    rows = [row for row in rows if (row.get("kind") or "new").lower() == "new" and row.get("model")]
    if not rows:
        return 0
    con.execute("DELETE FROM inventory WHERE site=?", (site,))
    for row in rows:
        arrived = None
        try:
            arrived = datetime.strptime(row.get("arrived") or "", "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
        con.execute("INSERT OR REPLACE INTO inventory(vin,site,stock,year,model,trim,msrp,status,arrived,link,seen)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (row["vin"], site, row.get("stock"), row.get("year"), row["model"].strip(), row.get("trim"),
                     money(row.get("msrp")), row.get("status"), arrived, row.get("link"), today.isoformat()))
    con.commit()
    return len(rows)


# Follow-ups that need no inventory. They are written as one person checking in, and kept short
# enough to fit a single text segment (160 characters) for ordinary names and vehicles.
FOLLOW_UPS = {
    "follow:1": ("{first}, still thinking about {the_vehicle}? I can check what we have at both stores today. - {rep}", "all"),
    "follow:2": ("{first}, it's {rep} at Premier Ford. Any questions on {the_vehicle} I can answer for you?", "all"),
    "follow:3": ("{first}, want to stop by this week about {the_vehicle}? Tell me a day and time and I'll set it up. - {rep}", "all"),
    "follow:4": ("{first}, checking in on {the_vehicle}. Is the timing still right for you? - {rep}", "all"),
    "lease:follow": ("{first}, the lease on your {lease_vehicle} ends soon. Want me to go over your options before it does? - {rep}", "lease"),
}


def build_offers(con, today: date) -> int:
    """Rewrite the automatic offers: the follow-ups above, plus, from today's inventory, one per model for
    shoppers, one per model for lease customers, and two general ones. Offers you typed yourself, or
    switched off, are left alone."""
    wanted: dict[str, tuple[str, str, str]] = {key: (body, audience, "") for key, (body, audience) in FOLLOW_UPS.items()}
    models = con.execute("SELECT model, count(*) AS n FROM inventory GROUP BY model ORDER BY n DESC").fetchall()
    # No counts or stock claims: a number in a text reads as a promise, and stock changes by the hour.
    for row in models:
        wanted[f"model:{row['model']}"] = (
            f"{{first}}, want me to send you a few new Ford {row['model']} options from our two Brooklyn stores? - {{rep}}",
            "all", row["model"])
        wanted[f"lease:{row['model']}"] = (
            f"{{first}}, with your {{lease_vehicle}} lease coming up, want to see a few new Ford {row['model']} options first? - {{rep}}",
            "lease", row["model"])
    week = (today - timedelta(days=7)).isoformat()
    new = con.execute("SELECT model, count(*) AS n FROM inventory WHERE arrived>=? GROUP BY model ORDER BY n DESC", (week,)).fetchall()
    if new:
        names = ", ".join(row["model"] for row in new[:3])
        wanted["arrivals"] = (f"{{first}}, new Fords came in this week, including {names}. "
                              "Anything you want me to check on? - {rep}", "all", "")
    if models:
        wanted["total"] = ("{first}, checking in. What are you looking for? I can check both our Brooklyn stores for you. - {rep}",
                           "all", "")

    existing = {row["auto_key"]: row["id"] for row in con.execute("SELECT id,auto_key FROM offers WHERE auto_key IS NOT NULL")}
    for key, (body, audience, model) in wanted.items():
        if key in existing:
            con.execute("UPDATE offers SET body=? WHERE id=?", (body, existing[key]))
        else:
            con.execute("INSERT INTO offers(body,audience,model,auto_key,created_at) VALUES(?,?,?,?,?)",
                        (body, audience, model, key, db.now()))
    for key, offer_id in existing.items():
        if key not in wanted:  # that model is no longer listed
            con.execute("DELETE FROM offers WHERE id=?", (offer_id,))
    con.commit()
    return len(wanted)
