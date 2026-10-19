import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))

from activity_end import PaneEndDetector,PaneObservation
from activity_history import category_sections,deduplicate_activities
from cdk_exhaustive_detail_driver import browser_program
from detail_index import DetailIndex
from discovery_index import DiscoveredLead,DiscoveryIndex
from exhaustive_detail_index import ExhaustiveDetailIndex,REQUIRED_SECTIONS


def url(number):return f"https://crm.connectcdk.com/evo2/fictional/newprospects/OpptyDetails.aspx?lDID={number}&lPID={int(number)+100}"


class ExhaustiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'index.db'
        with DiscoveryIndex(self.path) as d:
            d.begin_run('d');d.persist_rows([DiscoveredLead('1',url('1'),'Fictional Person')],'d')
        self.index=ExhaustiveDetailIndex(self.path);self.claim=self.index.claim_next('run')
    def tearDown(self):self.index.close();self.temp.cleanup()
    def scan(self):return {'scroll_cycles':4,'true_end':True,'container_selector':'document.scrollingElement','pagination_detected':False,'resume_scroll_top':100}
    def all_sections(self,activities=None):
        activities=activities or []
        sections=category_sections(activities,self.scan())
        sections.append({'section_name':'audit_trail','status':'empty','activity_count':0,**self.scan()})
        return sections
    def test_empty_tabs_are_explicit_and_completion_succeeds(self):
        sections=self.all_sections();self.index.save_sections(self.claim,sections,[])
        self.index.complete(self.claim,{'lead_key':self.claim.stable_lead_key},'run')
        self.assertEqual(self.index.state_count('detail_complete'),1)
        self.assertEqual(set(self.index.section_statuses(self.claim)),set(REQUIRED_SECTIONS))
    def test_partial_section_failure_refuses_detail_complete(self):
        self.index.save_sections(self.claim,self.all_sections()[:-1],[])
        with self.assertRaisesRegex(RuntimeError,'missing'):self.index.complete(self.claim,{},'run')
        self.assertEqual(self.index.state_count('detail_in_progress'),1)
    def test_restart_during_activity_tab_preserves_finished_sections(self):
        history=category_sections([],self.scan());self.index.save_sections(self.claim,history,[])
        self.index.fail(self.claim,'history_timeout',terminal=False,retry_delay_seconds=0)
        again=self.index.claim_next('restart',datetime.now(timezone.utc)+timedelta(seconds=1))
        self.assertEqual(len(self.index.section_statuses(again)),7)
        self.index.save_sections(again,[{'section_name':'audit_trail','status':'empty','activity_count':0,**self.scan()}],[])
        self.index.complete(again,{},'restart');self.assertEqual(self.index.state_count('detail_complete'),1)
    def test_duplicate_activities_across_views_are_database_deduplicated(self):
        a=deduplicate_activities(['01/01/2026 Outbound call completed'],'history',{'Fictional Person':'[customer]'})
        b=deduplicate_activities(['01/01/2026 Outbound call completed'],'audit_trail',{'Fictional Person':'[customer]'})
        self.index.save_sections(self.claim,self.all_sections(a),a+b)
        self.assertEqual(self.index.activity_count(self.claim.stable_lead_key),1)
    def test_legacy_complete_is_requeued_once_for_exhaustive_scan(self):
        self.index.close()
        # A fresh legacy-style database demonstrates the one-time migration boundary.
        legacy=Path(self.temp.name)/'legacy.db'
        with DiscoveryIndex(legacy) as d:d.begin_run('d');d.persist_rows([DiscoveredLead('2',url('2'),'Fictional Two')],'d')
        with DetailIndex(legacy) as d:
            claim=d.claim_next('old');d.complete(claim,{},'old');self.assertEqual(d.state_count('detail_complete'),1)
        with ExhaustiveDetailIndex(legacy) as exhaustive:self.assertEqual(exhaustive.state_count('pending_detail'),1)
        self.index=ExhaustiveDetailIndex(self.path)


class ActivityFixtureTests(unittest.TestCase):
    def obs(self,ids=('a','b'),top=900,new=0,loading=False,pager=False):return PaneObservation(tuple(ids),top,100,1000,loading,new,pager)
    def test_lazy_loaded_history_resets_no_new_counter(self):
        d=PaneEndDetector();d.observe(self.obs());d.observe(self.obs(ids=('b','c'),new=1));self.assertEqual(d.no_new_cycles,0)
    def test_pagination_blocks_end_until_next_page_consumed(self):
        d=PaneEndDetector();[d.observe(self.obs(pager=True)) for _ in range(3)];self.assertFalse(d.ready_for_final_probe(self.obs(pager=True),900))
        d.observe(self.obs(ids=('c','d'),new=2));self.assertEqual(d.no_new_cycles,0)
    def test_virtualized_rows_require_stable_final_sequence(self):
        d=PaneEndDetector();[d.observe(self.obs()) for _ in range(3)]
        self.assertFalse(d.confirms_end(self.obs(),self.obs(ids=('b','c'))));self.assertTrue(d.confirms_end(self.obs(),self.obs()))
    def test_category_counts_cover_required_activity_types(self):
        rows=['Phone call outbound','Email sent','Text Message inbound','General note','Appointment scheduled','Task due']
        acts=deduplicate_activities(rows,'history',{})
        sections={s['section_name']:s for s in category_sections(acts,{'scroll_cycles':1,'true_end':True,'container_selector':'document.scrollingElement','pagination_detected':False})}
        self.assertTrue(all(sections[name]['activity_count']==1 for name in ('calls','emails','texts','notes','appointments','tasks')))
    def test_multiple_tabs_and_tab_specific_document_scrollers_are_encoded(self):
        p=browser_program('run',Path('private/index.db'),5,900,60);compile(p,'<exhaustive>','exec')
        self.assertIn("wait_target('history.aspx')",p);self.assertIn("wait_target('CustomerAuditTrail.aspx')",p)
        self.assertIn("document.scrollingElement",p);self.assertIn("#liAuditTrail",p)
        self.assertIn("#gvOpptyHistory table",p);self.assertIn("#GridViewAuditTrail",p)
        self.assertIn('Page.createIsolatedWorld',p)
        for forbidden in ('x_email','x_phone','x_addnote','Save','Send','Submit','score','outreach'):self.assertNotIn(forbidden,p)


if __name__=='__main__':unittest.main()
