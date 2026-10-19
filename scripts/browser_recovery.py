"""Bounded, read-only best-effort cleanup after an external browser-driver timeout.

Killing Browser Use can bypass the driver's in-process finally block. Recovery is
not guaranteed: this routine reports success only after it reconnects, closes only
current-run detail targets recorded in recovery-state.json, and restores results.
"""
from __future__ import annotations
import json,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def recovery_program(run_id:str)->str:
    return f'''import json,sys\nfrom pathlib import Path\nROOT=Path({str(ROOT)!r});sys.path.insert(0,str(ROOT/"scripts"))\nfrom cdk_runtime import approved_top_level_url,parse_detail_destination\ns=json.loads((ROOT/"private"/"recovery-state.json").read_text())\nif s.get("run_id")!={run_id!r}:raise RuntimeError("recovery run mismatch")\ntabs={{t["targetId"]:t for t in list_tabs()}};results=s.get("results_target_id");result_target=tabs.get(results);base=s.get("crm_frame_url","");recorded=s.get("opened_detail_target_ids",[])\nif not result_target or not approved_top_level_url(result_target.get("url","")):raise RuntimeError("recorded results target is not approved")\nfor target_id in recorded:\n t=tabs.get(target_id)\n if t and not parse_detail_destination(t.get("url",""),base):raise RuntimeError("recorded detail target is not approved")\nfor target_id in recorded:\n if target_id in tabs:close_tab(target_id)\nafter={{t["targetId"]:t for t in list_tabs()}}\nif any(target_id in after for target_id in recorded):raise RuntimeError("recorded detail target remained open")\nif results not in after or not approved_top_level_url(after[results].get("url","")):raise RuntimeError("results target unavailable after cleanup")\nswitch_tab(results)\nactive=page_info()\nif not approved_top_level_url(active.get("url","")) or active.get("url")!=after[results].get("url"):raise RuntimeError("results target restoration was not verified")\nprint("recovery succeeded")'''
def recover(run_id:str,timeout:int=30)->bool:
    executable=shutil.which("browser-use")
    if not executable:return False
    try:return subprocess.run([executable],input=recovery_program(run_id),text=True,cwd=ROOT,timeout=timeout).returncode==0
    except (subprocess.TimeoutExpired,OSError):return False
