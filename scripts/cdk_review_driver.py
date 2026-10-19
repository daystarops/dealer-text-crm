"""One-shot deterministic, allowlisted, read-only CDK extraction driver."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from browser_recovery import recover

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT/"private"; SESSION_STATE=PRIVATE/"session-state.json"; LATEST_BATCH=PRIVATE/"latest-batch.json"
SCHEMA=ROOT/"schemas"/"extracted-batch.schema.json"


def utc_now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def atomic_json(path: Path,value: object):
    path.parent.mkdir(parents=True,exist_ok=True); temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False)+"\n",encoding="utf-8"); os.replace(temporary,path)
def failure_state(run_id: str,stage: str,error_type: str):
    atomic_json(SESSION_STATE,{"run_id":run_id,"timestamp":utc_now(),"workflow_stage":stage,"results_target_id":None,
      "selected_date_range":None,"batch_position":0,"hashed_reviewed_row_identifiers":[],"last_successful_action":None,
      "errors_and_retries":[{"error_type":error_type,"retries":0}]})


def validate_shape(instance: object,schema: dict,path: str="$") -> list[str]:
    errors=[]; expected=schema.get("type"); types={"object":dict,"array":list,"string":str,"integer":int,"boolean":bool}
    if expected in types and (not isinstance(instance,types[expected]) or expected=="integer" and isinstance(instance,bool)): return [f"{path}: expected {expected}"]
    if "const" in schema and instance!=schema["const"]: errors.append(f"{path}: unexpected constant")
    if "enum" in schema and instance not in schema["enum"]: errors.append(f"{path}: value is not in enum")
    if isinstance(instance,dict):
        for key in schema.get("required",[]):
            if key not in instance: errors.append(f"{path}: missing {key}")
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected {key}" for key in instance.keys()-props.keys()]
        for key,value in instance.items():
            if key in props: errors += validate_shape(value,props[key],f"{path}.{key}")
    elif isinstance(instance,list) and "items" in schema:
        for index,value in enumerate(instance): errors += validate_shape(value,schema["items"],f"{path}[{index}]")
        if len(instance)<schema.get("minItems",0): errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(instance)>schema["maxItems"]: errors.append(f"{path}: too many items")
    elif isinstance(instance,str):
        if len(instance)<schema.get("minLength",0): errors.append(f"{path}: string too short")
        if "maxLength" in schema and len(instance)>schema["maxLength"]: errors.append(f"{path}: string too long")
        if "pattern" in schema and re.search(schema["pattern"],instance) is None: errors.append(f"{path}: string does not match pattern")
    elif isinstance(instance,int) and not isinstance(instance,bool):
        if "minimum" in schema and instance<schema["minimum"]: errors.append(f"{path}: below minimum")
        if "maximum" in schema and instance>schema["maximum"]: errors.append(f"{path}: above maximum")
    return errors


def browser_program(run_id: str,batch_size: int,as_of_date: str,internal_timeout: int) -> str:
    return BROWSER_PROGRAM.replace("__RUN_ID__",json.dumps(run_id)).replace("__BATCH_SIZE__",str(batch_size)).replace(
        "__AS_OF_DATE__",json.dumps(as_of_date)).replace("__ROOT__",json.dumps(str(ROOT))).replace("__INTERNAL_TIMEOUT__",str(internal_timeout))


BROWSER_PROGRAM=r'''
import hashlib,json,os,re,sys,time
from datetime import date,datetime,timezone
from pathlib import Path
ROOT=Path(__ROOT__);sys.path.insert(0,str(ROOT/"scripts"))
from cdk_runtime import ElementIdentity,NavigationBlocked,authorize_action,approved_crm_url,approved_top_level_url,normalize_phone,one_new_detail_target,parse_detail_destination,is_active_results_url,select_crm_shell_frame,select_main_frame,verify_search_state,verify_selected_tab,visible_unique_rows
from identity_vault import IdentityRecord,default_vault
from privacy_gate import PrivacyGateError,sanitize_payload
RUN_ID=__RUN_ID__;BATCH_SIZE=__BATCH_SIZE__;AS_OF_DATE=date.fromisoformat(__AS_OF_DATE__);DEADLINE=time.monotonic()+__INTERNAL_TIMEOUT__
STATE=ROOT/"private"/"session-state.json";BATCH=ROOT/"private"/"latest-batch.json";PROGRESS=ROOT/"private"/"review-progress.json"
errors=[];reviewed=[];results_target=None;selected_range=None

def now():return datetime.now(timezone.utc).isoformat(timespec="seconds")
def ensure_deadline():
    if time.monotonic()>DEADLINE:raise TimeoutError("Internal browser deadline reached")
def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(value,indent=2,ensure_ascii=False)+"\n",encoding="utf-8");os.replace(temp,path)
def state(stage,position,action):atomic(STATE,{"run_id":RUN_ID,"timestamp":now(),"workflow_stage":stage,"results_target_id":results_target,"selected_date_range":selected_range,"batch_position":position,"hashed_reviewed_row_identifiers":reviewed,"last_successful_action":action,"errors_and_retries":errors})
def pause(ms=500):cdp("Runtime.evaluate",expression=f"new Promise(r=>setTimeout(r,{int(ms)}))",awaitPromise=True)
BACKEND_FRAME={}
def frame_context(frame_id):
    world=cdp("Page.createIsolatedWorld",frameId=frame_id,worldName="cdk-lead-copilot-frame-scope")
    context_id=world.get("executionContextId")
    if not context_id:raise NavigationBlocked("Verified frame execution context is unavailable")
    return context_id
def frame_eval(frame_id,expression,user_gesture=False):
    result=cdp("Runtime.evaluate",contextId=frame_context(frame_id),expression=expression,returnByValue=True,userGesture=bool(user_gesture))
    if result.get("exceptionDetails"):raise NavigationBlocked("Frame-scoped DOM execution failed")
    return result.get("result",{}).get("value")
def frame_tree():return cdp("Page.getFrameTree")["frameTree"]
def all_frames():
    found=[]
    def walk(tree):
        found.append(tree["frame"])
        for child in tree.get("childFrames",[]):walk(child)
    walk(frame_tree())
    return found
def frame_by(predicate):
    found=[frame for frame in all_frames() if predicate(frame)]
    if len(found)!=1:raise NavigationBlocked("Required frame is missing or ambiguous")
    return found[0]
def verified_crm_frame():return select_crm_shell_frame(all_frames())
def verified_main_frame():return select_main_frame(all_frames(),require_active=False)
def verified_active_results_frame():return select_main_frame(all_frames(),require_active=True)
def verified_history_frame():return frame_by(lambda f:approved_crm_url(f.get("url","")) and re.search(r"/History\.aspx(?:\?|$)",f.get("url",""),re.I))
def verified_detail_frame():return frame_by(lambda f:approved_crm_url(f.get("url","")) and re.search(r"/newprospects/OpptyDetails\.aspx(?:\?|$)",f.get("url",""),re.I))
def ax(frame_id):
    if not frame_id:raise NavigationBlocked("Explicit verified frame ID required")
    nodes=cdp("Accessibility.getFullAXTree",frameId=frame_id)["nodes"]
    for node in nodes:
        backend_id=node.get("backendDOMNodeId")
        if backend_id:BACKEND_FRAME[backend_id]=frame_id
    return nodes
def verified_context_node(nodes):
    matches=[node for node in nodes if role(node)=="RootWebArea" and node.get("backendDOMNodeId")]
    if len(matches)!=1:raise NavigationBlocked("Verified frame document context is missing or ambiguous")
    return matches[0]
def role(node):return node.get("role",{}).get("value","")
def name(node):return node.get("name",{}).get("value","")
def value(node):return str(node.get("value",{}).get("value",""))
def resolve(backend_id):
    frame_id=BACKEND_FRAME.get(backend_id)
    if not frame_id:raise NavigationBlocked("DOM node is not bound to a verified frame")
    resolved=cdp("DOM.resolveNode",backendNodeId=backend_id,executionContextId=frame_context(frame_id))
    object_id=resolved.get("object",{}).get("objectId")
    if not object_id:raise NavigationBlocked("Verified frame DOM node could not be resolved")
    return object_id
def call(backend_id,function,user_gesture=False):return cdp("Runtime.callFunctionOn",objectId=resolve(backend_id),functionDeclaration=function,returnByValue=True,userGesture=bool(user_gesture)).get("result",{}).get("value")
def call_in_element_context(node,function,user_gesture=False):return call(node["backendDOMNodeId"],function,user_gesture)
def crm_frame_action(node,function,user_gesture=False):return call_in_element_context(node,function,user_gesture)
def main_frame_action(node,function,user_gesture=False):return call_in_element_context(node,function,user_gesture)
def detail_frame_query(node,function):return call_in_element_context(node,function)
def metadata(node):
    if not node.get("backendDOMNodeId"):raise NavigationBlocked("Allowlisted element has no DOM identity")
    data=call(node["backendDOMNodeId"],r"""function(){const r=this.getBoundingClientRect(),s=getComputedStyle(this);return {tag:this.tagName,id:this.id||'',href:this.getAttribute('href')||'',onclick:this.getAttribute('onclick')||'',controls:this.getAttribute('aria-controls')||'',visible:!!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden'&&!this.closest('[aria-hidden="true"]')),enabled:!this.disabled&&this.getAttribute('aria-disabled')!=='true',top:r.top,left:r.left,inResults:!!this.closest('table.k-selectable')}}""")
    return ElementIdentity(name(node).strip(),role(node),data["tag"],data["id"],data["href"],data["onclick"] or ("aria-controls:"+data["controls"] if data["controls"] else ""),data["visible"],data["enabled"]),data
def exact(nodes,wanted,wanted_role,dom_id=None):
    matches=[]
    for node in nodes:
        if name(node).strip()==wanted and role(node)==wanted_role and node.get("backendDOMNodeId"):
            identity,data=metadata(node)
            if dom_id is None or identity.dom_id==dom_id:matches.append(node)
    if len(matches)!=1:raise NavigationBlocked(f"Required element is missing or ambiguous: {wanted}")
    return matches[0]
def activate(node,action,expected=None,base_url=""):
    identity,data=metadata(node)
    if action=="active":identity=ElementIdentity(identity.accessible_name,identity.role,identity.tag,identity.dom_id,identity.href,"fixed:NewProspects/Prospects.aspx?Folder=1&Title=Active",identity.visible,identity.enabled)
    if action=="days_control":
        control=call(node["backendDOMNodeId"],"function(){return {id:this.id||'',name:this.name||'',type:(this.type||'').toLowerCase(),value:this.value||''}}")
        if not control or control["id"]!="slctDateSpan" or control["name"]!="multi-select-dropdown" or control["type"]!="text" or not control["value"]:raise NavigationBlocked("Days control identity changed")
        identity=ElementIdentity(identity.accessible_name,identity.role,identity.tag,identity.dom_id,identity.href,"fixed:DaysControl",identity.visible,identity.enabled)
    if action=="refresh":identity=ElementIdentity(identity.accessible_name,identity.role,identity.tag,identity.dom_id,identity.href,"fixed:RefreshGrid",identity.visible,identity.enabled)
    destination=authorize_action(action,identity,base_url=base_url,expected_opportunity_id=expected)
    if action in {"prospects","days_control","all_days"}:return call(node["backendDOMNodeId"],"function(){this.click();return true;}")
    if action=="active":return crm_frame_action(node,"function(){this.click();return true;}")
    if action=="refresh":return main_frame_action(node,"function(){this.click();return true;}")
    if action=="lead_detail":return main_frame_action(node,"function(){this.ownerDocument.defaultView.open("+json.dumps(destination.url)+",'_blank');return true;}",user_gesture=True)
    raise NavigationBlocked("No fixed driver action exists")
def structured_fields(context_node):
    return detail_frame_query(context_node,r"""function(){const out={};for(const row of document.querySelectorAll('tr')){const text=(row.innerText||'').trim();for(const label of ['Cell #:','Mobile #:','Home #:','Phone #:'])if(text.startsWith(label)&&!out.phone)out.phone=text.slice(label.length).trim();}return out}""") or {}
def split_name(full):
    if ',' in full:last,first=[p.strip() for p in full.split(',',1)]
    else:parts=full.strip().split();first=parts[0];last=parts[-1] if len(parts)>1 else ''
    return first,last
def date_values(text):
    found=[]
    for m,d,y in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b",text):
        y=int(y)+(2000 if int(y)<100 else 0)
        try:found.append(date(y,int(m),int(d)))
        except ValueError:pass
    return found
def latest_statement(cells):
    current=None;candidates=[]
    for cell in cells:
        dates=date_values(cell)
        if dates:current=dates[0]
        low=cell.casefold()
        if current and any(k in low for k in ('customer ','stated','said','wants','not interested','out of market','no longer')) and 'ai summary' not in low:candidates.append((current,cell))
    return max(candidates,default=(None,''),key=lambda item:item[0] or date.min)[1]

state("connecting",0,"driver started")
tabs=list_tabs();verified=[t for t in tabs if approved_top_level_url(t.get("url",""))]
if len(verified)!=1:raise NavigationBlocked("Existing CDK tab is missing or ambiguous")
results_target=verified[0]["targetId"];authorize_action("switch_verified_tab",ElementIdentity("verified CDK tab","tab","TARGET"));switch_tab(results_target);info=page_info();authorize_action("page_info",ElementIdentity("page_info","inspection","PAGE"))
verify_selected_tab(verified[0],info)
atomic(ROOT/"private"/"recovery-state.json",{"run_id":RUN_ID,"results_target_id":results_target,"crm_frame_url":"","opened_detail_target_ids":[]})
crm=verified_crm_frame();atomic(ROOT/"private"/"recovery-state.json",{"run_id":RUN_ID,"results_target_id":results_target,"crm_frame_url":crm["url"],"opened_detail_target_ids":[]});main=verified_main_frame()
if not is_active_results_url(main["url"]):
    prospects=exact(ax(crm["id"]),"Prospects","tab","tr86")
    expanded=call(prospects["backendDOMNodeId"],"function(){return this.getAttribute('aria-expanded')}")
    if expanded!="true":activate(prospects,"prospects")
    active=exact(ax(crm["id"]),"Active","menuitem","MenuSections_MenuSectionItems_2_MenuSectionLink_2");activate(active,"active")
    main=None
    for _ in range(40):
        pause(250)
        try:
            main=verified_active_results_frame()
            break
        except NavigationBlocked:
            continue
    if main is None:raise NavigationBlocked("Active results route did not finish loading")
else:
    main=verified_active_results_frame()
def search_values():
    days=exact(ax(main["id"]),"Days","textbox","slctDateSpan")
    control=call(days["backendDOMNodeId"],"function(){return {id:this.id||'',name:this.name||'',type:(this.type||'').toLowerCase(),value:this.value||''}}")
    if not control or control["id"]!="slctDateSpan" or control["name"]!="multi-select-dropdown" or control["type"]!="text" or not control["value"]:raise NavigationBlocked("Days search control changed")
    return {"Days":control["value"]}
before=search_values();changed=before!={"Days":"All"};refresh_confirmed=False
if changed:
    days_control=exact(ax(main["id"]),"Days","textbox","slctDateSpan");activate(days_control,"days_control");pause(150)
    option=exact(ax(main["id"]),"All","button");activate(option,"all_days");pause(300)
if search_values()!={"Days":"All"}:raise NavigationBlocked("Days selection did not become All")
refresh=exact(ax(main["id"]),"Refresh","link")
main_frame_action(refresh,r"""function(){const d=this.ownerDocument,g=d.querySelector('table.k-selectable');if(!g)return {gridCount:0};const previous=d.defaultView.__cdkLeadCopilotRefreshState;if(previous&&previous.observer)previous.observer.disconnect();const state={mutated:false,observer:new MutationObserver(()=>{state.mutated=true})};state.observer.observe(g,{subtree:true,childList:true,characterData:true,attributes:true});d.defaultView.__cdkLeadCopilotRefreshState=state;return {gridCount:d.querySelectorAll('table.k-selectable').length}}""")
activate(refresh,"refresh")
for _ in range(120):
    pause(250);main=verified_active_results_frame();refresh=exact(ax(main["id"]),"Refresh","link")
    grid_state=main_frame_action(refresh,r"""function(){const d=this.ownerDocument,visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),grids=[...d.querySelectorAll('table.k-selectable')].filter(visible),loading=[...d.querySelectorAll('[aria-busy=true],[class*=loading],[class*=Loading],[id*=loading],[id*=Loading]')].filter(visible);return {ready:d.readyState==='complete',days:[...d.querySelectorAll('input#slctDateSpan')].filter(visible).map(e=>e.value),gridCount:grids.length,rows:grids.length===1?[...grids[0].querySelectorAll('tbody tr')].filter(visible).length:0,loading:loading.length}}""")
    if grid_state and grid_state["ready"] and grid_state["days"]==["All"] and grid_state["gridCount"]==1 and grid_state["rows"]>0 and grid_state["loading"]==0:
        refresh_confirmed=True
        break
if not refresh_confirmed:raise NavigationBlocked("Refresh completion was not verified")
after=search_values()
verify_search_state(after["Days"],refresh_confirmed)
selected_range={"period":after["Days"],"start_date":"","end_date":"","refresh_confirmed":refresh_confirmed}
def scan_rows():
    raw=frame_eval(main["id"],r"""(()=>{const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),grids=[...document.querySelectorAll('table.k-selectable')].filter(visible);if(grids.length!==1)throw new Error('Results grid ambiguous:'+grids.length);return [...grids[0].querySelectorAll('a')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&(e.getAttribute('onclick')||'').includes('OpptyDetails.aspx')).map(e=>{const r=e.getBoundingClientRect();return {raw_name:(e.innerText||e.textContent||'').trim(),onclick:e.getAttribute('onclick')||'',href:e.getAttribute('href')||'',visible:true,enabled:true,visual_order:[r.top,r.left]}})})()""") or []
    rows=[]
    for data in raw:
        destination=parse_detail_destination(data["onclick"] or data["href"],main["url"])
        identity=ElementIdentity(data["raw_name"],"link","A","",data["href"],data["onclick"],data["visible"],data["enabled"])
        if destination:
            authorize_action("lead_detail",identity,base_url=main["url"],expected_opportunity_id=destination.opportunity_id);rows.append(data)
    return rows
def open_lead_detail(expected_opportunity_id,base_url):
    fresh=[candidate for candidate in scan_rows() if parse_detail_destination(candidate["onclick"] or candidate["href"],base_url).opportunity_id==expected_opportunity_id]
    if len(fresh)!=1:raise NavigationBlocked("Fresh lead DOM node is missing or ambiguous")
    candidate=fresh[0];identity=ElementIdentity(candidate["raw_name"],"link","A","",candidate["href"],candidate["onclick"],candidate["visible"],candidate["enabled"])
    authorize_action("lead_detail",identity,base_url=base_url,expected_opportunity_id=expected_opportunity_id)
    expected=json.dumps(expected_opportunity_id)
    return frame_eval(main["id"],r"""(()=>{const expected="""+expected+r""",visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),xs=[...document.querySelectorAll('table.k-selectable a')].filter(e=>visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&((e.getAttribute('onclick')||'').match(/(?:[?&])lDID=(\d+)/)||[])[1]===expected);if(xs.length!==1)throw new Error('Fresh lead link ambiguous:'+xs.length);xs[0].click();return true})()""",user_gesture=True)
try:
    progress=json.loads(PROGRESS.read_text(encoding="utf-8"))
except (OSError,json.JSONDecodeError):
    progress={}
completed=set(progress.get("reviewed_row_hashes",[])) if progress.get("source_scope")=="all_active" else set()
unreviewed=[row for row in scan_rows() if hashlib.sha256(parse_detail_destination(row["onclick"] or row["href"],main["url"]).opportunity_id.encode()).hexdigest() not in completed]
snapshot=visible_unique_rows(unreviewed,BATCH_SIZE,main["url"]);state("batch_snapshotted",0,"visible unique unreviewed batch snapshotted")
vault=default_vault(ROOT);leads=[];identities={}
try:
  for position,row in enumerate(snapshot,1):
    ensure_deadline()
    detail_id=None
    try:
      before_tabs={t["targetId"] for t in list_tabs()};open_lead_detail(row["cdk_opportunity_id"],main["url"])
      detail=None
      for _ in range(40):
        pause(250);current_tabs=list_tabs();opened=[t for t in current_tabs if t["targetId"] not in before_tabs]
        if len(opened)>1:raise NavigationBlocked("Lead action opened multiple targets")
        if len(opened)==1:
          destination=parse_detail_destination(opened[0].get("url",""),main["url"])
          if destination and destination.opportunity_id==row["cdk_opportunity_id"]:
            detail=opened[0];break
      if not detail:raise NavigationBlocked("Approved detail target did not finish opening")
      detail_id=detail["targetId"];atomic(ROOT/"private"/"recovery-state.json",{"run_id":RUN_ID,"results_target_id":results_target,"crm_frame_url":main["url"],"opened_detail_target_ids":[detail_id]});switch_tab(detail_id);wait_for_load(timeout=30);ensure_deadline()
      detail_frame=None;history=None
      for _ in range(120):
        pause(250)
        try:
          detail_frame=verified_detail_frame();history=verified_history_frame();break
        except NavigationBlocked:
          continue
      if detail_frame is None or history is None:raise NavigationBlocked("Detail history frame did not finish loading")
      detail_nodes=ax(detail_frame["id"]);history_nodes=ax(history["id"])
      fields=structured_fields(verified_context_node(detail_nodes));first,last=split_name(row["raw_name"]);identities[f"{first} {last}".strip()]=first
      main_cells=[name(n) for n in detail_nodes if role(n) in ("LayoutTableCell","StaticText","option") and name(n)]
      history_cells=[name(n) for n in history_nodes if role(n) in ("cell","LayoutTableCell") and name(n)]
      combined=" | ".join(main_cells);vehicle=source=trade=""
      for item in main_cells:
        if "Vehicle" in item and not vehicle:
          match=re.search(r"Vehicle\s+(.*?)(?:Window Sticker|Stock #|Website VDP|Trade:)",item,re.I);vehicle=match.group(1).strip() if match else vehicle
        if "Source:" in item and not source:
          match=re.search(r"Source:\s*(.*?)(?:Sub-source:|Date/Time Due:|$)",item,re.I);source=match.group(1).strip() if match else source
        if "Trade:" in item and not trade:
          match=re.search(r"Trade:\s*(.*?)(?:Manager Review:|Sales Teams:|$)",item,re.I);trade=match.group(1).strip() if match else trade
      created_dates=[d for item in history_cells if "Active -" in item for d in date_values(item)];created=min(created_dates) if created_dates else AS_OF_DATE
      identity=IdentityRecord(row["cdk_opportunity_id"],first,last,f"{first} {last}".strip(),normalize_phone(fields.get("phone", "")),vehicle,row["conversation_thread_id"])
      lead_key=vault.extraction_upsert(identity);statement=latest_statement(history_cells)
      analysis_lead={"lead_key":lead_key,"first_name":first,"vehicle":vehicle,"trade":{"submitted":bool(trade),"year":next(iter(re.findall(r"\b(?:19|20)\d{2}\b",trade)),""),"make":"","model":"","approx_mileage":next(iter(re.findall(r"\b\d{1,3}(?:,\d{3})+\s*(?:mi|miles)?\b",trade,re.I)),"")},"lead_age_days":max(0,(AS_OF_DATE-created).days),"lead_source":source,
        "current_status":next((item for item in history_cells if "Active -" in item),""),"latest_explicit_customer_statement":statement,
        "last_meaningful_customer_response":statement,"last_salesperson_contact":next((item for item in history_cells if any(k in item.casefold() for k in ('outbound call','left message','manual email','text message'))),""),
        "communication_history":history_cells[:30],"appointment_or_showroom_history":[item for item in history_cells if 'appointment' in item.casefold() or 'showroom' in item.casefold()][:10],
        "scheduled_tasks":[item for item in history_cells if any(k in item.casefold() for k in ('follow up','phone follow','email follow'))][:10],
        "buying_signals":[label for token,label in (("showroom","Showroom activity"),("trade","Trade submitted"),("opened","Message opened"),("inbound","Direct response")) if token in " ".join(history_cells).casefold()],
        "main_obstacle":"Customer is out of market" if any(k in statement.casefold() for k in ('out of market','no longer','not shopping')) else "No decisive next step recorded",
        "crm_generated_summary":next((item for item in history_cells if 'AI Summary' in item),"")}
      gated=sanitize_payload(analysis_lead,{f"{first} {last}".strip():first});leads.append(gated.safe_payload);reviewed.append(hashlib.sha256(row["cdk_opportunity_id"].encode()).hexdigest())
      state("reviewing_leads",position,"completed encrypted identity and sanitized analysis extraction")
    finally:
      current={t["targetId"] for t in list_tabs()}
      if detail_id and detail_id in current:authorize_action("close_detail",ElementIdentity("detail target","tab","TARGET"));close_tab(detail_id)
      atomic(ROOT/"private"/"recovery-state.json",{"run_id":RUN_ID,"results_target_id":results_target,"crm_frame_url":main["url"],"opened_detail_target_ids":[]})
      current={t["targetId"] for t in list_tabs()}
      if results_target not in current:raise NavigationBlocked("Original results target disappeared during cleanup")
      authorize_action("restore_results",ElementIdentity("results target","tab","TARGET"));switch_tab(results_target)
      restored=verified_active_results_frame()
      if not is_active_results_url(restored.get("url","")):raise NavigationBlocked("Active results page was not restored")
finally:vault.close()
payload={"run_id":RUN_ID,"extracted_at":now(),"as_of_date":AS_OF_DATE.isoformat(),"search_period":selected_range,"batch_size":BATCH_SIZE,"reviewed_count":len(leads),"leads":leads}
payload=sanitize_payload(payload,identities).safe_payload;atomic(BATCH,payload);state("extraction_complete",BATCH_SIZE,"encrypted vault and sanitized batch written")
atomic(PROGRESS,{"source_scope":"all_active","reviewed_row_hashes":sorted(completed|set(reviewed)),"last_run_id":RUN_ID,"updated_at":now()})
print(json.dumps({"ok":True,"run_id":RUN_ID,"reviewed_count":len(leads)}))
'''


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--batch-size",type=int,default=5);parser.add_argument("--as-of-date",default=datetime.now().astimezone().date().isoformat())
    parser.add_argument("--browser-timeout",type=int,default=300);parser.add_argument("--dry-run",action="store_true");args=parser.parse_args()
    if args.batch_size<1:parser.error("batch size must be positive")
    datetime.strptime(args.as_of_date,"%Y-%m-%d");run_id=f"cdk-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    schema=json.loads(SCHEMA.read_text(encoding="utf-8"));program=browser_program(run_id,args.batch_size,args.as_of_date,max(1,args.browser_timeout-5));compile(program,"<fixed-cdk-driver>","exec")
    if args.dry_run:
        failure_state(run_id,"dry_run_complete","none");print("Dry run passed; Browser Use was not started.");return 0
    executable=shutil.which("browser-use")
    if not executable:failure_state(run_id,"failed","browser_use_unavailable");return 1
    try:process=subprocess.run([executable],input=program,text=True,cwd=ROOT,timeout=args.browser_timeout,check=False)
    except subprocess.TimeoutExpired:
        recovered=recover(run_id,30);failure_state(run_id,"failed","browser_timeout_recovered" if recovered else "browser_timeout_recovery_failed");print("Browser extraction timed out; bounded recovery "+("succeeded" if recovered else "did not succeed"),file=sys.stderr);return 124
    if process.returncode:failure_state(run_id,"failed","browser_driver_failure");return process.returncode
    try:batch=json.loads(LATEST_BATCH.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError):failure_state(run_id,"failed","missing_sanitized_output");return 1
    if validate_shape(batch,schema):failure_state(run_id,"failed","schema_validation_failure");return 1
    leads=batch.get("leads",[]);keys=[lead.get("lead_key") for lead in leads]
    if batch.get("run_id")!=run_id or batch.get("reviewed_count")!=len(leads) or batch.get("batch_size")!=len(leads) or len(keys)!=len(set(keys)):
        failure_state(run_id,"failed","extraction_invariant_failure");return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
