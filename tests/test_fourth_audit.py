import contextlib,io,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"scripts"))
import pipeline_runner
from analysis_pipeline import codex_command
from browser_recovery import recovery_program
from cdk_review_driver import BROWSER_PROGRAM
from identity_vault import IdentityRecord,IdentityVault
from messaging import MessageQueue,QueueRequest
from privacy_gate import PrivacyGateError,assert_no_residual_pii,sanitize_text

class Protector:
 def protect(self,s):return s.encode()
 def unprotect(self,b):return b.decode()

class FourthAuditTests(unittest.TestCase):
 def test_first_stage_failure_is_browser_extraction(self):
  reports=[]
  with patch.object(sys,"argv",["pipeline_runner.py"]),patch.object(pipeline_runner,"run_stage",side_effect=RuntimeError("failed")),patch.object(pipeline_runner,"failure_report",side_effect=lambda stage,reason:reports.append((stage,reason))):
   self.assertEqual(pipeline_runner.main(),1)
  self.assertEqual(reports,[('browser-extraction','RuntimeError')])
 def test_no_identity_command_argument_or_stdout_metadata(self):
  command=codex_command("codex",Path("C:/isolated"));self.assertNotIn("Alex Smith"," ".join(command))
  source=(ROOT/"scripts"/"pipeline_runner.py").read_text();self.assertNotIn("--identities-json",source);self.assertNotIn('metadata["identities"]',source)
  analysis=(ROOT/"scripts"/"analysis_pipeline.py").read_text();self.assertNotIn('"identities":identities',analysis)
 def test_previous_queue_schema_migrates_without_losing_rows(self):
  with tempfile.TemporaryDirectory() as td:
   path=Path(td)/"q.db";db=sqlite3.connect(path);db.execute("""CREATE TABLE messages(message_id TEXT PRIMARY KEY,lead_key TEXT NOT NULL,destination BLOB NOT NULL,sender_identity_reference TEXT NOT NULL,message_body BLOB NOT NULL,message_type TEXT NOT NULL,eligibility_basis TEXT NOT NULL,automation_mode TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,approved_at TEXT,sent_at TEXT,provider_message_id TEXT,delivery_status TEXT,failure_reason TEXT,conversation_thread_id BLOB,reply_classification TEXT,human_handoff_required INTEGER NOT NULL DEFAULT 0)""");db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("old","lead",b"phone","sender",b"body","SMS","basis","APPROVAL_QUEUE","PENDING_APPROVAL","2026-01-01",None,None,None,None,None,b"thread",None,0));db.commit();db.close();queue=MessageQueue(path,Protector());columns={r[1] for r in queue.connection.execute("pragma table_info(messages)")};self.assertTrue({"idempotency_key","workflow_campaign","template_version"}<=columns);self.assertEqual(queue.connection.execute("select count(*) from messages").fetchone()[0],1);self.assertEqual(queue.connection.execute("pragma user_version").fetchone()[0],2);queue.close()
 def test_identity_schema_version_and_send_state_contract(self):
  with tempfile.TemporaryDirectory() as td:
   vault=IdentityVault(Path(td)/"v.db",Protector());self.assertEqual(vault.connection.execute("pragma user_version").fetchone()[0],1);self.assertIn("SENDING",(ROOT/"scripts"/"messaging.py").read_text());self.assertIn("Never auto-retry",(ROOT/"scripts"/"messaging.py").read_text());vault.close()
 def test_incomplete_extraction_preserves_valid_fields(self):
  with tempfile.TemporaryDirectory() as td:
   vault=IdentityVault(Path(td)/"v.db",Protector());old=IdentityRecord("1","Alex","Smith","Alex Smith","+12125550123","Explorer","thread");key=vault.upsert(old);vault.extraction_upsert(IdentityRecord("1","","","","","",""));same=vault.get(key);self.assertEqual((same.full_name,same.phone_e164,same.vehicle),("Alex Smith","+12125550123","Explorer"));vault.extraction_upsert(IdentityRecord("1","Alexa","Jones","Alexa Jones","+12125550124","Transit","thread2"));new=vault.get(key);self.assertEqual((new.full_name,new.phone_e164,new.vehicle),("Alexa Jones","+12125550124","Transit"));vault.close()
 def test_phone_change_supersedes_active_message(self):
  with tempfile.TemporaryDirectory() as td:
   root=Path(td);vault=IdentityVault(root/"v.db",Protector());old=IdentityRecord("1","A","B","A B","+12125550123",sms_eligibility_status="ELIGIBLE",eligibility_basis="consent");key=vault.upsert(old);queue=MessageQueue(root/"q.db",Protector());request=QueueRequest(key,old.phone_e164,"sender","Hello","SMS","consent","APPROVAL_QUEUE");first=queue.enqueue(request,vault).message_id;vault.extraction_upsert(IdentityRecord("1","A","B","A B","+12125550124"));new=QueueRequest(key,"+12125550124","sender","Hello","SMS","consent","APPROVAL_QUEUE");second=queue.enqueue(new,vault).message_id;self.assertNotEqual(first,second);self.assertEqual(queue.connection.execute("select status from messages where message_id=?",(first,)).fetchone()[0],"SUPERSEDED");self.assertEqual(queue.approve([first]),None);self.assertEqual(queue.connection.execute("select status from messages where message_id=?",(first,)).fetchone()[0],"SUPERSEDED")
   with self.assertRaises(ValueError):queue.enqueue(QueueRequest(key,"","sender","Hello","SMS","consent","APPROVAL_QUEUE"),vault)
   queue.close();vault.close()
 def test_vehicle_strings_are_not_phones(self):
  for value in ("2026 350 HR Transit","2025 250 148 WB","Transit 350 148 extended","24 months 12k miles","36 10.5k lease"):self.assertEqual(sanitize_text(value),value)
  for value in ("(212) 555-1234","212-555-1234","212 555 1234","+1 212 555 1234"):
   with self.assertRaises(PrivacyGateError):assert_no_residual_pii(value,{})
 def test_frames_recovery_and_log_quarantine_are_explicit(self):
  for helper in ("verified_crm_frame","verified_main_frame","verified_history_frame","verified_detail_frame"):self.assertIn("def "+helper,BROWSER_PROGRAM)
  self.assertNotIn("detail_nodes[0]",BROWSER_PROGRAM);self.assertNotIn("ax()",BROWSER_PROGRAM)
  recovery=recovery_program("run");self.assertIn("after={t[\"targetId\"]:t for t in list_tabs()}",recovery);self.assertIn("remained open",recovery);self.assertIn("active=page_info()",recovery)
  analysis=(ROOT/"scripts"/"analysis_pipeline.py").read_text();self.assertIn("codex-events.pending.jsonl",analysis);self.assertIn("os.replace(temporary_log,final_log)",analysis);self.assertIn("temporary_log.unlink",analysis);self.assertIn('"event_log":"redacted"',analysis)

if __name__=="__main__":unittest.main()
