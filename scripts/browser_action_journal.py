"""Crash-safe, privacy-safe browser action journals."""
from __future__ import annotations
import hashlib,json,os,re,uuid
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit
ERRORS={'browser_timeout','canonical_ambiguous','frame_missing','grid_not_ready','history_timeout','journal_unavailable','opportunity_mismatch','required_content_missing','route_rejected','screenshot_failed','selector_ambiguous','unexpected_tab','verification_failed'}
SKEY=re.compile(r'(customer.?name|recorded.?name|phone|email|message|note|body|cookie|token|secret|password|api.?key|identity)',re.I)
PHONE=re.compile(r'(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)');EMAIL=re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b',re.I);SECRET=re.compile(r'(?i)\b(?:bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)');DETAIL={'ldid','lpid','opportunityid','personid','customerid'}
def read_valid_jsonl(path):
 rows=[]
 with Path(path).open(encoding='utf-8') as stream:
  for line in stream:
   try:rows.append(json.loads(line))
   except json.JSONDecodeError:break
 return rows
class JournalUnavailable(RuntimeError):pass
def new_run_id(prefix):
 safe=re.sub(r'[^a-z0-9-]','-',str(prefix).casefold()).strip('-') or 'browser';return f"{safe}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:10]}"
def safe_ref(value,length=12):return hashlib.sha256(str(value).encode()).hexdigest()[:length] if value else ''
def sanitize_text(value):
 text=str(value or '')[:2000];return SECRET.sub('[secret]',PHONE.sub('[phone]',EMAIL.sub('[email]',text)))
def sanitize_url(value):
 try:
  p=urlsplit(str(value or ''))
  if not p.scheme or not p.netloc:return sanitize_text(value)
  q=[(k,'[redacted]' if k.casefold() in DETAIL else sanitize_text(v)) for k,v in parse_qsl(p.query,keep_blank_values=True)]
  return urlunsplit((p.scheme,p.netloc,p.path,urlencode(q),''))
 except Exception:return '[invalid-url]'
def sanitize(value,key=''):
 if SKEY.search(key):return '[redacted]'
 if isinstance(value,dict):return {sanitize_text(k):sanitize(v,str(k)) for k,v in value.items()}
 if isinstance(value,(list,tuple)):return [sanitize(v) for v in value]
 if isinstance(value,(bool,int,float)) or value is None:return value
 return sanitize_url(value) if 'url' in key.casefold() or str(value).startswith(('http://','https://')) else sanitize_text(value)
@dataclass(frozen=True)
class ActionHandle:action_id:str;stage:str;intended_action:str;retry_count:int
class BrowserActionJournal:
 def __init__(self,root,run_id):
  self.run_id=sanitize_text(run_id);d=Path(root)/'logs';d.mkdir(parents=True,exist_ok=True);self.jsonl_path=d/f'browser-actions-{self.run_id}.jsonl';self.markdown_path=d/f'browser-actions-{self.run_id}.md'
  if self.jsonl_path.exists() or self.markdown_path.exists():raise JournalUnavailable('journal_unavailable')
  self.sequence=0;self.closed=False
  try:self.j=self.jsonl_path.open('x',encoding='utf-8',newline='\n');self.m=self.markdown_path.open('x',encoding='utf-8',newline='\n');self._write(self.m,f'# Browser action journal: {self.run_id}\n\n')
  except Exception as e:self._close();raise JournalUnavailable('journal_unavailable') from e
 @staticmethod
 def _write(stream,text):stream.write(text);stream.flush();os.fsync(stream.fileno())
 def _close(self):
  for name in ('j','m'):
   try:getattr(self,name).close()
   except Exception:pass
 def record(self,row):
  if self.closed:raise JournalUnavailable('journal_unavailable')
  self.sequence+=1;row={'sequence':self.sequence,'timestamp_utc':datetime.now(timezone.utc).isoformat(),'run_id':self.run_id,**sanitize(row)}
  try:
   self._write(self.j,json.dumps(row,sort_keys=True,ensure_ascii=True)+'\n');t=row.get('target',{});md=f"## ACTION {self.sequence}\n\nStage: {row.get('workflow_stage','')}\n\nACTION: {row.get('intended_action','')}\n\nBEFORE: `{json.dumps(row.get('state_before',{}),sort_keys=True)}`\n\nCLICK TARGET: `{t.get('tag','')}#{t.get('id','')}` {t.get('role','')} \"{t.get('label','')}\" via {row.get('selector_strategy','')}\n\nAFTER: `{json.dumps(row.get('state_after',{}),sort_keys=True)}`\n\nVERIFICATION: {row.get('verification_condition','')} | {row.get('verification_result',False)}\n\nRESULT: {row.get('outcome','')}\n\n";self._write(self.m,md)
  except Exception as e:self.closed=True;self._close();raise JournalUnavailable('journal_unavailable') from e
  return row
 def start_action(self,*,stage,intended_action,action_type,state_before,retry_count=0,**meta):
  h=ActionHandle(uuid.uuid4().hex,stage,intended_action,retry_count);self.record({'action_id':h.action_id,'workflow_stage':stage,'intended_action':intended_action,'action_type':action_type,'state_before':state_before,'state_after':{},'verification_condition':meta.pop('verification_condition',''),'verification_result':False,'retry_count':retry_count,'outcome':'started',**meta});return h
 def finish(self,h,*,state_after,condition,result,outcome='succeeded',error_category='',**meta):
  if not result and outcome=='succeeded':outcome='failed_retryable';error_category='verification_failed'
  category=error_category if error_category in ERRORS else ('verification_failed' if error_category else '')
  return self.record({'action_id':h.action_id,'workflow_stage':h.stage,'intended_action':h.intended_action,'action_type':meta.pop('action_type','verification'),'state_before':{},'state_after':state_after,'verification_condition':condition,'verification_result':result,'retry_count':h.retry_count,'outcome':outcome,'error_category':category,**meta})
 def succeed_action(self,h,*,state_after,verification_condition,verification_result,**meta):return self.finish(h,state_after=state_after,condition=verification_condition,result=verification_result,**meta)
 def fail_action(self,h,*,state_after,verification_condition,error_category,retryable,**meta):return self.finish(h,state_after=state_after,condition=verification_condition,result=False,outcome='failed_retryable' if retryable else 'failed_terminal',error_category=error_category,**meta)
 def safety_stop(self,*,stage,intended_action,state_before,error_category,**meta):
  h=self.start_action(stage=stage,intended_action=intended_action,action_type='safety_stop',state_before=state_before,**meta);return self.finish(h,state_after=state_before,condition='workflow stopped safely',result=False,outcome='safety_stop',error_category=error_category)
 def log_scroll(self,*,stage,intended_action,state_before,state_after,verification_result,scroll_metrics,**meta):
  h=self.start_action(stage=stage,intended_action=intended_action,action_type='scroll',state_before=state_before,**meta);return self.finish(h,state_after=state_after,condition='scroll independently observed',result=verification_result,action_type='scroll',scroll_metrics=scroll_metrics,**meta)
 def log_screenshot(self,*,stage,screenshot_path,captured,error_category=''):
  h=self.start_action(stage=stage,intended_action='Capture browser screenshot',action_type='screenshot',state_before={})
  return self.finish(h,state_after={'captured':captured},condition='screenshot persisted',result=captured,outcome='succeeded' if captured else 'failed_retryable',error_category=error_category or ('screenshot_failed' if not captured else ''),screenshot_path=str(screenshot_path or ''))
 def close(self):
  if not self.closed:self.closed=True;self._close()
 def __enter__(self):return self
 def __exit__(self,*_):self.close()
def initialize_before_browser(root,run_id,browser_factory):
 journal=BrowserActionJournal(root,run_id)
 try:return journal,browser_factory()
 except Exception:journal.close();raise
