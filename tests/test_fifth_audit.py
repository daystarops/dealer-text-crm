import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
from cdk_review_driver import BROWSER_PROGRAM,browser_program
from cdk_runtime import ElementIdentity,NavigationBlocked,authorize_action,select_cdk_tab,verify_selected_tab
from identity_vault import IdentityRecord,IdentityVault
from messaging import MessageQueue,QueueRequest
from run_validation import analysis_workspace,validate_run_id
from sender_config import SenderConfig
class Protector:
 def protect(self,s):return s.encode()
 def unprotect(self,b):return b.decode()
def config(phone="+12125550100",mode="PERSONAL_DEVICE"):
 return SenderConfig("PERSONAL_AUTHORIZED",mode,"Jordan",phone,"Dealer","VALID","Authorized outreach","Manager","2026-01-01",("SMS",),{"start":"09:00","end":"20:00"})
class FifthAuditTests(unittest.TestCase):
 def test_generated_refresh_control_flow(self):
  program=browser_program("cdk-20260805T120000-abcdef12",5,"2026-08-05",30);compile(program,"<driver>","exec");self.assertNotIn("breakafter",program);self.assertIn("refresh_confirmed=True",program);self.assertIn("after=search_values()",program);self.assertIn("if not refresh_confirmed:raise NavigationBlocked",program);self.assertIn("table.k-selectable",program);self.assertIn('if action=="refresh":return main_frame_action(node,"function(){this.click();return true;}")',program)
 def test_all_active_filter_uses_live_cdk_identity(self):
  program=browser_program("cdk-20260805T120000-abcdef12",5,"2026-08-05",30)
  self.assertIn('"slctDateSpan"',program);self.assertIn('"multi-select-dropdown"',program)
  self.assertIn('option=exact(ax(main["id"]),"All","button")',program);self.assertNotIn('"90 Days","button"',program);self.assertIn('if action=="active":return crm_frame_action(node,"function(){this.click();return true;}")',program)
  self.assertIn('Page.createIsolatedWorld',program);self.assertIn('executionContextId=frame_context(frame_id)',program);self.assertIn("closest('table.k-selectable')",program);self.assertNotIn("trim()==='Name'",program);self.assertIn("visible(e)&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'",program);self.assertIn('PROGRESS=ROOT/"private"/"review-progress.json"',program);self.assertIn('completed|set(reviewed)',program);self.assertIn('source_scope":"all_active"',program)
  self.assertNotIn('cdp("DOM.resolveNode",backendNodeId=backend_id)["object"]',program)
  authorize_action("days_control",ElementIdentity("Days","textbox","INPUT","slctDateSpan",onclick="fixed:DaysControl"))
  authorize_action("all_days",ElementIdentity("All","button","BUTTON"))
  with self.assertRaises(NavigationBlocked):authorize_action("days_control",ElementIdentity("Days","textbox","INPUT","wrong",onclick="fixed:DaysControl"))
 def test_cdk_tab_selection_before_active_verification(self):
  cdk={"targetId":"cdk","url":"https://app-unify.app.connectcdk.com/applications"};self.assertEqual(select_cdk_tab([{"targetId":"other","url":"https://example.com"},cdk]),cdk);verify_selected_tab(cdk,cdk)
  verify_selected_tab(cdk,{"url":cdk["url"],"title":"CDK Unify - Modern Retail CRM"})
  with self.assertRaises(NavigationBlocked):select_cdk_tab([{"targetId":"wrong-path","url":"https://app-unify.app.connectcdk.com/x"}])
  with self.assertRaises(NavigationBlocked):select_cdk_tab([])
  with self.assertRaises(NavigationBlocked):select_cdk_tab([cdk,{**cdk,"targetId":"two"}])
  with self.assertRaises(NavigationBlocked):verify_selected_tab(cdk,{"targetId":"other","url":cdk["url"]})
  with self.assertRaises(NavigationBlocked):verify_selected_tab(cdk,{"url":"https://app-unify.app.connectcdk.com/other"})
  self.assertLess(BROWSER_PROGRAM.index("tabs=list_tabs()"),BROWSER_PROGRAM.index("info=page_info()"))
  self.assertIn("verify_selected_tab(verified[0],info)",BROWSER_PROGRAM)
 def test_run_id_and_workspace_validation(self):
  valid="cdk-20260805T120000-abcdef12";self.assertEqual(validate_run_id(valid),valid)
  with tempfile.TemporaryDirectory() as td:self.assertEqual(analysis_workspace(Path(td),valid).parent,Path(td).resolve())
  for bad in ("../x","C:/absolute",valid+"/x",valid+"\\x",valid+"\u2215x","x"*200):
   with self.assertRaises(ValueError):validate_run_id(bad)
 def test_sender_validation_and_identity_key(self):
  cfg=config();cfg.validate();self.assertEqual(cfg.sender_identity_key,config().sender_identity_key);self.assertNotIn(cfg.sender_phone_e164,cfg.sender_identity_key)
  for bad in (config("2125550100"),config("+100"),SenderConfig("PERSONAL_AUTHORIZED","PERSONAL_DEVICE","","+12125550100","Dealer","VALID","scope","M","d",("SMS",),{"start":"09:00","end":"20:00"}),SenderConfig("PERSONAL_AUTHORIZED","PERSONAL_DEVICE","J","+12125550100","Dealer","VALID","scope","M","d",("SMS",),{"start":"bad","end":"20:00"})):
   with self.assertRaises(ValueError):bad.validate()
 def test_message_content_and_reconciliation_blocks(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);vault=IdentityVault(root/"v.db",Protector());record=IdentityRecord("1","A","B","A B","+12125550123",sms_eligibility_status="ELIGIBLE",eligibility_basis="consent");key=vault.upsert(record);queue=MessageQueue(root/"q.db",Protector());sender=config().sender_identity_key
   for body,kind,mode in (("","SMS","APPROVAL_QUEUE"),("   ","SMS","APPROVAL_QUEUE"),("x"*321,"SMS","APPROVAL_QUEUE"),("Hi","EMAIL","APPROVAL_QUEUE"),("Hi","SMS","BAD")):
    with self.assertRaises(ValueError):queue.enqueue(QueueRequest(key,record.phone_e164,sender,body,kind,"consent",mode),vault)
   pending=queue.enqueue(QueueRequest(key,record.phone_e164,sender,"Hi","SMS","consent","APPROVAL_QUEUE"),vault);queue.connection.execute("update messages set status='SENDING' where message_id=?",(pending.message_id,));queue.connection.commit();vault.extraction_upsert(IdentityRecord("1","A","B","A B","+12125550124"));blocked=queue.enqueue(QueueRequest(key,"+12125550124",sender,"Hi","SMS","consent","APPROVAL_QUEUE"),vault);self.assertEqual(blocked.blocked_reason,"reconciliation_required");queue.close();vault.close()
 def test_schema_and_report_brief_fields(self):
  schema=json.loads((ROOT/"schemas"/"analyzed-batch.schema.json").read_text());field=schema["properties"]["ranked_leads"]["items"]["properties"]["follow_up_text"];self.assertEqual(field["minLength"],1);self.assertIn("pattern",field)
  source=(ROOT/"scripts"/"report_renderer.py").read_text()
  for key in ("call_opening","discovery_questions","objections","voicemail","follow_up_text","appointment_close","human_handoff_flags","messaging_risk_flags"):self.assertIn(key,source)
if __name__=="__main__":unittest.main()
