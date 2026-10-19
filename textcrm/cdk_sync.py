"""Read-only CDK sync over Chrome DevTools: recent Active leads and the Lease Retention Detail report.

Runs in your everyday Chrome, in its own tab, using the CDK login you already have there.
It clicks only menu links, the Days filter, Refresh and the report's GO button, at human pace.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import bdc, db

CRM_HOST = "crm.connectcdk.com"
CRM_URL = f"https://{CRM_HOST}/evo2/fresh/elead-v45/elead_track/index.aspx"

# Shared helpers injected ahead of every expression. frameFor() finds the same-origin CRM frame by page name.
JS_PRELUDE = r"""
const walk=(w,o=[])=>{for(let i=0;i<w.frames.length;i++){try{const c=w.frames[i];c.location.pathname;o.push(c);walk(c,o)}catch(e){}}return o};
const frameFor=p=>walk(window).find(w=>w.location.pathname.toLowerCase().includes(p.toLowerCase()));
const vis=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
const txt=e=>(e.innerText||e.textContent||'').trim().replace(/\s+/g,' ');
const clickOne=(els,what)=>{if(els.length<1)throw Error(what+' not found');els[0].click();return true};
"""

# A Prospects folder: (menu link text, Title parameter it opens). The grid is the same for all of them.
ACTIVE = ("Active", "Active")
# Customers who bought: the Sold folder, and Active's own sold sub-folders.
SOLD_FOLDERS = [("Sold", "Sold"), ("Sold Not Delivered", "Active - Sold Not Delivered"),
                ("Sold Deposit", "Active - Sold Deposit"), ("Pre-Sold", "Active - Pre-Sold")]

# Opens a folder from the menu: the link with this exact text whose target carries this exact Title.
JS_OPEN_FOLDER = r"""(()=>{const want=%s,title=%s;return clickOne([...document.querySelectorAll('a')].filter(a=>txt(a)===want&&
  ((a.getAttribute('onclick')||'').match(/Title=([^&']*)/)||[])[1]!==undefined&&
  decodeURIComponent(((a.getAttribute('onclick')||'').match(/Title=([^&']*)/)||[])[1].replace(/\+/g,' '))===title),want+' menu link')})()"""

# The grid's state, only once the frame shows the folder we asked for (its Title parameter), so a slow load
# never hands back the previous folder's rows.
JS_GRID_STATE = r"""(()=>{const w=frameFor('NewProspects/Prospects.aspx');if(!w||!w.document.body)return null;const d=w.document;
  const days=d.querySelector('input#slctDateSpan');const total=(d.body.innerText.match(/Search Result \((\d+)\)/)||[])[1];
  return {title:new URLSearchParams(w.location.search).get('Title')===%s,days:days?days.value:'',total:total===undefined?null:+total,
    rows:d.querySelectorAll('table.k-selectable tbody tr').length,
    busy:[...d.querySelectorAll('[aria-busy=true],.k-loading-mask')].filter(vis).length}})()"""

JS_OPEN_DAYS = r"""(()=>{const d=frameFor('NewProspects/Prospects.aspx').document;return clickOne([...d.querySelectorAll('input#slctDateSpan')].filter(vis),'Days filter')})()"""
JS_PICK_DAYS = r"""(()=>{const d=frameFor('NewProspects/Prospects.aspx').document;return clickOne([...d.querySelectorAll('button')].filter(b=>vis(b)&&txt(b)===%s),'Days option')})()"""
JS_REFRESH = r"""(()=>{const d=frameFor('NewProspects/Prospects.aspx').document;return clickOne([...d.querySelectorAll('a')].filter(a=>vis(a)&&a.getAttribute('href')==='javascript:RefreshGrid()'),'Refresh link')})()"""

# Rows are read with their header names so a reordered column cannot shift data.
JS_GRID_ROWS = r"""(()=>{const d=frameFor('NewProspects/Prospects.aspx').document;
  const heads=[...d.querySelectorAll('th')].map(txt);
  return [...d.querySelectorAll('table.k-selectable tbody tr')].filter(vis).map(tr=>{
    const cells=[...tr.cells].map(c=>(c.innerText||'').trim());
    if(cells.length!==heads.length)throw Error('grid layout changed: '+heads.length+' headers, '+cells.length+' cells');
    const row={};heads.forEach((h,i)=>{if(h)row[h]=cells[i]});
    const a=[...tr.querySelectorAll('a')].find(a=>/OpptyDetails/.test((a.getAttribute('onclick')||'')+(a.getAttribute('href')||'')));
    const link=a?(a.getAttribute('onclick')||'')+' '+(a.getAttribute('href')||''):'';
    row._opp=(link.match(/lDID=(\d+)/)||[])[1]||'';
    const path=(link.match(/[^'"\s()]*OpptyDetails\.aspx\?[^'"\s()]+/i)||[])[0];
    row._url=path?new URL(path.replace(/&amp;/g,'&'),d.location.href).href:'';
    return row}).filter(Boolean)})()"""

JS_GRID_SCROLL = r"""(()=>{const d=frameFor('NewProspects/Prospects.aspx').document;const c=d.querySelector('.k-grid-content');
  if(!c)return {end:true};c.scrollTop=Math.min(c.scrollHeight-c.clientHeight,c.scrollTop+c.clientHeight*0.8);
  c.dispatchEvent(new Event('scroll',{bubbles:true}));return {end:c.scrollTop+c.clientHeight>=c.scrollHeight-3}})()"""

JS_OPEN_REPORTS = r"""clickOne([...document.querySelectorAll('a')].filter(a=>txt(a)==='All'&&
  /ReportMenu/i.test((a.getAttribute('onclick')||'')+(a.getAttribute('href')||''))),'Reports > All link')"""
JS_OPEN_LEASE_REPORT = r"""(()=>{const w=frameFor('ReportMenu');if(!w)return false;
  const a=[...w.document.querySelectorAll('a')].filter(a=>txt(a)==='Lease Retention Detail');if(!a.length)return false;a[0].click();return true})()"""
JS_RUN_LEASE_REPORT = r"""(()=>{const w=frameFor('CustomReport');if(!w)return false;const d=w.document;
  const s=d.getElementById('datePickerStartDate'),e=d.getElementById('datePickerEndDate'),go=d.getElementById('btnRunReport');
  if(!s||!e||!go||!/Lease Retention Detail/.test(d.body.innerText))return false;
  s.value=%s;e.value=%s;go.click();return true})()"""
JS_LEASE_ROWS = r"""(()=>{const w=frameFor('CustomReport');if(!w)return null;const d=w.document;
  if(d.getElementById('btnRunReport'))return null;const t=d.getElementById('gvReport');
  if(!t)return d.readyState==='complete'?[]:null;const heads=[...t.rows[0].cells].map(txt);
  return [...t.rows].slice(1).map(r=>{const o={};[...r.cells].forEach((c,i)=>o[heads[i]]=txt(c));return o})})()"""


# Visible text of a page and every same-origin frame in it, frame by frame.
# History (scheduled activities and the activity stream) and the Audit Trail, read the way the original copilot's
# driver reads them: the pane's rows by their grid IDs, its scroll position, any loading spinner, and any pager.
PANES = {"history": ("history.aspx", "[...d.querySelectorAll('#gvScheduled > tbody > tr'),"
                                     "...(d.querySelector('#gvOpptyHistory table')?[...d.querySelector('#gvOpptyHistory table')"
                                     ".querySelectorAll(':scope > tbody > tr')]:[])]"),
         "audit": ("CustomerAuditTrail.aspx", "[...d.querySelectorAll('#GridViewAuditTrail > tbody > tr')]")}
JS_PANE_STATE = r"""(()=>{const w=frameFor(%s);if(!w||!w.document.body)return null;const d=w.document,s=d.scrollingElement||d.documentElement;
  const rows=%s;return {rows:rows.filter(vis).map(r=>txt(r)).filter(Boolean),top:s.scrollTop,ch:s.clientHeight,sh:s.scrollHeight,
  loading:[...d.querySelectorAll('[aria-busy=true],[class*=loading],[class*=Loading],[id*=loading],[id*=Loading]')].filter(vis).length,
  paging:[...d.querySelectorAll('a,button,input')].filter(e=>vis(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&
    /^(next|load more|show more)$/i.test((e.innerText||e.value||e.getAttribute('aria-label')||'').trim()))
    .map(e=>(e.innerText||e.value||e.getAttribute('aria-label')||'').trim())}})()"""
JS_PANE_SCROLL = r"""(()=>{const d=frameFor(%s).document,s=d.scrollingElement||d.documentElement;
  s.scrollTop=Math.min(s.scrollHeight-s.clientHeight,s.scrollTop+%s);s.dispatchEvent(new Event('scroll'));return s.scrollTop})()"""
# A pane's own pager ("Next", "Load more"): it only shows more of the log.
JS_PANE_PAGE = r"""(()=>{const d=frameFor(%s).document,want=%s;const xs=[...d.querySelectorAll('a,button,input')].filter(e=>vis(e)&&!e.disabled&&
  (e.innerText||e.value||e.getAttribute('aria-label')||'').trim()===want);if(xs.length!==1)throw Error('pager ambiguous');xs[0].click();return true})()"""
# The Audit Trail tab of a lead record: opening it only shows the log, it changes nothing.
JS_OPEN_AUDIT = r"""(()=>{const w=[window,...walk(window)].find(w=>[...w.document.querySelectorAll('#liAuditTrail')].some(vis));
  if(!w)return false;const e=[...w.document.querySelectorAll('#liAuditTrail')].filter(vis);if(e.length!==1)throw Error('audit tab ambiguous');
  (e[0].querySelector('a')||e[0]).click();return true})()"""
JS_AUDIT_LOADED = r"""(()=>{const w=frameFor('CustomerAuditTrail.aspx');return !!(w&&w.document.readyState==='complete'&&w.document.body)})()"""

JS_ALL_TEXT = r"""[window,...walk(window)].map(w=>{try{return w.document.body?w.document.body.innerText:''}catch(e){return ''}})
  .filter(t=>t&&t.trim()).join('\n----\n')"""


class SyncError(RuntimeError):
    pass


def debug_endpoint(cfg: dict) -> str:
    """WebSocket address of the Chrome you are already using, from the file Chrome writes when
    remote debugging is switched on at chrome://inspect/#remote-debugging."""
    folder = Path(cfg["chrome_user_data"] or Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data")
    try:
        port, path = (folder / "DevToolsActivePort").read_text(encoding="utf-8").split()[:2]
    except (OSError, ValueError) as error:
        raise SyncError("Chrome is not accepting connections. Open Chrome, go to chrome://inspect/#remote-debugging "
                        "and turn on remote debugging, then rerun.") from error
    return f"ws://127.0.0.1:{port}{path}"


class Browser:
    """One connection to your own Chrome. Chrome asks you to allow each new connection, so a
    long-running process opens this once and keeps it."""

    def __init__(self, cfg: dict):
        import websocket  # websocket-client
        try:
            # The long timeout leaves time to click Allow.
            self.socket = websocket.create_connection(debug_endpoint(cfg), timeout=120, suppress_origin=True)
        except (OSError, websocket.WebSocketException) as error:
            raise SyncError("Could not connect to Chrome. Is it open, and did you click Allow?") from error
        self.counter = 0

    def call(self, method: str, session: str | None = None, **params):
        self.counter += 1
        message = {"id": self.counter, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        try:
            self.socket.send(json.dumps(message))
            while True:
                reply = json.loads(self.socket.recv())
                if reply.get("id") == self.counter:
                    break
        except Exception as error:  # websocket-client raises several unrelated types when Chrome goes away
            raise SyncError(f"Lost the connection to Chrome ({type(error).__name__})") from error
        if "error" in reply:
            raise SyncError(f"{method}: {reply['error'].get('message')}")
        return reply["result"]

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


class Tab:
    """A tab of its own in your Chrome (your logins carry over). Closing it leaves your other tabs alone."""

    def __init__(self, browser: Browser, url: str = CRM_URL, settle: float = 6):
        self.browser = browser
        self.target = browser.call("Target.createTarget", url=url, background=True)["targetId"]
        self.session = browser.call("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        time.sleep(settle)

    def call(self, method: str, **params):
        return self.browser.call(method, session=self.session, **params)

    def js(self, expression: str):
        result = self.call("Runtime.evaluate", expression=f"(()=>{{{JS_PRELUDE}return {expression}}})()",
                           returnByValue=True, userGesture=True)
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise SyncError((detail.get("exception") or {}).get("description") or detail.get("text", "page script failed"))
        return result["result"].get("value")

    def wait_for(self, expression: str, what: str, timeout: float = 45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.js(expression)
            if value not in (None, False):
                return value
            time.sleep(0.5)
        raise SyncError(f"Timed out waiting for {what}")

    def require_crm(self) -> None:
        """Stop unless the tab landed on the CRM, logged in."""
        if CRM_HOST not in self.js("location.href") or not self.js("document.querySelectorAll('a').length>50"):
            raise SyncError("CDK is not logged in. Log into CDK in Chrome as usual, then rerun.")

    def require_crm_page(self) -> None:
        """Stop unless the tab is on the CRM host and not on a login page."""
        href = self.js("location.href") or ""
        if CRM_HOST not in href or "login" in href.lower():
            raise SyncError("CDK is not logged in. Log into CDK in Chrome as usual, then rerun.")

    def close(self) -> None:
        try:
            self.browser.call("Target.closeTarget", targetId=self.target)
        except SyncError:
            pass


def pause(low: float = 1.2, high: float = 2.6) -> None:
    time.sleep(random.uniform(low, high))


def grid_ready(tab: Tab, title: str = "Active") -> dict:
    def ready():
        state = tab.js(JS_GRID_STATE % json.dumps(title))
        return state if state and state["title"] and state["total"] is not None and not state["busy"] \
            and (state["rows"] > 0 or state["total"] == 0) else None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = ready()
        if state:
            return state
        time.sleep(0.5)
    raise SyncError(f"{title} grid did not finish loading")


def read_active_leads(tab: Tab, days: int = 90) -> list[dict]:
    return read_folder(tab, ACTIVE, days)


def read_sold(tab: Tab, days: int = 90) -> list[dict]:
    """Everyone who bought in the last `days`: the Sold folder and Active's sold sub-folders. A folder that will
    not load is skipped with a warning rather than stopping the morning read."""
    rows = []
    for folder in SOLD_FOLDERS:
        try:
            rows += read_folder(tab, folder, days)
        except SyncError as error:
            print(f"warning: {folder[0]} not read: {error}", flush=True)
        pause()
    return rows


def read_folder(tab: Tab, folder: tuple[str, str], days: int = 90) -> list[dict]:
    link, title = folder
    wanted = f"{days} Days"
    tab.require_crm()
    tab.js(JS_OPEN_FOLDER % (json.dumps(link), json.dumps(title)))
    pause()
    state = grid_ready(tab, title)
    if state["days"] != wanted:
        tab.js(JS_OPEN_DAYS)
        pause(0.6, 1.2)
        tab.js(JS_PICK_DAYS % json.dumps(wanted))
        pause(0.6, 1.2)
        tab.js(JS_REFRESH)
        pause(2.5, 4)
        state = grid_ready(tab, title)
        if state["days"] != wanted:
            raise SyncError(f"Days filter shows {state['days']!r}, expected {wanted!r}")
    seen: dict[str, dict] = {}
    idle = 0
    for _ in range(400):
        before = len(seen)
        for row in tab.js(JS_GRID_ROWS):
            seen.setdefault(row.get("_opp") or f"{row.get('Name')}|{row.get('Date In')}", row)
        idle = idle + 1 if len(seen) == before else 0
        at_end = tab.js(JS_GRID_SCROLL)["end"]
        if at_end and idle >= 2:
            break
        pause()
    rows = list(seen.values())
    if state["total"] and len(rows) != state["total"]:
        print(f"warning: CDK reports {state['total']} in {link}, read {len(rows)}")
    return rows


def read_lease_report(tab: Tab, start: date, end: date) -> list[dict]:
    tab.require_crm()
    tab.js(JS_OPEN_REPORTS)
    pause()
    tab.wait_for(JS_OPEN_LEASE_REPORT, "the report menu")
    pause()
    run = JS_RUN_LEASE_REPORT % (json.dumps(f"{start.month}/{start.day}/{start.year} 12:00:00 AM"),
                                 json.dumps(f"{end.month}/{end.day}/{end.year} 11:59:59 PM"))
    tab.wait_for(run, "the Lease Retention Detail criteria")
    pause(2.5, 4)
    return tab.wait_for(f"(v=>v===null?null:{{rows:v}})({JS_LEASE_ROWS})", "the lease report", timeout=90)["rows"]


# The Desk Log (Desking > Desk Log): its Appointments panel lists who is booked to come in. The page is a form
# post whose dates live in two hidden fields (Calendar, Calendar2, YYYY-MM-DD); setting both to one day and
# pressing Refresh shows that day. Only the date filter and Refresh are touched; nothing is typed into a record.
JS_OPEN_DESKLOG = r"""(()=>clickOne([...document.querySelectorAll('a')].filter(a=>vis(a)&&txt(a)==='Desk Log'),'Desk Log link'))()"""
JS_DESKLOG_READY = r"""(()=>{const w=frameFor('desklog.aspx');return !!(w&&w.document.readyState==='complete'&&w.document.forms[0]
  &&w.document.forms[0].Calendar&&w.document.querySelector('#RowInsertionPoint'))})()"""
JS_DESKLOG_DAY = r"""(()=>{const w=frameFor('desklog.aspx'),f=w.document.forms[0],iso=%s,us=%s;
  w.__asked=iso;f.Calendar.value=iso;f.Calendar2.value=iso;f['start-date-input-simple'].value=us;f['end-date-input-simple'].value=us;
  return clickOne([...w.document.querySelectorAll('button')].filter(b=>/^refresh$/i.test(txt(b))),'Desk Log Refresh')})()"""
# Rows once the page that was asked for has loaded (the old page still carries __asked, the new one does not).
JS_DESKLOG_ROWS = r"""(()=>{const w=frameFor('desklog.aspx');if(!w||w.__asked||w.document.readyState!=='complete')return null;
  const f=w.document.forms[0];if(!f||!f.Calendar||f.Calendar.value!==%s||f.Calendar2.value!==%s)return null;
  const table=w.document.querySelector('#RowInsertionPoint');if(!table)return null;
  const heads=[...table.querySelectorAll('th')].map(txt);
  if(heads.join('|')!=='Show|N/U|Customer|Due|Salesperson|Confirm|No Show')throw Error('desk log layout changed: '+heads.join('|'));
  const tip=c=>[c,...c.querySelectorAll('[data-tooltip]')].map(e=>e.getAttribute('data-tooltip')||'').filter(Boolean).join(' / ');
  return [...table.querySelectorAll('tr')].filter(tr=>tr.querySelectorAll('td').length===7).map(tr=>{const c=[...tr.cells];
    return {name:txt(c[2]),time:txt(c[3]),salesperson:txt(c[4]),vehicle:tip(c[1]),state:tip(c[2])||tip(c[5]),
            phones:(tip(c[3]).match(/\(\d{3}\)\s*\d{3}-\d{4}/g)||[]),confirmed:!!(c[5].querySelector('input[type=checkbox]')||{}).checked}})})()"""


def read_desk_log(tab: Tab, start: date, days: int) -> list[dict]:
    """Every appointment on the Desk Log from `start` through `days` days after it, one day at a time (a row
    shows only its time, so each day is read on its own)."""
    tab.require_crm()
    tab.js(JS_OPEN_DESKLOG)
    tab.wait_for(JS_DESKLOG_READY, "the Desk Log")
    found = []
    for offset in range(days + 1):
        day = start + timedelta(days=offset)
        iso, us = day.isoformat(), f"{day.month:02d}/{day.day:02d}/{day.year}"
        pause(0.8, 1.6)
        tab.js(JS_DESKLOG_DAY % (json.dumps(iso), json.dumps(us)))
        rows = tab.wait_for(f"(v=>v===null?null:{{rows:v}})({JS_DESKLOG_ROWS % (json.dumps(iso), json.dumps(iso))})",
                            f"the Desk Log for {us}")["rows"]
        found += [dict(row, day=iso) for row in rows]
    return found


def lead_record_url(url: str) -> bool:
    """Only a CDK lead record (OpptyDetails.aspx with its ID parameters) may be opened."""
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    return (parsed.scheme == "https" and parsed.hostname == CRM_HOST and not parsed.fragment
            and parsed.path.lower().endswith("/newprospects/opptydetails.aspx")
            and set(query) <= {"lDID", "lPID", "Loc"} and all(len(v) == 1 and v[0].isdigit() for k, v in query.items() if k != "Loc")
            and "lDID" in query)


def read_lead_record(browser: Browser, url: str) -> str:
    """Open one lead's CDK record in its own background tab and read all of it the way the original copilot does:
    the record page, then every History row (scheduled activities and the activity stream, scrolled and paged to a
    proven end), then the Audit Trail tab and every one of its rows. Only the Audit Trail tab and the panes' own
    pagers are clicked; nothing is typed or saved. A pane that never proves its end leaves the record unread."""
    if not lead_record_url(url):
        raise SyncError("Not a CDK lead record address")
    tab = Tab(browser, url, settle=5)
    try:
        tab.require_crm_page()
        tab.wait_for(JS_PANE_STATE % (json.dumps(PANES["history"][0]), PANES["history"][1]), "the History pane")
        history = scan_pane(tab, "history")
        page = tab.js(JS_ALL_TEXT) or ""
        if not tab.js(JS_OPEN_AUDIT):
            raise SyncError("Audit Trail tab not found on the lead record")
        tab.wait_for(JS_AUDIT_LOADED, "the Audit Trail")
        audit = scan_pane(tab, "audit")
        return (page + "\n----\nHISTORY\n" + "\n".join(history) + "\n----\nAUDIT TRAIL\n" + "\n".join(audit))
    finally:
        tab.close()


def scan_pane(tab: Tab, kind: str, cycles: int = 120) -> list[str]:
    """Every row of one pane, in order, collected while scrolling (a row that scrolls out of view is kept), paging
    when the pane has a pager. Ends only on proof: at the bottom, not moving, no new rows for three steps, nothing
    loading and no pager left. Ported from the original copilot's driver."""
    page, rows_js = PANES[kind]
    state = lambda: tab.js(JS_PANE_STATE % (json.dumps(page), rows_js))
    seen: dict[str, None] = {}
    no_new = 0

    def absorb(snapshot) -> None:
        nonlocal no_new
        before = len(seen)
        seen.update(dict.fromkeys(snapshot["rows"]))
        no_new = no_new + 1 if len(seen) == before else 0

    s = state()
    if s is None:
        raise SyncError(f"{kind} pane not found")
    absorb(s)
    last_rows = tuple(s["rows"])
    for _ in range(cycles):
        before = s["top"]
        tab.js(JS_PANE_SCROLL % (json.dumps(page), max(24, s["ch"] * 0.8)))
        for _ in range(20):  # wait for the pane to move or render more
            time.sleep(0.2)
            n = state()
            if tuple(n["rows"]) != last_rows or n["top"] > before + 1 or n["sh"] != s["sh"]:
                break
        n = state()
        absorb(n)
        near, stuck = n["top"] + n["ch"] >= n["sh"] - 3, n["top"] <= before + 1
        if near and stuck and no_new >= 3 and not n["loading"]:
            if n["paging"]:
                tab.js(JS_PANE_PAGE % (json.dumps(page), json.dumps(n["paging"][0])))
                time.sleep(0.4)
                after = state()
                absorb(after)
                last_rows, s = tuple(after["rows"]), after
                continue
            tab.js(JS_PANE_SCROLL % (json.dumps(page), 8))
            time.sleep(0.3)
            final = state()
            count = len(seen)
            absorb(final)
            if final["top"] <= n["top"] + 1 and len(seen) == count and not final["loading"] and not final["paging"]:
                return list(seen)
            n = final
        last_rows, s = tuple(n["rows"]), n
    raise SyncError(f"{kind} never reached a proven end")


def iso_date(value: str) -> str | None:
    value = (value or "").strip().split(" ")[0]
    for pattern in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def store_leads(con, rows: list[dict], store: str) -> dict:
    """One customer per phone, however they arrived: a lead you took and texted from Quo first, or the same person
    entered in CDK twice, is merged into one lead and enriched from CDK, and its audit trail says so. With two CDK
    records, the newest inquiry is the main one and both records are read."""
    counts = {"saved": 0, "no_phone": 0, "verified": 0, "duplicates": 0}
    rows = sorted(rows, key=lambda r: iso_date(r.get("Date In", "")) or "")  # oldest first: the newest ends up main
    for row in rows:
        phone = db.normalize_phone(row.get("Phone", ""))
        if not phone:
            counts["no_phone"] += 1
            continue
        name = " ".join(row.get("Name", "").split())
        vehicle = " ".join(row.get("Vehicle", "").replace(" N Ford ", " Ford ").split())
        opp, date_in = row.get("_opp") or "", iso_date(row.get("Date In", ""))
        before = con.execute("SELECT id, vehicle, opp_id, other_opps, origin, date_in FROM leads WHERE phone=?", (phone,)).fetchone()
        fields = dict(name=name, first_name=db.first_name(name), store=store, vehicle=vehicle,
                      salesperson=row.get("Salesperson", ""), lead_source=row.get("Source", "").splitlines()[0] if row.get("Source") else "",
                      date_in=date_in, opp_id=opp, opp_url=row.get("_url") if lead_record_url(row.get("_url", "")) else None)
        others = set(filter(None, ((before and before["other_opps"]) or "").split(",")))
        main = before and before["opp_id"]
        if main and opp and opp != main and opp not in others:
            counts["duplicates"] += 1
            db.log(con, before["id"], "second_cdk_record", f"CDK has {opp} and {main} for this phone; merged into one lead")
        if main and opp and opp != main and (date_in or "") < (before["date_in"] or ""):
            others.add(opp)  # an older record of the same customer: keep reading it, but the newer one stays main
            con.execute("UPDATE leads SET other_opps=? WHERE id=?", (",".join(sorted(others)), before["id"]))
            counts["saved"] += 1
            continue
        if main and opp and opp != main:
            others.add(main)
        others.discard(opp)
        lead_id = db.upsert_lead(con, phone, origin=None if before else "cdk", **fields)
        con.execute("UPDATE leads SET other_opps=? WHERE id=?", (",".join(sorted(others)) or None, lead_id))
        if not before:
            db.log(con, lead_id, "from_cdk", f"{opp} {store}, {fields['salesperson']}, {fields['lead_source']}, in {date_in}")
        elif opp and not main:
            counts["verified"] += 1
            who = "your lead from Quo" if before["origin"] == "you" else "lead already here"
            db.log(con, lead_id, "verified_in_cdk", f"{who} found in CDK as {opp} ({fields['salesperson']}, "
                                                    f"{fields['lead_source']}, in {date_in}); merged, not duplicated")
        if opp:
            con.execute("UPDATE leads SET cdk_found=coalesce(cdk_found,?) WHERE id=?", (db.now(), lead_id))
        if before and before["vehicle"] and vehicle and vehicle != before["vehicle"]:
            bdc.record(con, lead_id, "vehicle_change", f"{vehicle} (was {before['vehicle']})")
            db.log(con, lead_id, "vehicle_change", f"{vehicle} (was {before['vehicle']})")
        counts["saved"] += 1
    con.commit()
    return counts


def store_sold(con, rows: list[dict], store: str) -> dict:
    """Customers in CDK's sold folders get status 'sold': no offers or check-ins, and "I got it" is read as a
    purchase from us. Someone who came back as a newer Active lead after the sale is shopping again and stays."""
    counts = {"sold": 0, "shopping_again": 0, "no_phone": 0}
    for row in rows:
        phone = db.normalize_phone(row.get("Phone", ""))
        if not phone:
            counts["no_phone"] += 1
            continue
        sold_on = iso_date(row.get("Date Sold", "") or row.get("Date In", ""))
        existing = con.execute("SELECT date_in, status FROM leads WHERE phone=?", (phone,)).fetchone()
        if existing is None:
            name = " ".join(row.get("Name", "").split())
            db.upsert_lead(con, phone, name=name, first_name=db.first_name(name), store=store,
                           vehicle=" ".join(row.get("Vehicle", "").replace(" N Ford ", " Ford ").split()),
                           salesperson=row.get("Salesperson", ""), opp_id=row.get("_opp"))
        elif existing["date_in"] and sold_on and existing["date_in"] > sold_on:
            counts["shopping_again"] += 1
            continue
        con.execute("UPDATE leads SET status='sold', sold_on=?, updated_at=? WHERE phone=? AND status!='opted_out'",
                    (sold_on, db.now(), phone))
        counts["sold"] += 1
    con.commit()
    return counts


def store_leases(con, rows: list[dict], store: str) -> dict:
    counts = {"saved": 0, "no_phone": 0}
    for row in rows:
        phone = db.normalize_phone(row.get("Phone Number", ""))
        lease_end = iso_date(row.get("Estimated Last Payment", ""))
        if not phone or not lease_end:
            counts["no_phone"] += 1
            continue
        name = " ".join(row.get("Customer", "").split())
        fields = dict(lease_end=lease_end, lease_term=row.get("Term"), lease_payment=row.get("Payment"),
                      lease_vehicle=row.get("Vehicle"), vin=row.get("VIN"),
                      lease_still_owns=int(row.get("Still Owns", "").strip().upper().startswith("Y")),
                      lease_renewed=int(bool(row.get("Renewal Date", "").strip())))
        existing = con.execute("SELECT salesperson FROM leads WHERE phone=?", (phone,)).fetchone()
        if existing is None or not existing["salesperson"]:
            # Only fill identity from the report when the Active grid has not already supplied it.
            fields.update(name=name, first_name=db.first_name(name), store=store,
                          salesperson=row.get("New Sales Person") or row.get("Original Sales Person"))
        db.upsert_lead(con, phone, **fields)
        counts["saved"] += 1
    con.commit()
    return counts
