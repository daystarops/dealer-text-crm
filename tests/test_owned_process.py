import json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from owned_process import LifecycleJournal,OwnedProcess,run_owned

class OwnedProcessTests(unittest.TestCase):
 def journal(self,root):return LifecycleJournal(Path(root)/'life.jsonl')
 def rows(self,root):return [json.loads(x) for x in (Path(root)/'life.jsonl').read_text().splitlines()]
 def test_normal_exit_is_confirmed_before_report(self):
  with tempfile.TemporaryDirectory() as d:
   r=run_owned([sys.executable,'-c','pass'],cwd=ROOT,stdin_text='',timeout=5,journal_path=Path(d)/'life.jsonl')
   self.assertTrue(r.shutdown_confirmed);self.assertEqual([x['event'] for x in self.rows(d)],['shutdown_started','shutdown_complete'])
 def test_forced_termination_after_timeout(self):
  with tempfile.TemporaryDirectory() as d:
   code='import signal,time;signal.signal(signal.SIGTERM,lambda *a:None);time.sleep(30)' if os.name!='nt' else 'import time;time.sleep(30)'
   r=run_owned([sys.executable,'-c',code],cwd=ROOT,stdin_text='',timeout=.1,journal_path=Path(d)/'life.jsonl',graceful_timeout=.1,force_timeout=3)
   self.assertTrue(r.shutdown_confirmed)
 def test_worker_exception_still_shuts_down(self):
  with tempfile.TemporaryDirectory() as d:
   r=run_owned([sys.executable,'-c','raise RuntimeError()'],cwd=ROOT,stdin_text='',timeout=5,journal_path=Path(d)/'life.jsonl');self.assertTrue(r.shutdown_confirmed)
 def test_ctrl_c_uses_common_shutdown(self):
  with tempfile.TemporaryDirectory() as d:
   j=self.journal(d);o=OwnedProcess([sys.executable,'-c','import time;time.sleep(30)'],cwd=ROOT,stdin_text='',journal=j,graceful_timeout=.1)
   real_wait=o.process.wait;calls=[0]
   def interrupt_once(*args,**kwargs):
    calls[0]+=1
    if calls[0]==1:raise KeyboardInterrupt()
    return real_wait(*args,**kwargs)
   with patch.object(o.process,'wait',side_effect=interrupt_once):r=o.run(5)
   self.assertTrue(r.shutdown_confirmed);self.assertEqual(self.rows(d)[0]['reason'],'keyboard_interrupt')
 def test_journal_exception_does_not_claim_success(self):
  with tempfile.TemporaryDirectory() as d:
   j=self.journal(d);o=OwnedProcess([sys.executable,'-c','pass'],cwd=ROOT,stdin_text='',journal=j)
   with patch.object(j,'record',side_effect=OSError('disk')):
    with self.assertRaises(OSError):o.run(5)
 def test_unrelated_process_never_targeted(self):
  with tempfile.TemporaryDirectory() as d:
   j=self.journal(d);o=OwnedProcess([sys.executable,'-c','pass'],cwd=ROOT,stdin_text='',journal=j);pid=o.owned_root_pid;o.run(5)
   self.assertEqual(self.rows(d)[-1]['owned_root_pid'],pid)
 def test_no_action_after_shutdown_complete(self):
  with tempfile.TemporaryDirectory() as d:
   run_owned([sys.executable,'-c','pass'],cwd=ROOT,stdin_text='',timeout=5,journal_path=Path(d)/'life.jsonl');self.assertEqual(self.rows(d)[-1]['event'],'shutdown_complete')

if __name__=='__main__':unittest.main()
