"""Pure deterministic CDK rules shared by the fixed driver and local tests."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlparse

APPROVED_TOP_LEVEL_HOSTS=frozenset({"app-unify.app.connectcdk.com"})
APPROVED_CRM_HOSTS=frozenset({"crm.connectcdk.com"})
CANONICAL_APPLICATIONS_PATH="/applications"
ACTIVE_ROUTE="NewProspects/Prospects.aspx?Folder=1&Title=Active"
DETAIL_PATH_RE=re.compile(r"/(?:evo2/(?:[^/?#]+/)*)?newprospects/OpptyDetails\.aspx$",re.I)
PROSPECTS_DOM_ID="tr86";ACTIVE_DOM_ID="MenuSections_MenuSectionItems_2_MenuSectionLink_2";REFRESH_HREF="javascript:RefreshGrid()"


class NavigationBlocked(RuntimeError):pass


@dataclass(frozen=True)
class DetailDestination:
    opportunity_id:str
    person_id:str
    url:str


@dataclass(frozen=True)
class ElementIdentity:
    accessible_name:str;role:str;tag:str;dom_id:str="";href:str="";onclick:str="";visible:bool=True;enabled:bool=True


def approved_top_level_url(url:str)->bool:
    parsed=urlparse(url)
    return (
        parsed.scheme=="https"
        and (parsed.hostname or "").lower() in APPROVED_TOP_LEVEL_HOSTS
        and parsed.path.rstrip("/")==CANONICAL_APPLICATIONS_PATH
        and not parsed.query
        and not parsed.fragment
    )


def approved_crm_url(url:str)->bool:
    parsed=urlparse(url)
    return parsed.scheme=="https" and (parsed.hostname or "").lower() in APPROVED_CRM_HOSTS






def is_active_results_url(url:str)->bool:
    if not approved_crm_url(url):
        return False
    parsed=urlparse(url)
    if not parsed.path.lower().endswith("/newprospects/prospects.aspx"):
        return False
    query=parse_qs(parsed.query,keep_blank_values=True)
    return query.get("Folder")==["1"] and query.get("Title")==["Active"]


def select_main_frame(frames:list[dict],*,require_active:bool=False)->dict:
    matches=[
        frame for frame in frames
        if frame.get("name")=="Main"
        and approved_crm_url(frame.get("url",""))
        and (not require_active or is_active_results_url(frame.get("url","")))
    ]
    if len(matches)!=1:
        kind="active CDK Main" if require_active else "CDK Main"
        raise NavigationBlocked(f"Unique {kind} frame is missing or ambiguous")
    return matches[0]

def select_crm_shell_frame(frames:list[dict])->dict:
    """Select the nearest approved CRM ancestor of the unique Main frame."""
    by_id={str(frame.get("id","")):frame for frame in frames if frame.get("id")}
    main_frames=[frame for frame in frames if frame.get("name")=="Main"]
    if len(main_frames)!=1:
        raise NavigationBlocked("Unique CDK Main frame is missing or ambiguous")
    parent_id=str(main_frames[0].get("parentId","") or "")
    visited=set()
    while parent_id:
        if parent_id in visited:
            raise NavigationBlocked("CDK frame ancestry contains a cycle")
        visited.add(parent_id)
        parent=by_id.get(parent_id)
        if parent is None:
            raise NavigationBlocked("CDK Main frame parent is missing")
        if approved_crm_url(parent.get("url","")) and parent.get("name")!="Main":
            return parent
        parent_id=str(parent.get("parentId","") or "")
    raise NavigationBlocked("Approved CRM shell ancestor was not found")

def select_cdk_tab(tabs:list[dict])->dict:
    matches=[tab for tab in tabs if approved_top_level_url(tab.get("url",""))]
    if len(matches)!=1:raise NavigationBlocked("Existing CDK tab is missing or ambiguous")
    return matches[0]

def verify_selected_tab(selected:dict,active:dict)->None:
    # page_info() may omit targetId. Because select_cdk_tab() already requires
    # exactly one approved CDK tab, URL equality is the authoritative check.
    if not approved_top_level_url(active.get("url","")) or active.get("url")!=selected.get("url"):
        raise NavigationBlocked("Switched CDK target was not verified")
    active_target_id=active.get("targetId")
    if active_target_id is not None and active_target_id!=selected.get("targetId"):
        raise NavigationBlocked("Switched CDK target was not verified")
def _extract_destination_text(text:str)->str|None:
    if not text:return None
    # Extract only an http(s) or relative OpptyDetails token from a known attribute; never execute it.
    match=re.search(r"((?:https?://[^'\"\s)]+|(?:\.\./|\./|/)?[^'\"\s)]*?newprospects/OpptyDetails\.aspx\?[^'\"\s)]+))",text,re.I)
    return match.group(1).replace("&amp;","&") if match else None


def parse_detail_destination(text:str,base_url:str)->DetailDestination|None:
    if not approved_crm_url(base_url):return None
    token=_extract_destination_text(text)
    if not token:return None
    raw=urlparse(token)
    if raw.scheme and raw.scheme.lower() not in {"http","https"}:return None
    resolved=urljoin(base_url,token);parsed=urlparse(resolved)
    if parsed.scheme!="https" or (parsed.hostname or "").lower() not in APPROVED_CRM_HOSTS:return None
    if parsed.username or parsed.password or parsed.fragment or not DETAIL_PATH_RE.fullmatch(parsed.path):return None
    query=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True)
    if set(query)-{"lDID","lPID","Loc"}:return None
    if len(query.get("lDID",[]))!=1 or len(query.get("lPID",[]))!=1:return None
    if "Loc" in query and len(query["Loc"])!=1:return None
    opportunity=query["lDID"][0];person=query["lPID"][0]
    if not opportunity.isdigit() or not person.isdigit():return None
    return DetailDestination(opportunity,person,resolved)


def authorize_action(action:str,element:ElementIdentity,*,base_url:str="",expected_opportunity_id:str|None=None)->DetailDestination|None:
    if not element.visible or not element.enabled:raise NavigationBlocked(f"{action}: target hidden or disabled")
    destination=None
    if action=="prospects":
        valid=(element.accessible_name=="Prospects" and element.role=="tab" and element.tag=="H3" and element.dom_id==PROSPECTS_DOM_ID and element.onclick=="aria-controls:86")
    elif action=="active":
        valid=(element.accessible_name=="Active" and element.role=="menuitem" and element.tag=="A" and element.dom_id==ACTIVE_DOM_ID and element.onclick==f"fixed:{ACTIVE_ROUTE}")
    elif action=="days_control":valid=(element.accessible_name=="Days" and element.role=="textbox" and element.tag=="INPUT" and element.dom_id=="slctDateSpan" and element.onclick=="fixed:DaysControl")
    elif action=="all_days":valid=(element.accessible_name=="All" and element.role=="button" and element.tag=="BUTTON" and not element.href and not element.onclick)
    elif action=="refresh":valid=(element.accessible_name=="Refresh" and element.role=="link" and element.tag=="A" and element.href==REFRESH_HREF and element.onclick=="fixed:RefreshGrid")
    elif action=="lead_detail":
        destination=parse_detail_destination(element.onclick or element.href,base_url)
        valid=(element.role=="link" and element.tag=="A" and destination is not None and (expected_opportunity_id is None or destination.opportunity_id==expected_opportunity_id))
    elif action in {"page_info","switch_verified_tab","inspect","scroll","close_detail","restore_results"}:valid=True
    else:valid=False
    if not valid:raise NavigationBlocked(f"{action}: identity or destination is not allowlisted")
    return destination


def normalize_phone(raw:str)->str:
    value=(raw or "").strip()
    if not value or re.search(r"[A-Za-z]",value) or not re.fullmatch(r"\+?[0-9 ()\-.]+",value):return ""
    digits=re.sub(r"\D","",value)
    if value.startswith("+"):
        normalized="+"+digits
    elif len(digits)==10:
        normalized="+1"+digits
    elif len(digits)==11 and digits.startswith("1"):
        normalized="+"+digits
    else:return ""
    return normalized if re.fullmatch(r"\+[1-9]\d{7,14}",normalized) else ""


def verify_search_state(days:str,refresh_confirmed:bool)->None:
    if days!="All":raise NavigationBlocked("Active search scope mismatch")
    if not refresh_confirmed:raise NavigationBlocked("Filter refresh/reload was not verified")


def visible_unique_rows(rows:list[dict],batch_size:int,base_url:str)->list[dict]:
    selected=[];seen=set()
    for row in sorted(rows,key=lambda item:item["visual_order"]):
        if not row.get("visible") or not row.get("enabled"):continue
        destination=parse_detail_destination(row.get("onclick","") or row.get("href",""),base_url)
        if not destination or destination.opportunity_id in seen:continue
        seen.add(destination.opportunity_id);selected.append({**row,"cdk_opportunity_id":destination.opportunity_id,"conversation_thread_id":destination.person_id,"detail_url":destination.url})
        if len(selected)==batch_size:break
    if len(selected)<batch_size:raise NavigationBlocked(f"Only {len(selected)} valid visible unique leads available")
    return selected


def one_new_detail_target(before:set[str],after:list[dict],expected_opportunity_id:str,base_url:str)->dict:
    opened=[target for target in after if target["targetId"] not in before]
    matching=[]
    for target in opened:
        destination=parse_detail_destination(target.get("url",""),base_url)
        if destination and destination.opportunity_id==expected_opportunity_id:matching.append(target)
    if len(opened)!=1 or len(matching)!=1:raise NavigationBlocked("Expected exactly one approved detail target")
    return matching[0]
