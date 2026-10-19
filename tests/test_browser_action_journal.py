import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from browser_action_journal import BrowserActionJournal,JournalUnavailable,new_run_id,read_valid_jsonl,sanitize,sanitize_url
from cdk_discovery_driver import browser_program as discovery_program
from cdk_exhaustive_detail_driver import browser_program as detail_program
class JournalTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_unique_ids_and_no_overwrite(self):
  self.assertNotEqual(new_run_id('test'),new_run_id('test'));j=BrowserActionJournal(self.root,'run');j.close()
  with self.assertRaises(JournalUnavailable):BrowserActionJournal(self.root,'run')
 def test_creation_append_monotonic_and_lifecycle(self):
  with BrowserActionJournal(self.root,'life') as j:
   h=j.start_action(stage='fictional',intended_action='Click fictional button',action_type='click',state_before={'open':False});j.succeed_action(h,state_after={'open':True},verification_condition='opened',verification_result=True)
   h=j.start_action(stage='fictional',intended_action='Verify fictional panel',action_type='poll',state_before={});j.fail_action(h,state_after={'open':False},verification_condition='visible',error_category='raw customer details must not survive',retryable=True)
  rows=read_valid_jsonl(j.jsonl_path);self.assertEqual([r['sequence'] for r in rows],[1,2,3,4]);self.assertEqual(rows[0]['outcome'],'started');self.assertEqual(rows[1]['outcome'],'succeeded');self.assertEqual(rows[3]['outcome'],'failed_retryable');self.assertEqual(rows[3]['error_category'],'verification_failed');self.assertIn('ACTION 4',j.markdown_path.read_text())
 def test_flush_and_fsync_each_write(self):
  with patch('browser_action_journal.os.fsync') as fsync:
   with BrowserActionJournal(self.root,'sync') as j:
    h=j.start_action(stage='x',intended_action='x',action_type='poll',state_before={});j.succeed_action(h,state_after={},verification_condition='x',verification_result=True)
   self.assertGreaterEqual(fsync.call_count,5)
 def test_partial_final_line_is_tolerated(self):
  with BrowserActionJournal(self.root,'partial') as j:
   h=j.start_action(stage='x',intended_action='x',action_type='poll',state_before={});j.succeed_action(h,state_after={},verification_condition='x',verification_result=True)
  with j.jsonl_path.open('a',encoding='utf-8') as f:f.write('{"partial":')
  self.assertEqual(len(read_valid_jsonl(j.jsonl_path)),2)
 def test_strict_sanitizer_and_urls(self):
  value=sanitize({'customer_name':'Fictional Alice','phone':'212-555-0188','email':'alice@example.test','notes':'private note','api_key':'abc','safe':'Bearer abc','detail_url':'https://crm.connectcdk.com/x?lDID=123&lPID=456'})
  text=json.dumps(value);self.assertNotIn('Fictional Alice',text);self.assertNotIn('212-555',text);self.assertNotIn('alice@',text);self.assertNotIn('private note',text);self.assertNotIn('Bearer abc',text);self.assertNotIn('123',sanitize_url(value['detail_url']))
 def test_scroll_and_screenshot_metadata(self):
  with BrowserActionJournal(self.root,'metadata') as j:
   j.log_scroll(stage='grid',intended_action='scroll',state_before={'scrollTop':0},state_after={'scrollTop':80},verification_result=True,scroll_metrics={'scrollTop_before':0,'scrollTop_after':80,'clientHeight':100,'scrollHeight':500,'first_identifier':'abc','last_identifier':'def','newly_discovered_count':2,'duplicate_count':3,'rendered_content_changed':True,'true_end_signals':{}})
   j.log_screenshot(stage='nav',screenshot_path=self.root/'fictional.png',captured=True)
  rows=read_valid_jsonl(j.jsonl_path);self.assertIn('scroll_metrics',rows[1]);self.assertTrue(rows[3]['state_after']['captured'])
 def test_write_failure_stops_journal(self):
  j=BrowserActionJournal(self.root,'fail');h=j.start_action(stage='x',intended_action='x',action_type='poll',state_before={});j.j.close()
  with self.assertRaises(JournalUnavailable):j.succeed_action(h,state_after={},verification_condition='x',verification_result=True)
  with self.assertRaises(JournalUnavailable):j.start_action(stage='x',intended_action='later',action_type='click',state_before={})
 def test_driver_integration_and_initialization_order(self):
  for program in (discovery_program('d',Path('private/x.db'),10,10,30),detail_program('e',Path('private/x.db'),1,10,30)):
   compile(program,'<driver>','exec');self.assertIn('BrowserActionJournal(ROOT,RUN_ID)',program);self.assertLess(program.index('BrowserActionJournal(ROOT,RUN_ID)'),program.index('list_tabs()'));self.assertIn('start_action(',program);self.assertIn('succeed_action(',program)
 def test_no_browser_factory_when_journal_init_fails(self):
  from browser_action_journal import initialize_before_browser
  BrowserActionJournal(self.root,'same').close();called=[]
  with self.assertRaises(JournalUnavailable):initialize_before_browser(self.root,'same',lambda:called.append(True))
  self.assertEqual(called,[])
if __name__=='__main__':unittest.main()
