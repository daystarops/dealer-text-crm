from __future__ import annotations
import csv, html, json, re, sqlite3, sys, time
from collections import Counter
from pathlib import Path
ROOT=Path.cwd();sys.path.insert(0,str(ROOT/'scripts'))
from browser_action_journal import BrowserActionJournal,new_run_id,safe_ref
from cdk_runtime import approved_top_level_url,parse_detail_destination,normalize_phone
from manual_enrichment_policy import classify_enrichment

RUN_ID=new_run_id('manual-enrichment')
journal=BrowserActionJournal(ROOT,RUN_ID)
STOP=re.compile(r'\b(?:stop|unsubscribe|do not (?:contact|call|text)|wrong number|disconnected|not in service)\b',re.I)
CLOSED=re.compile(r'\b(?:already (?:bought|purchased)|purchased elsewhere|no longer (?:shopping|interested)|status\s*:?\s*(?:sold|closed)|opportunity\s+(?:sold|closed))\b',re.I)
QUESTION=re.compile(r'\?|\b(?:asked|question|wondering|can you|could you|how much|payment|finance|trade)\b',re.I)
WRITE_LABELS={'Email','Phone','Text Message','Add Note'}

def pause(ms=250): cdp('Runtime.evaluate',expression=f'new Promise(r=>setTimeout(r,{ms}))',awaitPromise=True)
def frames():
 out=[]
 def walk(node):
  out.append(node['frame'])
  for child in node.get('childFrames',[]):walk(child)
 walk(cdp('Page.getFrameTree')['frameTree']);return out
def context(fid):return cdp('Page.createIsolatedWorld',frameId=fid,worldName='manual-enrichment-readonly')['executionContextId']
def evalf(fid,expr):return cdp('Runtime.evaluate',contextId=context(fid),expression=expr,returnByValue=True).get('result',{}).get('value')
def main_frame(opp):
 matches=[]
 for f in frames():
  d=parse_detail_destination(f.get('url',''),'https://crm.connectcdk.com/')
  if d and d.opportunity_id==opp:matches.append(f)
 return matches[0] if len(matches)==1 else None
def target_frame(token):
 matches=[f for f in frames() if f.get('name')=='tabsTargetFrame' and token.lower() in f.get('url','').lower()]
 return matches[0] if len(matches)==1 else None
def wait_frame(token):
 for _ in range(100):
  f=target_frame(token)
  if f and evalf(f['id'],'document.readyState')=='complete':return f
  pause()
 return None
def shot(label):
 import base64
 path=ROOT/'logs'/f'browser-actions-{RUN_ID}-{label}.png'
 try:path.write_bytes(base64.b64decode(cdp('Page.captureScreenshot',format='png')['data']));journal.log_screenshot(stage='manual_enrichment',screenshot_path=path,captured=True)
 except Exception:journal.log_screenshot(stage='manual_enrichment',screenshot_path=None,captured=False,error_category='screenshot_failed')
def log_simple(stage,intent,atype,before,after,condition,result,**meta):
 h=journal.start_action(stage=stage,intended_action=intent,action_type=atype,state_before=before,**meta)
 journal.succeed_action(h,state_after=after,verification_condition=condition,verification_result=result,**meta)

queue=list(csv.DictReader((ROOT/'outputs'/'manual-review-recipients.csv').open(encoding='utf-8-sig')))
if len(queue)!=15 or len({r['opportunity_id'] for r in queue})!=15:raise RuntimeError('manual queue integrity failed')
db=sqlite3.connect(ROOT/'private'/'all-active-discovery.db');db.row_factory=sqlite3.Row
urls={r['opportunity_id']:r['detail_url'] for r in db.execute("select opportunity_id,detail_url from discovered_leads where opportunity_id in (%s)"%(','.join('?'*len(queue))),[r['opportunity_id'] for r in queue])}
if set(urls)!={r['opportunity_id'] for r in queue}:raise RuntimeError('manual queue URL mismatch')
results=[];opened=closed=failed=retries=0
try:
 init=journal.start_action(stage='manual_enrichment',intended_action='Initialize targeted 15-record enrichment',action_type='wait',state_before={'queue_count':15})
 journal.succeed_action(init,state_after={'queue_integrity':True},verification_condition='exactly 15 unique queued opportunities',verification_result=True)
 rec=start_recording(RUN_ID,title='Targeted CDK manual-candidate enrichment')
 log_simple('manual_enrichment','Start privacy-controlled browser recording','wait',{}, {'recording_started':bool(rec)},'recording started',bool(rec))
 canon=[t for t in list_tabs() if approved_top_level_url(t.get('url',''))]
 log_simple('canonical','Locate unique canonical Applications tab','poll',{'tab_count':len(list_tabs())},{'canonical_count':len(canon)},'exactly one canonical Applications tab',len(canon)==1)
 if len(canon)!=1:raise RuntimeError('canonical_ambiguous')
 canonical=canon[0];cid=canonical['targetId'];switch_tab(cid)
 log_simple('canonical','Switch to canonical Applications tab','switch tab',{}, {'canonical_restored':True},'canonical tab active',True,target_tab_id=safe_ref(cid),top_level_url_after=canonical['url'])
 for position,row in enumerate(queue,1):
  opp=row['opportunity_id'];before={t['targetId'] for t in list_tabs()}
  log_simple('candidate_selection','Select next intended manual-review candidate','poll',{'position':position},{'selected':True,'stable_identifier':safe_ref(opp)},'candidate is one of exact 15 queue entries',True,expected_opportunity_id=opp)
  try:
   h=journal.start_action(stage='detail_navigation',intended_action='Open exact queued opportunity detail',action_type='open tab',state_before={'stable_identifier':safe_ref(opp)},target_tab_id=safe_ref(cid),top_level_url_before=canonical['url'])
   new_tab(urls[opp]);opened+=1
   detail=None
   for _ in range(80):
    pause();new=[t for t in list_tabs() if t['targetId'] not in before]
    if len(new)==1:
     d=parse_detail_destination(new[0].get('url',''),'https://crm.connectcdk.com/')
     if d:detail=new[0];break
   verified=bool(detail and parse_detail_destination(detail['url'],'https://crm.connectcdk.com/').opportunity_id==opp)
   journal.succeed_action(h,state_after={'detail_tabs':len(new) if 'new' in locals() else 0,'route_verified':verified},verification_condition='one approved detail tab with expected opportunity ID',verification_result=verified,target_tab_id=safe_ref(detail['targetId']) if detail else '')
   if not verified:failed+=1;shot('failed-'+str(position));raise RuntimeError('opportunity_mismatch')
   switch_tab(detail['targetId']);wait_for_load(timeout=30);shot('detail-'+str(position))
   mf=None
   for _ in range(80):
    mf=main_frame(opp)
    if mf:break
    pause()
   if not mf:raise RuntimeError('required_content_missing')
   info=evalf(mf['id'],r"""(()=>{const visible=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),norm=s=>(s||'').trim().replace(/\s+/g,' '),isLabel=s=>/^(phone|mobile|cell|home|work)(\s*#)?\s*:?$/i.test(norm(s)),phone=s=>{const m=norm(s).match(/(?:\+?1[ .()-]*)?(?:\(?\d{3}\)?[ .-]*)\d{3}[ .-]*\d{4}/);return m?m[0]:''},txt=document.body.innerText||'',values=[];for(const e of document.querySelectorAll('label,th,td,dt,span,div')){if(!isLabel(e.innerText))continue;for(const c of [e.nextElementSibling,e.parentElement&&e.parentElement.querySelector('input,a[href^="tel:"],td,dd,span')].filter(Boolean)){const p=phone((c.matches('input')?c.value:(c.getAttribute('href')||c.innerText||'')).replace(/^tel:/i,''));if(p)values.push(p)}}const unique=[...new Set(values)],status=([...document.querySelectorAll('tr,td,span')].map(e=>(e.innerText||'').trim()).find(x=>/^Status:/i.test(x))||'').replace(/^Status:/i,'').trim(),write=[...document.querySelectorAll('a,button')].filter(visible).map(e=>(e.innerText||e.value||'').trim()).filter(x=>['Email','Phone','Text Message','Add Note'].includes(x));return {body:txt,phone:unique.length===1?unique[0]:'',phone_candidate_count:unique.length,status,write_labels:write}})()""")
   expected_name=sorted(re.findall(r'[a-z0-9]+',row['full_name'].casefold()));body_name=sorted(re.findall(r'[a-z0-9]+',info['body'].casefold()));name_ok=all(body_name.count(token)>=expected_name.count(token) for token in set(expected_name));phone_ok=bool(normalize_phone(info['phone'])) and normalize_phone(info['phone'])==normalize_phone(row['phone'])
   log_simple('field_observation','Verify current identity, phone, and read-only fields','poll',{'stable_identifier':safe_ref(opp)},{'name_match':name_ok,'phone_match':phone_ok,'write_controls_avoided':all(x in WRITE_LABELS for x in info['write_labels'])},'recorded identity matched; phone observation is optional and confidence-scoped',name_ok,expected_opportunity_id=opp)
   hf=wait_frame('history.aspx')
   log_simple('history','Observe existing History iframe without mutation','poll',{}, {'history_iframe':bool(hf)},'tabsTargetFrame history.aspx loaded',bool(hf),frame_name='tabsTargetFrame',frame_url=hf.get('url','') if hf else '')
   if not hf:raise RuntimeError('required_content_missing')
   history_meta=evalf(hf['id'],"(()=>({scheduled:document.querySelectorAll('#gvScheduled tbody tr').length,history:document.querySelectorAll('#gvOpptyHistory table tbody tr').length}))()")
   log_simple('history','Observe indexed History context','poll',{}, {'scheduled_count':history_meta['scheduled'],'history_count':history_meta['history']},'History structures observed',True)
   ah=journal.start_action(stage='audit',intended_action='Open read-only Audit Trail tab',action_type='click',state_before={'selected':False},selector_strategy='unique visible #liAuditTrail',target={'tag':'LI','id':'liAuditTrail','role':'tab','label':'Audit Trail'})
   clicked=evalf(mf['id'],"(()=>{const x=[...document.querySelectorAll('#liAuditTrail')].filter(e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length));if(x.length!==1)return false;(x[0].querySelector('a')||x[0]).click();return true})()")
   af=wait_frame('CustomerAuditTrail.aspx') if clicked else None
   journal.succeed_action(ah,state_after={'audit_iframe':bool(af)},verification_condition='Audit Trail selected and iframe loaded',verification_result=bool(af),frame_name='tabsTargetFrame',frame_url=af.get('url','') if af else '')
   if not af:raise RuntimeError('required_content_missing')
   audit_count=evalf(af['id'],"document.querySelectorAll('#GridViewAuditTrail tbody tr').length")
   log_simple('audit','Observe Audit Trail context','poll',{}, {'audit_rows_rendered':audit_count},'Audit Trail grid observed',audit_count is not None)
   acts=db.execute('select * from detail_activities where stable_lead_key=(select stable_lead_key from discovered_leads where opportunity_id=?)',(opp,)).fetchall()
   base=classify_enrichment(indexed_phone=row['phone'],live_phone=info['phone'],identity_pairing_valid=True,resolver_failed=info.get('phone_candidate_count',0)!=1,status=info['status'],activity_text='\n'.join(a['sanitized_body'] or '' for a in acts),inbound_present=any(a['direction']=='inbound' for a in acts),unanswered_attempts=int(row.get('consecutive_unanswered_attempts') or 0),record_context_valid=bool(info['body'].strip()),vehicle=row['vehicle'])
   category,reason,message,confidence,operational_phone=base
   inbound=[a for a in acts if a['direction']=='inbound'];outbound=[a for a in acts if a['direction']=='outbound'];last_customer=max((a['activity_timestamp'] for a in inbound if a['activity_timestamp']),default='unknown');last_dealer=max((a['activity_timestamp'] for a in outbound if a['activity_timestamp']),default='unknown');unanswered=int(row.get('consecutive_unanswered_attempts') or 0)
   results.append({**row,'current_status':info['status'] or 'unknown','last_customer_activity':last_customer,'last_dealership_activity':last_dealer,'recent_unanswered_attempts':unanswered,'customer_position':'unknown','main_obstacle':reason,'phone_confidence':confidence,'classification':category,'classification_reason':reason,'recommended_channel':'text' if category=='MANUAL_TEXT_PILOT_APPROVED' else ('call' if category=='PERSONAL_CALL_RECOMMENDED' else 'personalized response' if category=='PERSONALIZED_RESPONSE_REQUIRED' else 'none'),'proposed_message':message,'verify_before_sending':'Jordan must recheck current status, cadence, phone suitability, and message accuracy immediately before any send.'})
   log_simple('classification','Record local read-only enrichment classification','verification',{'stable_identifier':safe_ref(opp)},{'classification':category},'exactly one allowed enrichment category assigned',category in {'MANUAL_TEXT_PILOT_APPROVED','PERSONAL_CALL_RECOMMENDED','PERSONALIZED_RESPONSE_REQUIRED','SUPPRESS','INSUFFICIENT_EVIDENCE'},expected_opportunity_id=opp)
  except Exception:
   failed+=1
   if not any(r['opportunity_id']==opp for r in results):results.append({**row,'current_status':'unknown','last_customer_activity':'unknown','last_dealership_activity':'unknown','recent_unanswered_attempts':'unknown','customer_position':'unknown','main_obstacle':'Live verification failed','phone_confidence':'PHONE_UNAVAILABLE','classification':'INSUFFICIENT_EVIDENCE','classification_reason':'Live verification failed safely','recommended_channel':'none','proposed_message':'','verify_before_sending':'Do not contact until the record is manually reverified.'})
  finally:
   for t in list_tabs():
    if t['targetId'] not in before and t['targetId']!=cid:close_tab(t['targetId']);closed+=1
   switch_tab(cid);remaining=[t for t in list_tabs() if approved_top_level_url(t.get('url',''))]
   log_simple('cleanup','Close detail tab and restore canonical Applications tab','close tab',{}, {'canonical_count':len(remaining)},'only canonical Applications tab remains',len(remaining)==1 and remaining[0]['targetId']==cid,target_tab_id=safe_ref(cid))

 out=ROOT/'outputs';fields=['full_name','phone','phone_confidence','opportunity_id','vehicle','current_status','last_customer_activity','last_dealership_activity','recent_unanswered_attempts','customer_position','main_obstacle','classification','classification_reason','recommended_channel','proposed_message','verify_before_sending']
 def write_csv(name,category):
  with (out/name).open('w',newline='',encoding='utf-8-sig') as f:
   w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows({k:r.get(k,'unknown') for k in fields} for r in results if r['classification']==category)
 for name,cat in [('enriched-manual-text-approved.csv','MANUAL_TEXT_PILOT_APPROVED'),('enriched-personal-call.csv','PERSONAL_CALL_RECOMMENDED'),('enriched-personalized-response.csv','PERSONALIZED_RESPONSE_REQUIRED'),('enriched-suppressed.csv','SUPPRESS'),('enriched-insufficient-evidence.csv','INSUFFICIENT_EVIDENCE')]:write_csv(name,cat)
 counts=Counter(r['classification'] for r in results);approved=[r for r in results if r['classification']=='MANUAL_TEXT_PILOT_APPROVED'][:10]
 summary='# Enriched manual pilot\n\n'+f'- Candidates inspected: {len(results)}\n'+''.join(f'- {k}: {counts[k]}\n' for k in ['MANUAL_TEXT_PILOT_APPROVED','PERSONAL_CALL_RECOMMENDED','PERSONALIZED_RESPONSE_REQUIRED','SUPPRESS','INSUFFICIENT_EVIDENCE'])+f'- Recommended pilot size: {len(approved)}\n\nNo message was sent. Every proposed recipient requires Jordan’s individual review. Salesperson ownership and absent generic contact-preference data were neutral.\n'
 (out/'enriched-manual-pilot-summary.md').write_text(summary,encoding='utf-8')
 rows=''.join('<tr>'+''.join(f'<td>{html.escape(str(r.get(k,"")))}</td>' for k in fields)+'</tr>' for r in results)
 (out/'enriched-manual-pilot-operational.html').write_text('<!doctype html><meta charset="utf-8"><h1>Enriched manual pilot</h1><p>Local review only; sending is disabled.</p><table border="1"><tr>'+''.join(f'<th>{k}</th>' for k in fields)+'</tr>'+rows+'</table>',encoding='utf-8')
 print(json.dumps({'run_id':RUN_ID,'inspected':len(results),'counts':counts,'pilot_size':len(approved),'opened':opened,'closed':closed,'failed_verifications':failed,'retries':retries,'journal_jsonl':str(journal.jsonl_path),'journal_md':str(journal.markdown_path)}))
finally:
 try:stop_recording()
 except Exception:pass
 db.close();journal.close()
