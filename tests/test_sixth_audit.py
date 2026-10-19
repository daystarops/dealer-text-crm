import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

from cdk_review_driver import BROWSER_PROGRAM, validate_shape
from cdk_runtime import ElementIdentity, NavigationBlocked, authorize_action, parse_detail_destination
from identity_vault import IdentityRecord, IdentityVault
from messaging import DefinitelyNotSentError, MessageQueue, QueueRequest, SenderAdapter, UncertainDeliveryError
from privacy_gate import assert_no_residual_pii, sanitize_text
from report_renderer import render_operational_html
from sender_config import SenderConfig
from launcher.cdk_copilot_launcher import windows_review_command

class Protector:
    def protect(self,value): return value.encode()
    def unprotect(self,value): return value.decode()

class DefinitelyFails(SenderAdapter):
    sender_mode='PERSONAL_DEVICE'
    def health_check(self): return True
    def send_message(self,destination,body,thread_id=''): raise DefinitelyNotSentError('not sent')

class AmbiguousFailure(SenderAdapter):
    sender_mode='PERSONAL_DEVICE'
    def health_check(self): return True
    def send_message(self,destination,body,thread_id=''): raise RuntimeError('network broke')

def config():
    return SenderConfig('PERSONAL_AUTHORIZED','PERSONAL_DEVICE','Jordan','+12125550100','Dealer','VALID','Authorized outreach','Manager','2026-01-01',('SMS',),{'start':'00:00','end':'23:59'})

class SixthAuditTests(unittest.TestCase):
    def test_days_control_is_allowlisted_and_opened_before_all(self):
        allowed=ElementIdentity('Days','textbox','INPUT','slctDateSpan',onclick='fixed:DaysControl')
        authorize_action('days_control',allowed)
        with self.assertRaises(NavigationBlocked):
            authorize_action('days_control',ElementIdentity('Days','textbox','INPUT','wrong',onclick='fixed:DaysControl'))
        self.assertLess(BROWSER_PROGRAM.index('activate(days_control,"days_control")'),BROWSER_PROGRAM.index('activate(option,"all_days")'))

    def test_duplicate_detail_query_parameters_are_rejected(self):
        base='https://crm.connectcdk.com/evo2/x/Main.aspx'
        self.assertIsNotNone(parse_detail_destination('../newprospects/OpptyDetails.aspx?lDID=12&lPID=9',base))
        live_base='https://crm.connectcdk.com/evo2/fresh/elead-v45/elead_track/NewProspects/Prospects.aspx?Folder=1&Title=Active'
        self.assertIsNotNone(parse_detail_destination('../newprospects/OpptyDetails.aspx?lDID=12&lPID=9&Loc=PT',live_base))
        self.assertIsNone(parse_detail_destination('../newprospects/OpptyDetails.aspx?lDID=12&lDID=13&lPID=9',base))
        self.assertIsNone(parse_detail_destination('../newprospects/OpptyDetails.aspx?lDID=12&lPID=9&lPID=10',base))

    def test_schema_validator_enforces_pattern_and_enum(self):
        schema={'type':'object','required':['value','mode'],'additionalProperties':False,'properties':{'value':{'type':'string','pattern':r'.*\S.*'},'mode':{'type':'string','enum':['A']}}}
        self.assertTrue(validate_shape({'value':'   ','mode':'B'},schema))
        self.assertFalse(validate_shape({'value':'ok','mode':'A'},schema))

    def test_launcher_interactive_argument_is_inside_command_text(self):
        self.assertEqual(windows_review_command('cmd.exe',False),['cmd.exe','/d','/c',r'call scripts\run-review.cmd'])
        self.assertEqual(windows_review_command('cmd.exe',True),['cmd.exe','/d','/c',r'call scripts\run-review.cmd --interactive'])

    def test_contextual_surname_redaction_preserves_vehicle_make(self):
        identities={'Alex Ford':'Alex'}
        self.assertEqual(sanitize_text('2026 Ford Explorer',identities),'2026 Ford Explorer')
        self.assertNotIn('Ford',sanitize_text('Mr. Ford asked to call later',identities))
        self.assertNotIn('Ford',sanitize_text('Ford, Alex asked to call later',identities))
        with self.assertRaises(Exception): assert_no_residual_pii('Customer: Ford asked to call later',identities)

    def _queue(self,temp):
        vault=IdentityVault(Path(temp)/'v.db',Protector())
        record=IdentityRecord('1','A','B','A B','+12125550123',sms_eligibility_status='ELIGIBLE',eligibility_basis='consent')
        key=vault.upsert(record)
        queue=MessageQueue(Path(temp)/'q.db',Protector())
        request=QueueRequest(key,record.phone_e164,config().sender_identity_key,'Hello','SMS','consent','APPROVAL_QUEUE')
        result=queue.enqueue(request,vault);queue.approve([result.message_id])
        return vault,queue,result.message_id

    def test_send_failures_default_to_uncertain_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            vault,queue,message_id=self._queue(td)
            with self.assertRaises(UncertainDeliveryError): queue.send_approved(message_id,AmbiguousFailure(),config(),vault)
            self.assertEqual(queue.connection.execute('select status from messages where message_id=?',(message_id,)).fetchone()[0],'UNCERTAIN_DELIVERY')
            queue.close();vault.close()
        with tempfile.TemporaryDirectory() as td:
            vault,queue,message_id=self._queue(td)
            with self.assertRaises(DefinitelyNotSentError): queue.send_approved(message_id,DefinitelyFails(),config(),vault)
            self.assertEqual(queue.connection.execute('select status from messages where message_id=?',(message_id,)).fetchone()[0],'FAILED')
            queue.close();vault.close()

    def test_operational_report_fails_on_missing_identity(self):
        class Vault:
            def get(self,key): return None
        analysis={'ranked_leads':[{'lead_key':'a'*64,'first_name':'A','priority_score':1,'latest_explicit_customer_position':'Unknown','main_obstacle':'Unknown','recommended_next_action':'Call','call_opening':'Hi','discovery_questions':['1','2','3'],'objections':[{'objection':'a','response':'b'},{'objection':'c','response':'d'}],'voicemail':'v','follow_up_text':'t','appointment_close':'c','human_handoff_flags':[],'messaging_risk_flags':[]}]}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(KeyError):render_operational_html(analysis,Vault(),Path(td)/'r.html',lambda path:None)

if __name__=='__main__': unittest.main()
