"""Read-only Stage A discovery for the virtualized CDK All-Active grid."""
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


def browser_program(run_id: str, index_path: Path, max_unique: int, max_cycles: int, timeout: int) -> str:
    replacements = {
        "__ROOT__": json.dumps(str(ROOT)), "__RUN_ID__": json.dumps(run_id),
        "__INDEX__": json.dumps(str(index_path)), "__MAX_UNIQUE__": str(max_unique),
        "__MAX_CYCLES__": str(max_cycles), "__TIMEOUT__": str(timeout),
    }
    program = BROWSER_PROGRAM
    for marker, value in replacements.items():
        program = program.replace(marker, value)
    return program


BROWSER_PROGRAM = r'''
import base64,hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__ROOT__);sys.path.insert(0,str(ROOT/'scripts'))
from cdk_runtime import NavigationBlocked,approved_top_level_url,is_active_results_url,parse_detail_destination,select_crm_shell_frame,select_main_frame
from browser_action_journal import BrowserActionJournal,safe_ref
from discovery_index import DiscoveredLead,DiscoveryIndex
from virtualized_discovery import EndDetector,ScrollObservation
RUN_ID=__RUN_ID__;INDEX=Path(__INDEX__);MAX_UNIQUE=__MAX_UNIQUE__;MAX_CYCLES=__MAX_CYCLES__;DEADLINE=time.monotonic()+__TIMEOUT__
journal=BrowserActionJournal(ROOT,RUN_ID)

def pause(ms=250):cdp('Runtime.evaluate',expression=f'new Promise(r=>setTimeout(r,{int(ms)}))',awaitPromise=True)
def capture(stage,label):
 path=ROOT/'logs'/f'browser-actions-{RUN_ID}-{label}.png'
 try:path.write_bytes(base64.b64decode(cdp('Page.captureScreenshot',format='png')['data']));journal.log_screenshot(stage=stage,screenshot_path=path,captured=True)
 except Exception:journal.log_screenshot(stage=stage,screenshot_path=None,captured=False,error_category='screenshot_failed')
def frames():
 out=[]
 def walk(tree):
  out.append(tree['frame'])
  for child in tree.get('childFrames',[]):walk(child)
 walk(cdp('Page.getFrameTree')['frameTree']);return out
def context(frame_id):
 value=cdp('Page.createIsolatedWorld',frameId=frame_id,worldName='cdk-discovery-frame-scope').get('executionContextId')
 if not value:raise NavigationBlocked('Frame-scoped execution context unavailable')
 return value
def evaluate(frame_id,expression,gesture=False):
 handle=journal.start_action(stage='browser_dom',intended_action='Execute verified frame-scoped browser gesture',action_type='click_or_scroll',state_before={'frame_id':safe_ref(frame_id)},selector_strategy='fresh frame-scoped isolated execution context') if gesture else None
 result=cdp('Runtime.evaluate',contextId=context(frame_id),expression=expression,returnByValue=True,userGesture=gesture)
 if result.get('exceptionDetails'):
  capture('browser_dom','verification-failed-'+str(journal.sequence))
  if handle:journal.fail_action(handle,state_after={'exception':True},verification_condition='DOM expression completed without exception',error_category='verification_failed',retryable=True)
  raise NavigationBlocked('Frame-scoped DOM operation failed')
 value=result.get('result',{}).get('value')
 if handle:journal.succeed_action(handle,state_after={'command_returned':True},verification_condition='DOM command returned; caller must independently verify state',verification_result=True)
 return value
def main(active=False):return select_main_frame(frames(),require_active=active)
def shell():return select_crm_shell_frame(frames())
def unique_tab():
 found=[tab for tab in list_tabs() if approved_top_level_url(tab.get('url',''))]
 if len(found)!=1:raise NavigationBlocked('Unique canonical CDK tab missing or ambiguous')
 return found[0]
def wait_active():
 for _ in range(60):
  pause(250)
  try:return main(True)
  except NavigationBlocked:pass
 raise NavigationBlocked('Exact Active route did not load')
def loading(frame_id):return bool(evaluate(frame_id,"""(()=>[...document.querySelectorAll('[aria-busy=true],[class*=loading],[class*=Loading],[id*=loading],[id*=Loading]')].some(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)))()"""))
def grid_ready(frame_id):
 return evaluate(frame_id,"""(()=>{const v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),g=[...document.querySelectorAll('table.k-selectable')].filter(v),d=[...document.querySelectorAll('input#slctDateSpan')].filter(v);return {grids:g.length,rows:g.length==1?[...g[0].querySelectorAll('tbody tr')].filter(v).length:0,days:d.length==1?d[0].value:'',loading:[...document.querySelectorAll('[aria-busy=true],[class*=loading],[class*=Loading],[id*=loading],[id*=Loading]')].filter(v).length}})()""")
def wait_grid():
 for _ in range(120):
  pause(250);f=main(True);state=grid_ready(f['id'])
  if state and state['grids']==1 and state['rows']>0 and state['days']=='All' and state['loading']==0:return f,state
 raise NavigationBlocked('All-Active grid did not become ready')
def scan(frame):
 raw=evaluate(frame['id'],r"""(()=>{const v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),gs=[...document.querySelectorAll('table.k-selectable')].filter(v);if(gs.length!==1)throw Error('grid ambiguous');return [...gs[0].querySelectorAll('tbody tr')].filter(v).map((tr,i)=>{const links=[...tr.querySelectorAll('a')].filter(a=>v(a)&&((a.getAttribute('onclick')||'')+(a.getAttribute('href')||'')).includes('OpptyDetails.aspx'));if(links.length!==1)return {malformed:true,position:String(i)};const a=links[0],r=tr.getBoundingClientRect();return {name:(a.innerText||a.textContent||'').trim(),onclick:a.getAttribute('onclick')||'',href:a.getAttribute('href')||'',position:`${Math.round(r.top)}:${i}`}})})()""") or []
 rows=[];malformed=0;ids=[]
 for item in raw:
  if item.get('malformed'):malformed+=1;continue
  destination=parse_detail_destination(item.get('onclick') or item.get('href'),frame['url'])
  if not destination or not item.get('name'):malformed+=1;continue
  ids.append(destination.opportunity_id)
  rows.append(DiscoveredLead(destination.opportunity_id,destination.url,item['name'],item['position']))
 return rows,tuple(ids),malformed
def candidates(frame):
 return evaluate(frame['id'],r"""(()=>{const v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),gs=[...document.querySelectorAll('table.k-selectable')].filter(v);if(gs.length!==1)throw Error('grid ambiguous');const out=[];let e=gs[0].parentElement,depth=1;while(e&&e!==document.documentElement){const s=getComputedStyle(e);if(e.scrollHeight>e.clientHeight+2&&['auto','scroll','overlay'].includes(s.overflowY))out.push({depth,tag:e.tagName,id:e.id||'',classes:(e.className||'').toString().split(/\s+/).filter(Boolean).slice(0,4),overflowY:s.overflowY,scrollTop:e.scrollTop,clientHeight:e.clientHeight,scrollHeight:e.scrollHeight});e=e.parentElement;depth++}return out})()""") or []
def metrics(frame,depth):
 expression=f"""(()=>{{const g=[...document.querySelectorAll('table.k-selectable')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(g.length!==1)throw Error('grid ambiguous');let e=g[0];for(let i=0;i<{depth};i++)e=e.parentElement;if(!e)throw Error('scroll ancestor missing');const s=getComputedStyle(e);return {{scrollTop:e.scrollTop,clientHeight:e.clientHeight,scrollHeight:e.scrollHeight,overflowY:s.overflowY}}}})()"""
 return evaluate(frame['id'],expression)
def scroll(frame,depth,amount):
 expression=f"""(()=>{{const g=[...document.querySelectorAll('table.k-selectable')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(g.length!==1)throw Error('grid ambiguous');let e=g[0];for(let i=0;i<{depth};i++)e=e.parentElement;if(!e)throw Error('scroll ancestor missing');const before=e.scrollTop;e.scrollTop=Math.min(e.scrollHeight-e.clientHeight,e.scrollTop+{float(amount)});e.dispatchEvent(new Event('scroll',{{bubbles:true}}));return {{before,after:e.scrollTop}}}})()"""
 return evaluate(frame['id'],expression,True)
def ordered_viewport_ids(frame,depth):
 expression=f"""(()=>{{const v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),gs=[...document.querySelectorAll('table.k-selectable')].filter(v);if(gs.length!==1)throw Error('grid ambiguous');let c=gs[0];for(let i=0;i<{depth};i++)c=c.parentElement;if(!c)throw Error('scroll ancestor missing');const cr=c.getBoundingClientRect();return [...gs[0].querySelectorAll('tbody tr')].filter(tr=>{{const r=tr.getBoundingClientRect();return v(tr)&&r.bottom>cr.top&&r.top<cr.bottom}}).map(tr=>{{const a=[...tr.querySelectorAll('a')].find(x=>((x.getAttribute('onclick')||'')+(x.getAttribute('href')||'')).includes('OpptyDetails.aspx'));return a?((((a.getAttribute('onclick')||'')+(a.getAttribute('href')||'')).match(/(?:[?&])lDID=([0-9]+)/)||[])[1]||''):''}}).filter(Boolean)}})()"""
 return tuple(evaluate(frame['id'],expression) or [])
def hashed(value):return hashlib.sha256(value.encode()).hexdigest()[:12] if value else ''
def persist(db,frame,cycle):
 rows,ids,bad=scan(frame);new,dupes,malformed=db.persist_rows(rows,RUN_ID);malformed+=bad
 return ids,new,dupes,malformed

handle=journal.start_action(stage='discovery_navigation',intended_action='Locate unique canonical Applications tab',action_type='poll',state_before={})
tab=unique_tab();journal.succeed_action(handle,state_after={'canonical_count':1},verification_condition='exactly one canonical tab',verification_result=True,target_tab_id=safe_ref(tab['targetId']),top_level_url_after=tab.get('url',''))
handle=journal.start_action(stage='discovery_navigation',intended_action='Switch canonical Applications tab',action_type='switch tab',state_before={'top_url':tab.get('url','')},target_tab_id=safe_ref(tab['targetId']))
switch_tab(tab['targetId']);info=page_info();journal.succeed_action(handle,state_after={'top_url':info.get('url','')},verification_condition='canonical tab is active',verification_result=info.get('url')==tab.get('url'),target_tab_id=safe_ref(tab['targetId']))
if info.get('url')!=tab.get('url'):capture('discovery_navigation','canonical-switch-failed');raise NavigationBlocked('Canonical CDK tab switch not verified')
capture('discovery_navigation','before-active-navigation')
f=main(False)
if not is_active_results_url(f['url']):
 crm=shell();h=journal.start_action(stage='active_navigation',intended_action='Click Prospects menu',action_type='click',state_before={'main_route':f['url']},frame_name=crm.get('name',''),frame_url=crm.get('url',''),selector_strategy='unique visible #MenuSections_SectionLabel_2 exact Prospects',target={'tag':'SPAN','id':'MenuSections_SectionLabel_2','role':'tab label','label':'Prospects'});opened=evaluate(crm['id'],"""(()=>{const xs=[...document.querySelectorAll('#MenuSections_SectionLabel_2')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(xs.length!==1)throw Error('Prospects ambiguous');const tab=xs[0].closest('[role=tab]');if(!tab)throw Error('Prospects tab missing');const before=tab.getAttribute('aria-expanded');if(before!=='true')xs[0].click();return before})()""",True);pause(250);crm=shell();menu_open=evaluate(crm['id'],"document.querySelector('#MenuSections_SectionLabel_2').closest('[role=tab]').getAttribute('aria-expanded')==='true'");journal.succeed_action(h,state_after={'menu_open':menu_open},verification_condition='Prospects menu aria-expanded true',verification_result=menu_open)
 h=journal.start_action(stage='active_navigation',intended_action='Click exact Active menu item',action_type='click',state_before={'menu_open':menu_open},frame_name=crm.get('name',''),frame_url=crm.get('url',''),selector_strategy='unique visible exact Active menu link',target={'tag':'A','id':'MenuSections_MenuSectionItems_2_MenuSectionLink_2','role':'menuitem','label':'Active'});clicked=evaluate(crm['id'],"""(()=>{const xs=[...document.querySelectorAll('#MenuSections_MenuSectionItems_2_MenuSectionLink_2')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)&&(e.innerText||e.textContent||'').trim()==='Active');if(xs.length!==1)throw Error('Active ambiguous');xs[0].click();return true})()""",True)
 f=wait_active();journal.succeed_action(h,state_after={'main_route':f['url']},verification_condition='exact Active route reached',verification_result=is_active_results_url(f['url']))
else:f=main(True)
days=evaluate(f['id'],"""(()=>{const xs=[...document.querySelectorAll('input#slctDateSpan')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(xs.length!==1||xs[0].name!=='multi-select-dropdown')throw Error('Days ambiguous');return xs[0].value})()""")
if days!='All':
 h=journal.start_action(stage='days_filter',intended_action='Click Days input',action_type='click',state_before={'days':days},frame_name=f.get('name','Main'),frame_url=f.get('url',''),selector_strategy='input#slctDateSpan',target={'tag':'INPUT','id':'slctDateSpan','role':'combobox','label':'Days'});evaluate(f['id'],"""(()=>{const x=document.querySelector('input#slctDateSpan');x.click();return true})()""",True);pause(200);f=main(True);dropdown=evaluate(f['id'],"(()=>[...document.querySelectorAll('button')].some(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)&&(e.innerText||e.textContent||'').trim()==='All'))()");journal.succeed_action(h,state_after={'dropdown_open':dropdown},verification_condition='unique visible All button available',verification_result=dropdown)
 h=journal.start_action(stage='days_filter',intended_action='Click exact All option',action_type='select option',state_before={'dropdown_open':dropdown},frame_name=f.get('name','Main'),frame_url=f.get('url',''),selector_strategy='unique visible BUTTON exact normalized text All',target={'tag':'BUTTON','id':'','role':'button','label':'All'});evaluate(f['id'],"""(()=>{const v=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),xs=[...document.querySelectorAll('button')].filter(e=>v(e)&&(e.innerText||e.textContent||'').trim()==='All');if(xs.length!==1)throw Error('All option ambiguous');xs[0].click();return true})()""",True);pause(250);f=main(True);days=evaluate(f['id'],"document.querySelector('input#slctDateSpan').value");journal.succeed_action(h,state_after={'days':days},verification_condition='input#slctDateSpan equals All',verification_result=days=='All')
h=journal.start_action(stage='days_filter',intended_action='Click Refresh',action_type='click',state_before={'days':days},frame_name=f.get('name','Main'),frame_url=f.get('url',''),selector_strategy='unique Refresh link invoking RefreshGrid',target={'tag':'A','id':'','role':'link','label':'Refresh'});refresh=evaluate(f['id'],"""(()=>{const xs=[...document.querySelectorAll('a')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)&&e.getAttribute('href')==='javascript:RefreshGrid()'&&(e.innerText||e.textContent||'').trim()==='Refresh');if(xs.length!==1)throw Error('Refresh ambiguous');xs[0].click();return true})()""",True)
handle=journal.start_action(stage='grid_ready',intended_action='Verify All-Active results grid readiness',action_type='wait/poll',state_before={})
f,state=wait_grid();capture('grid_ready','after-grid-ready');journal.succeed_action(h,state_after=state,verification_condition='Refresh produced ready grid',verification_result=state['grids']==1 and state['rows']>0 and state['days']=='All' and state['loading']==0);journal.succeed_action(handle,state_after=state,verification_condition='exact Active route, Days All, table ready, no loading',verification_result=state['grids']==1 and state['rows']>0 and state['days']=='All' and state['loading']==0,frame_name=f.get('name','Main'),frame_url=f.get('url',''))
db=DiscoveryIndex(INDEX);resume_count=db.begin_run(RUN_ID);detector=EndDetector();total_dupes=total_bad=0;cycle=0;end=False
try:
 ids,new,dupes,bad=persist(db,f,cycle);db.record_cycle(RUN_ID,dupes,bad,scrolled=False);total_dupes+=dupes;total_bad+=bad
 available=candidates(f)
 if not available:raise NavigationBlocked('No internal scrollable grid ancestor found')
 changing=[]
 for candidate in available:
  before_ids=ordered_viewport_ids(f,candidate['depth']);before=metrics(f,candidate['depth']);scroll(f,candidate['depth'],max(32,before['clientHeight']*.8));changed=False
  for _ in range(20):
   pause(200);f=main(True);after_ids=ordered_viewport_ids(f,candidate['depth']);after=metrics(f,candidate['depth'])
   if after_ids!=before_ids and after['scrollTop']>before['scrollTop']+1:changed=True;break
  if changed:changing.append(candidate)
 if len(changing)!=1:raise NavigationBlocked('Unique row-changing internal scroll ancestor missing or ambiguous')
 selected=changing[0];depth=selected['depth']
 # Persist the probe position; every DOM and frame lookup above was reacquired.
 f=main(True);ids,new,dupes,bad=persist(db,f,cycle);db.record_cycle(RUN_ID,dupes,bad);total_dupes+=dupes;total_bad+=bad
 while db.count()<MAX_UNIQUE and cycle<MAX_CYCLES and time.monotonic()<DEADLINE:
  cycle+=1;f=main(True);before=metrics(f,depth);before_ids=ordered_viewport_ids(f,depth)
  scroll_handle=journal.start_action(stage='all_active_discovery',intended_action='Scroll internal virtualized grid',action_type='scroll',state_before={'scrollTop':before['scrollTop'],'clientHeight':before['clientHeight'],'scrollHeight':before['scrollHeight'],'first_identifier':safe_ref(before_ids[0]) if before_ids else '','last_identifier':safe_ref(before_ids[-1]) if before_ids else ''},frame_name=f.get('name','Main'),frame_url=f.get('url',''),selector_strategy='DIV.k-grid-content.k-auto-scrollable',target={'tag':'DIV','id':'','role':'region','label':'All-Active results'})
  movement=scroll(f,depth,max(32,before['clientHeight']*.8));changed=False
  for _ in range(25):
   pause(200);f=main(True);current_ids=ordered_viewport_ids(f,depth)
   if current_ids!=before_ids:changed=True;break
  f=main(True);ids,new,dupes,bad=persist(db,f,cycle);db.record_cycle(RUN_ID,dupes,bad);total_dupes+=dupes;total_bad+=bad;now=metrics(f,depth)
  viewport_ids=ordered_viewport_ids(f,depth);observation=ScrollObservation(viewport_ids,now['scrollTop'],now['clientHeight'],now['scrollHeight'],loading(f['id']),new);detector.observe(observation);journal.succeed_action(scroll_handle,state_after={'scrollTop':now['scrollTop'],'clientHeight':now['clientHeight'],'scrollHeight':now['scrollHeight'],'first_identifier':safe_ref(viewport_ids[0]) if viewport_ids else '','last_identifier':safe_ref(viewport_ids[-1]) if viewport_ids else '','new':new,'duplicates':dupes,'rendered_content_changed':changed},verification_condition='scroll state independently observed after reacquisition',verification_result=True,action_type='scroll',scroll_metrics={'scrollTop_before':before['scrollTop'],'scrollTop_after':now['scrollTop'],'clientHeight':now['clientHeight'],'scrollHeight':now['scrollHeight'],'newly_discovered_count':new,'duplicate_count':dupes,'rendered_content_changed':changed,'true_end_signals':{'near_bottom':observation.near_bottom,'no_new_cycles':detector.consecutive_no_new}})
  print(json.dumps({'scroll_cycle':cycle,'total_unique':db.count(),'new':new,'duplicates_ignored':dupes,'malformed_rows':bad,'scrollTop':round(now['scrollTop'],1),'clientHeight':round(now['clientHeight'],1),'scrollHeight':round(now['scrollHeight'],1),'first_id_hash':hashed(viewport_ids[0]) if viewport_ids else '', 'last_id_hash':hashed(viewport_ids[-1]) if viewport_ids else '', 'consecutive_no_new':detector.consecutive_no_new}))
  if detector.ready_for_final_probe(observation,movement['before']):
   scroll(f,depth,max(8,now['clientHeight']*.1));pause(500);f=main(True);final_ids,final_new,final_dupes,final_bad=persist(db,f,cycle);db.record_cycle(RUN_ID,final_dupes,final_bad);total_dupes+=final_dupes;total_bad+=final_bad;final=metrics(f,depth)
   final_viewport_ids=ordered_viewport_ids(f,depth);final_observation=ScrollObservation(final_viewport_ids,final['scrollTop'],final['clientHeight'],final['scrollHeight'],loading(f['id']),final_new)
   if detector.confirms_end(observation,final_observation):end=True;break
 db.finish_run(RUN_ID,end_reached=end,scale_limit=(db.count()>=MAX_UNIQUE and not end))
 summary=db.run_summary(RUN_ID)
 print(json.dumps({'ok':True,'run_id':RUN_ID,'resume_count':resume_count,'unique_discovered':db.count(),'scroll_cycles':summary['scroll_cycles'],'duplicates_ignored':summary['duplicates_ignored'],'malformed_rows':summary['malformed_rows'],'end_reached':bool(summary['end_reached']),'scale_limit_reached':db.count()>=MAX_UNIQUE,'scroll_container':{'depth':depth,'tag':selected['tag'],'id':selected['id'],'classes':selected['classes'],'overflowY':selected['overflowY']},'index_path':str(INDEX)}))
except Exception:
 journal.safety_stop(stage='discovery',intended_action='Stop discovery after safe failure',state_before={'cycle':cycle},error_category='verification_failed');raise
finally:db.close();journal.close()
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--max-unique", type=int, default=150)
    parser.add_argument("--max-cycles", type=int, default=500)
    parser.add_argument("--browser-timeout", type=int, default=600)
    parser.add_argument("--browser-use-executable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_unique < 1 or args.max_cycles < 1 or args.browser_timeout < 10:
        parser.error("limits must be positive and browser timeout must be at least 10 seconds")
    run_id = f"discovery-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    program = browser_program(run_id, args.index.resolve(), args.max_unique, args.max_cycles, args.browser_timeout - 5)
    compile(program, "<cdk-discovery-driver>", "exec")
    if args.dry_run:
        print("Discovery dry run passed; Browser Use was not started.")
        return 0
    executable = args.browser_use_executable or shutil.which("browser-use")
    if not executable:
        print("Browser Use executable unavailable", file=sys.stderr)
        return 1
    result=run_owned([executable],cwd=ROOT,stdin_text=program,timeout=args.browser_timeout,journal_path=ROOT/"logs"/f"worker-lifecycle-{run_id}.jsonl")
    if not result.shutdown_confirmed:
        print("SAFETY STOP: owned Browser Use process tree exit could not be confirmed",file=sys.stderr);return 125
    if result.returncode:print("Discovery stopped; all owned worker processes exited",file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
