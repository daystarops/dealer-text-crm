import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from campaign_readiness import Candidate,classify,normalize_phone

def row(kind='texts',direction='',body='',timestamp='01/01/2026'):
    return {'activity_type':kind,'direction':direction,'sanitized_body':body,'activity_timestamp':timestamp}
def candidate(phone='2125550100',vehicle='Fictional Vehicle'):
    return Candidate('key','100','Fictional Person',normalize_phone(phone),vehicle,'Fictional Source','2026-01-01')

class CampaignReadinessTests(unittest.TestCase):
 def test_phone_normalization_supports_recipient_dedup_key(self):self.assertEqual(normalize_phone('+1 (212) 555-0100'),normalize_phone('212-555-0100'))
 def test_stop_and_internal_dnc_always_suppress(self):
  self.assertEqual(classify(candidate(),[row(body='Customer replied STOP')]).cohort,'SUPPRESS');self.assertEqual(classify(candidate(),[],dnc=True).cohort,'SUPPRESS')
 def test_wrong_number_and_sold_are_suppressed(self):
  self.assertEqual(classify(candidate(),[row(body='wrong number')]).cohort,'SUPPRESS');self.assertEqual(classify(candidate(),[row(body='customer already purchased')]).cohort,'SUPPRESS')
 def test_ambiguous_consent_never_automated(self):self.assertNotEqual(classify(candidate(),[]).cohort,'AUTOMATED_SMS_READY')
 def test_manual_and_automated_eligibility_are_separate(self):self.assertEqual(classify(candidate(),[]).cohort,'MANUAL_ONE_TO_ONE_REVIEW')
 def test_recent_conversation_routes_personal(self):self.assertEqual(classify(candidate(),[row(direction='inbound',body='customer replied')]).cohort,'CALL_ONLY_OR_PERSONAL_FOLLOW_UP')
 def test_duplicate_opportunity_is_suppressed(self):self.assertEqual(classify(candidate(),[],duplicate=True).cohort,'SUPPRESS')
 def test_name_opportunity_vehicle_message_pairing(self):
  c=classify(candidate(vehicle='Fictional Roadster'),[]);self.assertEqual((c.full_name,c.opportunity_id),('Fictional Person','100'));self.assertIn('Fictional Roadster',c.message)
 def test_no_send_or_cdk_write_capability(self):
  source=(ROOT/'scripts/campaign_readiness.py').read_text();self.assertNotIn('browser-use',source);self.assertNotIn('send_message(',source);self.assertNotIn('new_tab(',source);self.assertNotIn('Save',source)

if __name__=='__main__':unittest.main()
