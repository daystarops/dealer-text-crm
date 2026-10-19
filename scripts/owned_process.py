"""Bounded lifecycle management for one subprocess and its owned descendants."""
import json,os,signal,subprocess
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
@dataclass(frozen=True)
class OwnedResult:returncode:int;shutdown_confirmed:bool;forced:bool;root_pid:int
class LifecycleJournal:
 def __init__(self,path):self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.stream=self.path.open('x',encoding='utf-8',newline='\n')
 def record(self,event,**fields):self.stream.write(json.dumps({'timestamp_utc':datetime.now(timezone.utc).isoformat(),'event':event,**fields},sort_keys=True)+'\n');self.stream.flush();os.fsync(self.stream.fileno())
 def close(self):
  if not self.stream.closed:self.stream.flush();os.fsync(self.stream.fileno());self.stream.close()
class OwnedProcess:
 def __init__(self,command,*,cwd,stdin_text,journal,graceful_timeout=5,force_timeout=5):
  flags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0;self.process=subprocess.Popen(command,cwd=cwd,stdin=subprocess.PIPE,text=True,creationflags=flags,start_new_session=os.name!='nt');self.owned_root_pid=self.process.pid;self.journal=journal;self.graceful_timeout=graceful_timeout;self.force_timeout=force_timeout;self.started=False;self.forced=False
  try:self.process.stdin.write(stdin_text);self.process.stdin.close()
  except BaseException:self.shutdown('stdin_exception');raise
 def wait(self,timeout):
  try:self.process.wait(timeout=timeout);return True
  except subprocess.TimeoutExpired:return False
 def graceful(self):
  if self.process.poll() is not None:return
  if os.name=='nt':
   try:self.process.send_signal(signal.CTRL_BREAK_EVENT)
   except (OSError,ValueError):self.process.terminate()
  else:os.killpg(self.owned_root_pid,signal.SIGTERM)
 def force(self):
  self.forced=True
  if self.process.poll() is not None:return
  if os.name=='nt':subprocess.run(['taskkill','/PID',str(self.owned_root_pid),'/T','/F'],capture_output=True,check=False,timeout=self.force_timeout)
  else:
   try:os.killpg(self.owned_root_pid,signal.SIGKILL)
   except ProcessLookupError:pass
 def shutdown(self,reason):
  journal_error=None
  try:
   if not self.started:self.started=True;self.journal.record('shutdown_started',reason=reason,owned_root_pid=self.owned_root_pid)
  except BaseException as error:journal_error=error
  self.graceful();exited=self.wait(self.graceful_timeout)
  if not exited:self.force();exited=self.wait(self.force_timeout)
  code=self.process.returncode if self.process.returncode is not None else 125
  try:
   if journal_error is None:self.journal.record('shutdown_complete' if exited else 'shutdown_unconfirmed',owned_root_pid=self.owned_root_pid,forced=self.forced,returncode=code)
  finally:self.journal.close()
  if journal_error is not None:raise journal_error
  return OwnedResult(code,exited,self.forced,self.owned_root_pid)
 def run(self,timeout):
  reason='worker_exit'
  try:self.process.wait(timeout=timeout)
  except subprocess.TimeoutExpired:reason='timeout'
  except KeyboardInterrupt:reason='keyboard_interrupt'
  except BaseException:reason='orchestrator_exception';raise
  finally:result=self.shutdown(reason)
  return result
def run_owned(command,*,cwd,stdin_text,timeout,journal_path,graceful_timeout=5,force_timeout=5):
 journal=LifecycleJournal(journal_path)
 try:owned=OwnedProcess(command,cwd=cwd,stdin_text=stdin_text,journal=journal,graceful_timeout=graceful_timeout,force_timeout=force_timeout)
 except BaseException:journal.close();raise
 return owned.run(timeout)
