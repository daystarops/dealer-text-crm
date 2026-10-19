import json,sys,tempfile,unittest
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/"scripts"),str(ROOT/"launcher")]
from analysis_semantics import validate_semantics
from cdk_copilot_launcher import windows_review_command
from cdk_runtime import normalize_phone,parse_detail_destination
from identity_vault import IdentityRecord,IdentityVault
from messaging import MessageQueue,QueueRequest,SenderAdapter
from privacy_gate import sanitize_payload
from sender_config import SenderConfig

class Protector:
 def protect(self,s):return s.encode()
 def unprotect(self,b):return b.decode()
class Adapter(SenderAdapter):
 sender_mode="PERSONAL_DEVICE"
 def __init__(self):self.sent=0
 def health_check(self):return True
 def send_message(self,*args):self.sent+=1;return {"provider_message_id":"p1","delivery_status":"accepted"}
def config():return SenderConfig("PERSONAL_AUTHORIZED","PERSONAL_DEVICE","Me","+12125550100","Dealer","VALID","outreach","Mgr","2026-08-01",("SMS",),{"start":"00:00","end":"23:59"},"APPROVAL_QUEUE",False,())

class AuditTests(unittest.TestCase):
 def test_launcher_command(self):
  self.assertEqual(windows_review_command("cmd.exe"),["cmd.exe","/d","/c",r"call scripts\run-review.cmd"])
  self.assertEqual(windows_review_command("cmd.exe",True),["cmd.exe","/d","/c",r"call scripts\run-review.cmd --interactive"])
 def test_hosts_and_routes(self):
  base="https://crm.connectcdk.com/evo2/x/Main.aspx"
  self.assertEqual(parse_detail_destination("../newprospects/OpptyDetails.aspx?lDID=12&lPID=9",base).opportunity_id,"12")
  for bad in ("https://evil.example/newprospects/OpptyDetails.aspx?lDID=12&lPID=9","javascript:alert(1)","data:text/plain,x","file:///x"):
   self.assertIsNone(parse_detail_destination(bad,base))
 def test_phone_formats_and_malformed(self):
  expected={"(212) 555-1234":"+12125551234","212-555-1234":"+12125551234","212 555 1234":"+12125551234","+1 212 555 1234":"+12125551234","+44 20 7946 0958":"+442079460958"}
  for raw,want in expected.items():self.assertEqual(normalize_phone(raw),want)
  for raw in ("2026-08-05","555","+00 12345678","212-ABC-1234",""):self.assertEqual(normalize_phone(raw),"")
 def test_privacy_regressions(self):
  value={"date":"2026-08-05","vehicle":"Ford Explorer","color":"White","note":"Alex Ford +44 20 7946 0958","us":"(212) 555-1234"}
  safe=sanitize_payload(value,{"Alex Ford":"Alex","Jamie White":"Jamie"}).safe_payload
  self.assertEqual(safe["date"],"2026-08-05");self.assertEqual(safe["vehicle"],"Ford Explorer");self.assertEqual(safe["color"],"White");self.assertNotIn("Alex Ford",json.dumps(safe));self.assertNotIn("7946",json.dumps(safe));self.assertNotIn("555-1234",json.dumps(safe))
 def test_semantic_validation_and_sort(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/"r.json";extract={"run_id":"r","leads":[{"lead_key":"a","first_name":"A"},{"lead_key":"b","first_name":"B"}]};p.write_text(json.dumps({"run_id":"r","ranked_leads":[{"lead_key":"a","first_name":"A","priority_score":1},{"lead_key":"b","first_name":"B","priority_score":9}]}));self.assertEqual(validate_semantics(p,extract)["ranked_leads"][0]["lead_key"],"b")
   p.write_text(json.dumps({"run_id":"r","ranked_leads":[]}));
   with self.assertRaises(ValueError):validate_semantics(p,extract)
 def test_idempotent_send(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);v=IdentityVault(root/"v.db",Protector());record=IdentityRecord("1","A","B","A B","+12125551234",sms_eligibility_status="ELIGIBLE",eligibility_basis="consent",eligibility_source="CRM");key=v.upsert(record);q=MessageQueue(root/"q.db",Protector());cfg=config();mid=q.enqueue(QueueRequest(key,record.phone_e164,cfg.sender_identity_key,"hello","SMS","consent","APPROVAL_QUEUE"),v).message_id;q.approve([mid]);a=Adapter();q.send_approved(mid,a,cfg,v,datetime.now().astimezone());
   with self.assertRaises(ValueError):q.send_approved(mid,a,cfg,v,datetime.now().astimezone())
   self.assertEqual(a.sent,1);updated=v.get(key);self.assertEqual(updated.provider_message_id,"p1");self.assertTrue(updated.last_message_at);q.close();v.close()

if __name__=="__main__":unittest.main()
