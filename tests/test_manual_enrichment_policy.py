import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from manual_enrichment_policy import *
def decide(**kw):
 defaults=dict(indexed_phone='2125550100',status='Active',activity_text='',record_context_valid=True,vehicle='Fictional Roadster')
 defaults.update(kw);return classify_enrichment(**defaults)
class ManualEnrichmentPolicyTests(unittest.TestCase):
 def test_indexed_identity_phone_supports_manual_when_live_absent(self):
  result=decide(resolver_failed=True);self.assertEqual((result[0],result[3]),('MANUAL_TEXT_PILOT_APPROVED',INDEXED_PHONE_VERIFIED_BY_IDENTITY_PAIRING))
 def test_live_indexed_match_is_live_verified(self):self.assertEqual(decide(live_phone='+1 212 555 0100')[3],LIVE_PHONE_VERIFIED)
 def test_live_indexed_mismatch_is_conflict(self):
  result=decide(live_phone='6465550100');self.assertEqual((result[0],result[3]),('INSUFFICIENT_EVIDENCE',PHONE_CONFLICT))
 def test_missing_phones_are_unavailable(self):
  result=decide(indexed_phone='');self.assertEqual((result[0],result[3]),('INSUFFICIENT_EVIDENCE',PHONE_UNAVAILABLE))
 def test_pairing_is_mandatory(self):self.assertEqual(decide(identity_pairing_valid=False)[0],'INSUFFICIENT_EVIDENCE')
 def test_stop_dnc_and_wrong_number_suppress(self):
  for text in ('Customer replied STOP','Please do not contact me','wrong number','line disconnected'):
   self.assertEqual(decide(activity_text=text)[0],'SUPPRESS')
 def test_salesperson_and_missing_preference_are_neutral(self):
  self.assertEqual(decide(salesperson_assignment='Fictional Salesperson',contact_preference=None)[0],'MANUAL_TEXT_PILOT_APPROVED')
 def test_duplicate_phones_are_deduplicated(self):
  rows=[{'phone':'2125550100','id':'a'},{'phone':'+1 212 555 0100','id':'b'}];self.assertEqual([r['id'] for r in deduplicate_by_phone(rows)],['a'])
 def test_no_automated_eligibility(self):self.assertNotIn('AUTOMATED',decide()[0])
 def test_question_routes_personalized(self):self.assertEqual(decide(activity_text='Could you answer my question?',inbound_present=True)[0],'PERSONALIZED_RESPONSE_REQUIRED')
 def test_no_send_or_cdk_write_capability(self):
  source=(ROOT/'scripts/manual_enrichment_policy.py').read_text()
  for token in ('send_message(','click(','Save','browser-use'):self.assertNotIn(token,source)
if __name__=='__main__':unittest.main()
