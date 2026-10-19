"""Read-only Browser Use launcher for resumable Stage B detail indexing."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "private" / "all-active-discovery.db"


def browser_program(run_id: str, index_path: Path, success_limit: int,
                    stale_seconds: int, timeout: int) -> str:
    values = {"__ROOT__": json.dumps(str(ROOT)), "__RUN_ID__": json.dumps(run_id),
              "__INDEX__": json.dumps(str(index_path)), "__LIMIT__": str(success_limit),
              "__STALE__": str(stale_seconds), "__TIMEOUT__": str(timeout)}
    program = BROWSER_PROGRAM
    for marker, value in values.items():
        program = program.replace(marker, value)
    return program


BROWSER_PROGRAM = r'''
import json,re,sys,time
from datetime import date
from pathlib import Path
ROOT=Path(__ROOT__);sys.path.insert(0,str(ROOT/'scripts'))
from cdk_runtime import NavigationBlocked,approved_top_level_url,normalize_phone,parse_detail_destination
from detail_index import DetailIndex
from identity_vault import IdentityRecord,default_vault
from privacy_gate import sanitize_payload
RUN_ID=__RUN_ID__;INDEX=Path(__INDEX__);LIMIT=__LIMIT__;STALE=__STALE__;DEADLINE=time.monotonic()+__TIMEOUT__
attempted=completed=retryable=terminal=mismatches=tabs_opened=tabs_closed=0;max_detail_tabs=0

def pause(ms=250):cdp('Runtime.evaluate',expression=f'new Promise(r=>setTimeout(r,{int(ms)}))',awaitPromise=True)
def all_frames():
 out=[]
 def walk(tree):
  out.append(tree['frame'])
  for child in tree.get('childFrames',[]):walk(child)
 walk(cdp('Page.getFrameTree')['frameTree']);return out
def unique_frame(predicate,label):
 found=[frame for frame in all_frames() if predicate(frame)]
 if len(found)!=1:raise RuntimeError(label)
 return found[0]
def ax(frame_id):return cdp('Accessibility.getFullAXTree',frameId=frame_id)['nodes']
def role(node):return node.get('role',{}).get('value','')
def name(node):return node.get('name',{}).get('value','')
def context(frame_id):
 value=cdp('Page.createIsolatedWorld',frameId=frame_id,worldName='cdk-detail-index-frame-scope').get('executionContextId')
 if not value:raise RuntimeError('required_content_missing')
 return value
def frame_eval(frame_id,expression):
 result=cdp('Runtime.evaluate',contextId=context(frame_id),expression=expression,returnByValue=True)
 if result.get('exceptionDetails'):raise RuntimeError('required_content_missing')
 return result.get('result',{}).get('value')
def split_name(full):
 if ',' in full:
  last,first=[part.strip() for part in full.split(',',1)]
 else:
  parts=full.split();first=parts[0] if parts else '';last=parts[-1] if len(parts)>1 else ''
 return first,last
def safe_payload(claim,main_cells,history_cells,vehicle,source):
 raw={'lead_key':claim.stable_lead_key,'vehicle':vehicle,'lead_source':source,
      'current_status':next((x for x in history_cells if 'Active -' in x),''),
      'communication_history':history_cells[:60],
      'scheduled_tasks':[x for x in history_cells if any(k in x.casefold() for k in ('follow up','phone follow','email follow'))][:20],
      'appointment_history':[x for x in history_cells if 'appointment' in x.casefold() or 'showroom' in x.casefold()][:20]}
 return sanitize_payload(raw,{claim.recorded_display_name:'[customer]'}).safe_payload
def close_created(canonical_id,before):
 global tabs_closed
 current={tab['targetId']:tab for tab in list_tabs()}
 for target_id in list(current):
  if target_id not in before and target_id!=canonical_id:
   close_tab(target_id);tabs_closed+=1
 remaining={tab['targetId']:tab for tab in list_tabs()}
 if canonical_id not in remaining:raise RuntimeError('canonical tab disappeared')
 switch_tab(canonical_id)

canonical=[tab for tab in list_tabs() if approved_top_level_url(tab.get('url',''))]
if len(canonical)!=1:raise NavigationBlocked('Unique canonical CDK tab missing or ambiguous')
canonical=canonical[0];canonical_id=canonical['targetId'];switch_tab(canonical_id)
index=DetailIndex(INDEX);vault=default_vault(ROOT);recovered=index.recover_stale_claims(STALE)
try:
 while completed<LIMIT and time.monotonic()<DEADLINE:
  claim=index.claim_next(RUN_ID)
  if claim is None:break
  attempted+=1;before={tab['targetId'] for tab in list_tabs()}
  try:
   expected=parse_detail_destination(claim.detail_url,'https://crm.connectcdk.com/')
   if expected is None or expected.opportunity_id!=claim.opportunity_id:raise ValueError('route_rejected')
   new_tab(claim.detail_url);tabs_opened+=1
   detail=None
   for _ in range(80):
    pause(250);opened=[tab for tab in list_tabs() if tab['targetId'] not in before]
    max_detail_tabs=max(max_detail_tabs,len(opened))
    if len(opened)>1:raise RuntimeError('unexpected_tab')
    if len(opened)==1:
     destination=parse_detail_destination(opened[0].get('url',''),'https://crm.connectcdk.com/')
     if destination:
      if destination.opportunity_id!=claim.opportunity_id:raise ValueError('opportunity_mismatch')
      detail=opened[0];break
   if detail is None:raise TimeoutError('browser_timeout')
   switch_tab(detail['targetId']);wait_for_load(timeout=30)
   detail_frame=history_frame=None
   for _ in range(120):
    pause(250)
    frames=all_frames()
    detail_matches=[f for f in frames if parse_detail_destination(f.get('url',''),'https://crm.connectcdk.com/') and parse_detail_destination(f.get('url',''),'https://crm.connectcdk.com/').opportunity_id==claim.opportunity_id]
    history_matches=[f for f in frames if re.search(r'/History\.aspx(?:\?|$)',f.get('url',''),re.I)]
    if len(detail_matches)==1 and len(history_matches)==1:
     detail_frame=detail_matches[0];history_frame=history_matches[0];break
   if detail_frame is None or history_frame is None:raise TimeoutError('history_timeout')
   detail_nodes=ax(detail_frame['id']);history_nodes=ax(history_frame['id'])
   main_cells=[name(n) for n in detail_nodes if role(n) in ('LayoutTableCell','StaticText','option') and name(n)]
   history_cells=[name(n) for n in history_nodes if role(n) in ('cell','LayoutTableCell') and name(n)]
   if not main_cells or not history_cells:raise RuntimeError('required_content_missing')
   phone=frame_eval(detail_frame['id'],r"""(()=>{for(const row of document.querySelectorAll('tr')){const text=(row.innerText||'').trim();for(const label of ['Cell #:','Mobile #:','Home #:','Phone #:'])if(text.startsWith(label))return text.slice(label.length).trim()}return ''})()""") or ''
   vehicle=source=''
   for item in main_cells:
    if 'Vehicle' in item and not vehicle:
     match=re.search(r'Vehicle\s+(.*?)(?:Window Sticker|Stock #|Website VDP|Trade:)',item,re.I);vehicle=match.group(1).strip() if match else ''
    if 'Source:' in item and not source:
     match=re.search(r'Source:\s*(.*?)(?:Sub-source:|Date/Time Due:|$)',item,re.I);source=match.group(1).strip() if match else ''
   first,last=split_name(claim.recorded_display_name)
   identity=IdentityRecord(claim.opportunity_id,first,last,claim.recorded_display_name,normalize_phone(phone),vehicle,expected.person_id)
   identity_key=vault.extraction_upsert(identity)
   if identity_key!=claim.stable_lead_key:raise ValueError('opportunity_mismatch')
   payload=safe_payload(claim,main_cells,history_cells,vehicle,source)
   index.complete(claim,payload,RUN_ID);completed+=1
  except ValueError as error:
   category=str(error) if str(error) in ('route_rejected','opportunity_mismatch') else 'opportunity_mismatch'
   index.fail(claim,category,terminal=True);terminal+=1;mismatches+=int(category=='opportunity_mismatch')
  except TimeoutError as error:
   category=str(error) if str(error) in ('browser_timeout','history_timeout') else 'browser_timeout'
   index.fail(claim,category,terminal=False);retryable+=1
  except RuntimeError as error:
   category=str(error) if str(error) in ('unexpected_tab','required_content_missing') else 'required_content_missing'
   index.fail(claim,category,terminal=False);retryable+=1
  except Exception:
   index.fail(claim,'local_write_failed',terminal=False);retryable+=1
  finally:
   close_created(canonical_id,before)
   # Reacquire the canonical target and discard all frame/object state before the next claim.
   canonical_now=[tab for tab in list_tabs() if approved_top_level_url(tab.get('url',''))]
   if len(canonical_now)!=1 or canonical_now[0]['targetId']!=canonical_id:raise RuntimeError('canonical verification failed')
 print(json.dumps({'ok':True,'run_id':RUN_ID,'attempted':attempted,'completed':completed,
  'retryable_failures':retryable,'terminal_failures':terminal,'mismatches_rejected':mismatches,
  'tabs_opened':tabs_opened,'tabs_closed':tabs_closed,'max_simultaneous_detail_tabs':max_detail_tabs,
  'stale_claims_recovered':recovered,'discovery_total':index.discovery_count()}))
finally:
 vault.close();index.close()
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--success-limit", type=int, default=5)
    parser.add_argument("--stale-seconds", type=int, default=900)
    parser.add_argument("--browser-timeout", type=int, default=600)
    parser.add_argument("--browser-use-executable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.success_limit < 1 or args.stale_seconds < 1 or args.browser_timeout < 30:
        parser.error("invalid Stage B limits")
    run_id = f"detail-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    program = browser_program(run_id, args.index.resolve(), args.success_limit,
                              args.stale_seconds, args.browser_timeout - 5)
    compile(program, "<cdk-detail-index-driver>", "exec")
    if args.dry_run:
        print("Stage B dry run passed; Browser Use was not started.")
        return 0
    executable = args.browser_use_executable or shutil.which("browser-use")
    if not executable:
        print("Browser Use executable unavailable", file=sys.stderr)
        return 1
    try:
        result = subprocess.run([executable], input=program, text=True, cwd=ROOT,
                                timeout=args.browser_timeout, check=False)
    except subprocess.TimeoutExpired:
        print("Stage B timed out; claims remain recoverable", file=sys.stderr)
        return 124
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
