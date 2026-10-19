import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
from cdk_review_driver import BROWSER_PROGRAM
from cdk_runtime import ElementIdentity,NavigationBlocked,authorize_action
from identity_vault import IdentityRecord,IdentityVault
from messaging import MessageQueue,QueueRequest
from privacy_gate import PrivacyGateError,assert_no_residual_pii
from browser_recovery import recovery_program

class Protector:
 def protect(self,s):return s.encode()
 def unprotect(self,b):return b.decode()

class ThirdAuditTests(unittest.TestCase):
 def test_lead_authorization_requires_verified_base(self):
  element=ElementIdentity("Lead","link","A",onclick="NewWindow('../newprospects/OpptyDetails.aspx?lDID=12&lPID=9')")
  base="https://crm.connectcdk.com/evo2/x/Main.aspx"
  self.assertEqual(authorize_action("lead_detail",element,base_url=base).opportunity_id,"12")
  with self.assertRaises(NavigationBlocked):authorize_action("lead_detail",element)
  self.assertIn('authorize_action("lead_detail",identity,base_url=main["url"]',BROWSER_PROGRAM)
 @unittest.skipUnless(sys.platform=="win32","DPAPI rendering requires Windows")
 def test_render_cli_subprocess(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);analysis=root/"analysis.json";analysis.write_text(json.dumps({"run_id":"cdk-20260805T120000-abcdef12","ranked_leads":[]}))
   command=[sys.executable,str(ROOT/"scripts"/"analysis_pipeline.py"),"--render",str(analysis),"--output-dir",str(root/"out")]
   completed=subprocess.run(command,cwd=ROOT,capture_output=True,text=True)
   self.assertEqual(completed.returncode,0,completed.stderr);self.assertTrue((root/"out"/"latest-audit-report.md").exists())
 def test_extraction_upsert_preserves_operational_state(self):
  with tempfile.TemporaryDirectory() as td:
   vault=IdentityVault(Path(td)/"v.db",Protector());old=IdentityRecord("1","Alex","Smith","Alex Smith","+12125550101","Old","thread",sms_eligibility_status="ELIGIBLE",eligibility_basis="documented consent",eligibility_source="CRM",eligibility_timestamp="t",last_message_at="last",provider_message_id="provider")
   key=vault.upsert(old);vault.suppress(key,"stop-time");vault.extraction_upsert(IdentityRecord("1","Alexa","Jones","Alexa Jones","+12125550102","New","thread2"));record=vault.get(key)
   self.assertTrue(record.do_not_contact);self.assertEqual(record.sms_eligibility_status,"OPTED_OUT");self.assertEqual(record.opt_out_timestamp,"stop-time");self.assertEqual(record.eligibility_basis,"documented consent");self.assertEqual(record.last_message_at,"last");self.assertEqual(record.provider_message_id,"provider");self.assertEqual((record.full_name,record.phone_e164,record.vehicle),("Alexa Jones","+12125550102","New"));vault.close()
 def test_queue_idempotency_and_changed_message(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);vault=IdentityVault(root/"v.db",Protector());record=IdentityRecord("1","A","B","A B","+12125550123",sms_eligibility_status="ELIGIBLE",eligibility_basis="consent");key=vault.upsert(record);queue=MessageQueue(root/"q.db",Protector())
   request=QueueRequest(key,record.phone_e164,"sender","Hello there","SMS","consent","APPROVAL_QUEUE",workflow_campaign="followup")
   self.assertTrue(queue.enqueue(request,vault).created_new);self.assertTrue(queue.enqueue(request,vault).deduplicated);self.assertEqual(queue.connection.execute("select count(*) from messages").fetchone()[0],1)
   changed=QueueRequest(key,record.phone_e164,"sender","Hello again","SMS","consent","APPROVAL_QUEUE",workflow_campaign="followup");self.assertTrue(queue.enqueue(request,vault).deduplicated);self.assertTrue(queue.enqueue(changed,vault).created_new);self.assertEqual(queue.connection.execute("select count(*) from messages").fetchone()[0],2);queue.close();vault.close()
 def test_sent_message_obeys_recontact_period(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);vault=IdentityVault(root/"v.db",Protector());record=IdentityRecord("1","A","B","A B","+12125550123",sms_eligibility_status="ELIGIBLE",eligibility_basis="consent");key=vault.upsert(record);queue=MessageQueue(root/"q.db",Protector());request=QueueRequest(key,record.phone_e164,"sender","Hello","SMS","consent","APPROVAL_QUEUE",deduplication_period_days=30);first=queue.enqueue(request,vault).message_id;queue.connection.execute("update messages set status='SENT' where message_id=?",(first,));queue.connection.commit();self.assertEqual(queue.enqueue(request,vault).message_id,first)
   immediate=QueueRequest(key,record.phone_e164,"sender","Hello","SMS","consent","APPROVAL_QUEUE",deduplication_period_days=0);self.assertNotEqual(queue.enqueue(immediate,vault).message_id,first);queue.close();vault.close()
 def test_full_output_pii_gate(self):
  for value in ("x@y.example","PO Box 12","VIN 1HGCM82633A004352","SSN 123-45-6789","bank account 123456789"):
   with self.assertRaises(PrivacyGateError):assert_no_residual_pii(value,{})
 def test_frame_context_and_recovery_are_explicit(self):
  for helper in ("call_in_element_context","crm_frame_action","main_frame_action","detail_frame_query"):self.assertIn("def "+helper,BROWSER_PROGRAM)
  self.assertNotIn('expression="goTo(',BROWSER_PROGRAM);self.assertNotIn('expression="RefreshGrid()',BROWSER_PROGRAM)
  recovery=recovery_program("run");self.assertLess(recovery.index("list_tabs()"),recovery.index("switch_tab(results)"));self.assertGreater(recovery.index("page_info()"),recovery.index("switch_tab(results)"));self.assertIn("approved_top_level_url(result_target",recovery)
 def test_trade_and_preview_controls_are_declared(self):
  schema=json.loads((ROOT/"schemas"/"extracted-batch.schema.json").read_text());self.assertIn("trade",schema["properties"]["leads"]["items"]["properties"])
  renderer=(ROOT/"scripts"/"report_renderer.py").read_text();self.assertIn("disabled aria-disabled='true'",renderer);self.assertIn("Preview only",renderer)

if __name__=="__main__":unittest.main()
