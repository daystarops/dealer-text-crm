import unittest
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from cdk_review_driver import browser_program

class FrameScopedResolutionTests(unittest.TestCase):
    def test_ax_nodes_are_bound_to_their_verified_frame(self):
        program=browser_program("cdk-20260805T120000-abcdef12",5,"2026-08-05",30)
        self.assertIn("BACKEND_FRAME={}",program)
        self.assertIn("BACKEND_FRAME[backend_id]=frame_id",program)
        self.assertIn('Page.createIsolatedWorld',program)
        self.assertIn('DOM.resolveNode",backendNodeId=backend_id,executionContextId=frame_context(frame_id)',program)

    def test_unscoped_backend_resolution_is_not_used(self):
        program=browser_program("cdk-20260805T120000-abcdef12",5,"2026-08-05",30)
        self.assertNotIn('DOM.resolveNode",backendNodeId=backend_id)[',program)

if __name__=="__main__":unittest.main()
