"""Exhaustive read-only Stage B driver for History and Audit Trail panes."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from owned_process import run_owned

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "private" / "all-active-discovery.db"


def browser_program(run_id: str, index_path: Path, success_limit: int, stale_seconds: int, timeout: int) -> str:
    values = {"__ROOT__": json.dumps(str(ROOT)), "__RUN_ID__": json.dumps(run_id),
              "__INDEX__": json.dumps(str(index_path)), "__LIMIT__": str(success_limit),
              "__STALE__": str(stale_seconds), "__TIMEOUT__": str(timeout)}
    program = BROWSER_PROGRAM
    for marker, value in values.items(): program = program.replace(marker, value)
    return program


BROWSER_PROGRAM = r'''
import base64,json,re,sys,time
from pathlib import Path
ROOT=Path(__ROOT__);sys.path.insert(0,str(ROOT/'scripts'))
from activity_history import category_sections,deduplicate_activities
from browser_action_journal import BrowserActionJournal,safe_ref
from cdk_runtime import NavigationBlocked,approved_top_level_url,normalize_phone,parse_detail_destination
from exhaustive_detail_index import ExhaustiveDetailIndex
from identity_vault import IdentityRecord,default_vault
from privacy_gate import sanitize_payload
RUN_ID=__RUN_ID__;INDEX=Path(__INDEX__);LIMIT=__LIMIT__;STALE=__STALE__;DEADLINE=time.monotonic()+__TIMEOUT__
journal=BrowserActionJournal(ROOT,RUN_ID)
attempted=completed=retryable=terminal=mismatches=tabs_opened=tabs_closed=0;max_detail_tabs=0

def pause(ms=250):cdp('Runtime.evaluate',expression=f'new Promise(r=>setTimeout(r,{int(ms)}))',awaitPromise=True)
def capture(stage,label):
 path=ROOT/'logs'/f'browser-actions-{RUN_ID}-{label}.png'
 try:path.write_bytes(base64.b64decode(cdp('Page.captureScreenshot',format='png')['data']));journal.log_screenshot(stage=stage,screenshot_path=path,captured=True)
 except Exception:journal.log_screenshot(stage=stage,screenshot_path=None,captured=False,error_category='screenshot_failed')
def frames():
 out=[]
 def walk(tree):out.append(tree['frame']);[walk(child) for child in tree.get('childFrames',[])]
 walk(cdp('Page.getFrameTree')['frameTree']);return out
def context(frame_id):
 value=cdp('Page.createIsolatedWorld',frameId=frame_id,worldName='cdk-exhaustive-detail-frame-scope').get('executionContextId')
 if not value:raise RuntimeError('required_content_missing')
 return value
def evaluate(frame_id,expression,gesture=False):
 handle=journal.start_action(stage='detail_dom',intended_action='Execute verified frame-scoped browser gesture',action_type='click_or_scroll',state_before={'frame_id':safe_ref(frame_id)},selector_strategy='fresh frame-scoped isolated execution context') if gesture else None
 result=cdp('Runtime.evaluate',contextId=context(frame_id),expression=expression,returnByValue=True,userGesture=gesture)
 if result.get('exceptionDetails'):
  capture('detail_dom','verification-failed-'+str(journal.sequence))
  if handle:journal.fail_action(handle,state_after={'exception':True},verification_condition='DOM expression completed without exception',error_category='required_content_missing',retryable=True)
  raise RuntimeError('required_content_missing')
 value=result.get('result',{}).get('value')
 if handle:journal.succeed_action(handle,state_after={'command_returned':True},verification_condition='DOM command returned; caller independently verifies state',verification_result=True)
 return value
def target_frame(token):
 found=[f for f in frames() if f.get('name')=='tabsTargetFrame' and token.casefold() in f.get('url','').casefold()]
 return found[0] if len(found)==1 else None
def main_frame(expected_id):
 found=[]
 for frame in frames():
  destination=parse_detail_destination(frame.get('url',''),'https://crm.connectcdk.com/')
  if destination and destination.opportunity_id==expected_id:found.append(frame)
 if len(found)!=1:raise RuntimeError('required_content_missing')
 return found[0]
def wait_target(token):
 for _ in range(120):
  pause();frame=target_frame(token)
  if frame:
   ready=evaluate(frame['id'],"document.readyState")
   if ready=='complete':return frame
 raise TimeoutError('history_timeout')
def pane_snapshot(token,kind):
 frame=target_frame(token)
 if not frame:raise TimeoutError('history_timeout')
 expression=r"""(()=>{const s=document.scrollingElement||document.documentElement,v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);let rows=[];if('__K__'==='history'){const stream=document.querySelector('#gvOpptyHistory table');rows=[...document.querySelectorAll('#gvScheduled > tbody > tr'),...(stream?[...stream.querySelectorAll(':scope > tbody > tr')]:[])]}else rows=[...document.querySelectorAll('#GridViewAuditTrail > tbody > tr')];const paging=[...document.querySelectorAll('a,button,input')].filter(e=>v(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&/^(next|load more|show more)$/i.test((e.innerText||e.value||e.getAttribute('aria-label')||'').trim())).map(e=>(e.innerText||e.value||e.getAttribute('aria-label')||'').trim());return {rows:rows.filter(v).map(r=>(r.innerText||r.textContent||'').trim().replace(/\s+/g,' ')).filter(Boolean),top:s.scrollTop,ch:s.clientHeight,sh:s.scrollHeight,loading:[...document.querySelectorAll('[aria-busy=true],[class*=loading],[class*=Loading],[id*=loading],[id*=Loading]')].filter(v).length,paging}})()""".replace('__K__',kind)
 return evaluate(frame['id'],expression)
def click_pager(token,label):
 frame=target_frame(token);wanted=json.dumps(label)
 return evaluate(frame['id'],r"""(()=>{const wanted="""+wanted+r""",v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),xs=[...document.querySelectorAll('a,button,input')].filter(e=>v(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&(e.innerText||e.value||e.getAttribute('aria-label')||'').trim()===wanted);if(xs.length!==1)throw Error('pager ambiguous');xs[0].click();return true})()""",True)
def scan_pane(token,kind):
 seen={};cycles=no_new=page_actions=0;s=pane_snapshot(token,kind);initial=len(s['rows']);last_rows=tuple(s['rows'])
 def absorb(state):
  nonlocal no_new
  before=len(seen)
  for text in state['rows']:seen.setdefault(text,text)
  no_new=no_new+1 if len(seen)==before else 0
 absorb(s)
 while cycles<120 and time.monotonic()<DEADLINE:
  frame=target_frame(token);before=s['top'];amount=max(24,s['ch']*.8);pane_handle=journal.start_action(stage='detail_'+kind,intended_action='Scroll '+kind+' iframe document',action_type='scroll',state_before={'scrollTop':s['top'],'clientHeight':s['ch'],'scrollHeight':s['sh'],'first_identifier':safe_ref(last_rows[0]) if last_rows else '','last_identifier':safe_ref(last_rows[-1]) if last_rows else ''},frame_name=frame.get('name','tabsTargetFrame'),frame_url=frame.get('url',''),selector_strategy='document.scrollingElement',target={'tag':'HTML','id':'','role':'document','label':kind})
  evaluate(frame['id'],f"(()=>{{const s=document.scrollingElement||document.documentElement;s.scrollTop=Math.min(s.scrollHeight-s.clientHeight,s.scrollTop+{amount});s.dispatchEvent(new Event('scroll'));return s.scrollTop}})()",True)
  changed=False
  for _ in range(20):
   pause(200);n=pane_snapshot(token,kind)
   if tuple(n['rows'])!=last_rows or n['top']>before+1 or n['sh']!=s['sh']:changed=True;break
  n=pane_snapshot(token,kind);absorb(n);cycles+=1;near=n['top']+n['ch']>=n['sh']-3;stuck=n['top']<=before+1;same=tuple(n['rows'])==last_rows;journal.succeed_action(pane_handle,state_after={'scrollTop':n['top'],'clientHeight':n['ch'],'scrollHeight':n['sh'],'first_identifier':safe_ref(n['rows'][0]) if n['rows'] else '','last_identifier':safe_ref(n['rows'][-1]) if n['rows'] else '','new':len(seen),'rendered_content_changed':not same},verification_condition='pane state reacquired after scroll',verification_result=True,action_type='scroll',scroll_metrics={'scrollTop_before':before,'scrollTop_after':n['top'],'clientHeight':n['ch'],'scrollHeight':n['sh'],'rendered_content_changed':not same,'true_end_signals':{'near_bottom':near,'no_new_cycles':no_new,'loading':n['loading']}});last_rows=tuple(n['rows']);s=n
  if near and stuck and no_new>=3 and not s['loading']:
   if s['paging']:
    click_pager(token,s['paging'][0]);page_actions+=1;pause(400);after=pane_snapshot(token,kind)
    if tuple(after['rows'])!=last_rows:absorb(after);last_rows=tuple(after['rows']);s=after;continue
   frame=target_frame(token);evaluate(frame['id'],"(()=>{const s=document.scrollingElement||document.documentElement;s.scrollTop=Math.min(s.scrollHeight-s.clientHeight,s.scrollTop+8);s.dispatchEvent(new Event('scroll'));return s.scrollTop})()",True);pause(300);final=pane_snapshot(token,kind);before_count=len(seen);prior=last_rows;absorb(final)
   proven=final['top']<=s['top']+1 and len(seen)==before_count and tuple(final['rows'])==prior and final['loading']==0 and not final['paging']
   return {'rows':list(seen),'initial_count':initial,'final_count':len(seen),'scroll_cycles':cycles+1,'pagination_actions':page_actions,'true_end':proven,'container_selector':'document.scrollingElement','resume_scroll_top':final['top']}
 raise TimeoutError('history_timeout')
def split_name(full):
 if ',' in full:last,first=[part.strip() for part in full.split(',',1)]
 else:parts=full.split();first=parts[0] if parts else '';last=parts[-1] if len(parts)>1 else ''
 return first,last
def close_created(canonical_id,before):
 global tabs_closed
 current={tab['targetId']:tab for tab in list_tabs()}
 for target_id in list(current):
  if target_id not in before and target_id!=canonical_id:close_tab(target_id);tabs_closed+=1
 remaining={tab['targetId']:tab for tab in list_tabs()}
 if canonical_id not in remaining:raise RuntimeError('canonical tab disappeared')
 switch_tab(canonical_id)

canonical_handle=journal.start_action(stage='detail_navigation',intended_action='Locate unique canonical Applications tab',action_type='poll',state_before={})
canonical=[tab for tab in list_tabs() if approved_top_level_url(tab.get('url',''))]
if len(canonical)!=1:raise NavigationBlocked('Unique canonical CDK tab missing or ambiguous')
canonical=canonical[0];canonical_id=canonical['targetId'];journal.succeed_action(canonical_handle,state_after={'canonical_count':1},verification_condition='exactly one canonical Applications tab',verification_result=True,target_tab_id=safe_ref(canonical_id),top_level_url_after=canonical.get('url',''));switch_tab(canonical_id)
index=ExhaustiveDetailIndex(INDEX);vault=default_vault(ROOT);recovered=index.recover_stale_claims(STALE)
try:
 while completed<LIMIT and time.monotonic()<DEADLINE:
  claim_handle=journal.start_action(stage='detail_claim',intended_action='Atomically claim next eligible record',action_type='poll',state_before={})
  claim=index.claim_next(RUN_ID)
  if claim is None:
   journal.succeed_action(claim_handle,state_after={'eligible_record':False},verification_condition='no eligible claim remains',verification_result=True);break
  journal.succeed_action(claim_handle,state_after={'eligible_record':True,'stable_identifier':safe_ref(claim.stable_lead_key)},verification_condition='atomic claim acquired',verification_result=True,expected_opportunity_id=claim.opportunity_id)
  attempted+=1;before={tab['targetId'] for tab in list_tabs()};lead_report={'sequence':attempted,'tabs_discovered':['History','Audit Trail'],'tabs_fully_indexed':[],'sections':{}}
  try:
   expected=parse_detail_destination(claim.detail_url,'https://crm.connectcdk.com/')
   if expected is None or expected.opportunity_id!=claim.opportunity_id:raise ValueError('route_rejected')
   capture('detail_navigation','before-detail-'+safe_ref(claim.stable_lead_key));open_handle=journal.start_action(stage='detail_navigation',intended_action='Open exactly one claimed detail tab',action_type='open tab',state_before={'stable_identifier':safe_ref(claim.stable_lead_key)},target_tab_id=safe_ref(canonical_id),top_level_url_before=canonical.get('url',''))
   new_tab(claim.detail_url);tabs_opened+=1;detail=None
   for _ in range(80):
    pause();opened=[tab for tab in list_tabs() if tab['targetId'] not in before];max_detail_tabs=max(max_detail_tabs,len(opened))
    if len(opened)>1:raise RuntimeError('unexpected_tab')
    if len(opened)==1:
     destination=parse_detail_destination(opened[0].get('url',''),'https://crm.connectcdk.com/')
     if destination:
      if destination.opportunity_id!=claim.opportunity_id:raise ValueError('opportunity_mismatch')
      detail=opened[0];break
   if not detail:raise TimeoutError('browser_timeout')
   journal.succeed_action(open_handle,state_after={'detail_tabs':1,'detail_tab_id':safe_ref(detail['targetId'])},verification_condition='one approved detail tab opened with expected opportunity',verification_result=True,target_tab_id=safe_ref(detail['targetId']),top_level_url_after=detail.get('url',''))
   switch_tab(detail['targetId']);wait_for_load(timeout=30);capture('detail_navigation','after-detail-'+safe_ref(claim.stable_lead_key));main=main_frame(claim.opportunity_id);history_frame=wait_target('history.aspx');route_handle=journal.start_action(stage='detail_history',intended_action='Verify detail route, opportunity, and History iframe',action_type='wait/poll',state_before={'expected_opportunity_id':claim.opportunity_id});journal.succeed_action(route_handle,state_after={'route_verified':True,'opportunity_verified':True,'history_iframe':True},verification_condition='approved route and matching opportunity with history.aspx loaded',verification_result=True,target_tab_id=safe_ref(detail['targetId']),frame_name=history_frame.get('name','tabsTargetFrame'),frame_url=history_frame.get('url',''))
   statuses=index.section_statuses(claim);identities={claim.recorded_display_name:'[customer]'}
   if not all(statuses.get(name) in ('complete','empty') for name in ('calls','emails','texts','notes','appointments','tasks','activities')):
    history=scan_pane('history.aspx','history');activities=deduplicate_activities(history.pop('rows'),'history',identities);scan={'scroll_cycles':history['scroll_cycles'],'true_end':history['true_end'],'container_selector':history['container_selector'],'pagination_detected':history['pagination_actions']>0,'resume_scroll_top':history['resume_scroll_top']}
    if not history['true_end']:raise TimeoutError('history_timeout')
    sections=category_sections(activities,scan);index.save_sections(claim,sections,activities);lead_report['tabs_fully_indexed'].append('History');lead_report['sections'].update({s['section_name']:{'count':s['activity_count'],'cycles':s['scroll_cycles'],'true_end':s['true_end']} for s in sections});history_handle=journal.start_action(stage='detail_history',intended_action='Verify History true end',action_type='verification',state_before={'scroll_cycles':history['scroll_cycles']});journal.succeed_action(history_handle,state_after={'activity_count':history['final_count'],'true_end':history['true_end']},verification_condition='multi-signal History true-end proof',verification_result=history['true_end'])
   main=main_frame(claim.opportunity_id);audit_handle=journal.start_action(stage='detail_audit',intended_action='Open and verify Audit Trail',action_type='click',state_before={'selected':False},selector_strategy='unique visible #liAuditTrail',target={'tag':'LI','id':'liAuditTrail','role':'tab','label':'Audit Trail'});evaluate(main['id'],"(()=>{const xs=[...document.querySelectorAll('#liAuditTrail')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(xs.length!==1)throw Error('audit tab ambiguous');(xs[0].querySelector('a')||xs[0]).click();return true})()",True)
   audit_frame=wait_target('CustomerAuditTrail.aspx');main=main_frame(claim.opportunity_id);selected=evaluate(main['id'],"(()=>{const e=document.querySelector('#liAuditTrail');return !!e&&(e.className||'').includes('selected')})()");journal.succeed_action(audit_handle,state_after={'selected':selected,'audit_iframe':bool(audit_frame)},verification_condition='Audit Trail selected and CustomerAuditTrail.aspx loaded',verification_result=bool(selected and audit_frame))
   if not selected:raise RuntimeError('required_content_missing')
   statuses=index.section_statuses(claim)
   if statuses.get('audit_trail') not in ('complete','empty'):
    audit=scan_pane('CustomerAuditTrail.aspx','audit');audit_activities=deduplicate_activities(audit.pop('rows'),'audit_trail',identities)
    if not audit['true_end']:raise TimeoutError('history_timeout')
    audit_end_handle=journal.start_action(stage='detail_audit',intended_action='Verify Audit Trail true end',action_type='verification',state_before={'scroll_cycles':audit['scroll_cycles']});journal.succeed_action(audit_end_handle,state_after={'activity_count':audit['final_count'],'true_end':audit['true_end']},verification_condition='multi-signal Audit Trail true-end proof',verification_result=audit['true_end'])
    section={'section_name':'audit_trail','status':'complete' if audit_activities else 'empty','activity_count':len(audit_activities),'scroll_cycles':audit['scroll_cycles'],'true_end':True,'container_selector':audit['container_selector'],'pagination_detected':audit['pagination_actions']>0,'resume_scroll_top':audit['resume_scroll_top']};index.save_sections(claim,[section],audit_activities);lead_report['tabs_fully_indexed'].append('Audit Trail');lead_report['sections']['audit_trail']={'count':len(audit_activities),'cycles':audit['scroll_cycles'],'true_end':True}
   detail_nodes=cdp('Accessibility.getFullAXTree',frameId=main_frame(claim.opportunity_id)['id'])['nodes'];cells=[n.get('name',{}).get('value','') for n in detail_nodes if n.get('role',{}).get('value','') in ('LayoutTableCell','StaticText','option') and n.get('name',{}).get('value')]
   vehicle=source=''
   for item in cells:
    if 'Vehicle' in item and not vehicle:
     match=re.search(r'Vehicle\s+(.*?)(?:Window Sticker|Stock #|Website VDP|Trade:)',item,re.I);vehicle=match.group(1).strip() if match else ''
    if 'Source:' in item and not source:
     match=re.search(r'Source:\s*(.*?)(?:Sub-source:|Date/Time Due:|$)',item,re.I);source=match.group(1).strip() if match else ''
   phone=evaluate(main_frame(claim.opportunity_id)['id'],r"""(()=>{for(const row of document.querySelectorAll('tr')){const text=(row.innerText||'').trim();for(const label of ['Cell #:','Mobile #:','Home #:','Phone #:'])if(text.startsWith(label))return text.slice(label.length).trim()}return ''})()""") or ''
   first,last=split_name(claim.recorded_display_name);identity=IdentityRecord(claim.opportunity_id,first,last,claim.recorded_display_name,normalize_phone(phone),vehicle,expected.person_id)
   if vault.extraction_upsert(identity)!=claim.stable_lead_key:raise ValueError('opportunity_mismatch')
   report=index.section_report(claim.stable_lead_key);payload={'lead_key':claim.stable_lead_key,'vehicle':vehicle,'lead_source':source,'activity_count':index.activity_count(claim.stable_lead_key),'sections':[{k:v for k,v in row.items() if k not in ('container_selector','last_error_category')} for row in report]};payload=sanitize_payload(payload,identities).safe_payload;gate_handle=journal.start_action(stage='detail_completion',intended_action='Apply exhaustive completion gate',action_type='verification',state_before={'stable_identifier':safe_ref(claim.stable_lead_key)});index.complete(claim,payload,RUN_ID);completed+=1;journal.succeed_action(gate_handle,state_after={'detail_complete':True},verification_condition='all required sections complete',verification_result=True)
   print(json.dumps({'lead_complete':lead_report}))
  except ValueError as error:
   capture('detail_failure','terminal-'+str(journal.sequence));failure_handle=journal.start_action(stage='detail_failure',intended_action='Reject terminal detail failure',action_type='verification',state_before={'stable_identifier':safe_ref(claim.stable_lead_key)});journal.fail_action(failure_handle,state_after={},verification_condition='detail record rejected safely',error_category='opportunity_mismatch',retryable=False)
   category=str(error) if str(error) in ('route_rejected','opportunity_mismatch') else 'opportunity_mismatch';index.fail(claim,category,terminal=True);terminal+=1;mismatches+=int(category=='opportunity_mismatch')
  except TimeoutError:
   capture('detail_failure','retryable-'+str(journal.sequence));failure_handle=journal.start_action(stage='detail_failure',intended_action='Record retryable detail timeout',action_type='verification',state_before={'stable_identifier':safe_ref(claim.stable_lead_key)});journal.fail_action(failure_handle,state_after={},verification_condition='retry state persisted',error_category='history_timeout',retryable=True)
   index.fail(claim,'history_timeout',terminal=False);retryable+=1
  except RuntimeError as error:
   category='unexpected_tab' if str(error)=='unexpected_tab' else 'required_content_missing';index.fail(claim,category,terminal=False);retryable+=1
  except Exception:
   index.fail(claim,'local_write_failed',terminal=False);retryable+=1
  finally:
   close_handle=journal.start_action(stage='detail_cleanup',intended_action='Close detail tab and restore canonical Applications tab',action_type='close tab',state_before={'open_tabs':len(list_tabs())});close_created(canonical_id,before);current=[tab for tab in list_tabs() if approved_top_level_url(tab.get('url',''))];journal.succeed_action(close_handle,state_after={'canonical_tabs':len(current)},verification_condition='detail tab closed and unique canonical tab restored',verification_result=len(current)==1 and current[0]['targetId']==canonical_id,target_tab_id=safe_ref(canonical_id))
   if len(current)!=1 or current[0]['targetId']!=canonical_id:raise RuntimeError('canonical verification failed')
 print(json.dumps({'ok':True,'run_id':RUN_ID,'attempted':attempted,'completed':completed,'retryable_failures':retryable,'terminal_failures':terminal,'mismatches_rejected':mismatches,'tabs_opened':tabs_opened,'tabs_closed':tabs_closed,'max_simultaneous_detail_tabs':max_detail_tabs,'stale_claims_recovered':recovered,'discovery_total':index.discovery_count()}))
finally:vault.close();index.close();journal.close()
'''


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--index",type=Path,default=DEFAULT_INDEX);parser.add_argument("--success-limit",type=int,default=5);parser.add_argument("--stale-seconds",type=int,default=900);parser.add_argument("--browser-timeout",type=int,default=900);parser.add_argument("--browser-use-executable");parser.add_argument("--dry-run",action="store_true");args=parser.parse_args()
    if args.success_limit<1 or args.stale_seconds<1 or args.browser_timeout<30:parser.error("invalid exhaustive Stage B limits")
    run_id=f"exhaustive-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}";program=browser_program(run_id,args.index.resolve(),args.success_limit,args.stale_seconds,args.browser_timeout-5);compile(program,"<cdk-exhaustive-detail-driver>","exec")
    if args.dry_run:print("Exhaustive Stage B dry run passed; Browser Use was not started.");return 0
    executable=args.browser_use_executable or shutil.which("browser-use")
    if not executable:print("Browser Use executable unavailable",file=sys.stderr);return 1
    result=run_owned([executable],cwd=ROOT,stdin_text=program,timeout=args.browser_timeout,journal_path=ROOT/"logs"/f"worker-lifecycle-{run_id}.jsonl")
    if not result.shutdown_confirmed:print("SAFETY STOP: owned Browser Use process tree exit could not be confirmed",file=sys.stderr);return 125
    if result.returncode:print("Exhaustive Stage B stopped; all owned worker processes exited",file=sys.stderr)
    return result.returncode


if __name__=="__main__":raise SystemExit(main())
