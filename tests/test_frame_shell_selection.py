from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from cdk_runtime import NavigationBlocked, select_crm_shell_frame
from cdk_review_driver import BROWSER_PROGRAM


class FrameShellSelectionTests(unittest.TestCase):
    def test_selects_nearest_approved_ancestor_when_multiple_crm_frames_exist(self):
        frames = [
            {"id": "top", "url": "https://app-unify.app.connectcdk.com/applications", "name": ""},
            {"id": "shell", "parentId": "top", "url": "https://crm.connectcdk.com/evo2/shell.aspx", "name": "CRM"},
            {"id": "nav", "parentId": "shell", "url": "https://crm.connectcdk.com/evo2/nav.aspx", "name": "Nav"},
            {"id": "main", "parentId": "shell", "url": "about:blank", "name": "Main"},
        ]
        self.assertEqual(select_crm_shell_frame(frames)["id"], "shell")

    def test_walks_past_unapproved_intermediate_ancestor(self):
        frames = [
            {"id": "shell", "url": "https://crm.connectcdk.com/evo2/shell.aspx", "name": "CRM"},
            {"id": "wrapper", "parentId": "shell", "url": "about:blank", "name": ""},
            {"id": "main", "parentId": "wrapper", "url": "about:blank", "name": "Main"},
        ]
        self.assertEqual(select_crm_shell_frame(frames)["id"], "shell")

    def test_rejects_multiple_main_frames(self):
        frames = [
            {"id": "shell", "url": "https://crm.connectcdk.com/evo2/shell.aspx", "name": "CRM"},
            {"id": "main1", "parentId": "shell", "url": "about:blank", "name": "Main"},
            {"id": "main2", "parentId": "shell", "url": "about:blank", "name": "Main"},
        ]
        with self.assertRaises(NavigationBlocked):
            select_crm_shell_frame(frames)

    def test_browser_program_uses_structural_shell_selection(self):
        self.assertIn("def verified_crm_frame():return select_crm_shell_frame(all_frames())", BROWSER_PROGRAM)
        self.assertNotIn('f.get("name")!="Main" and "crm" in f.get("url","").casefold()', BROWSER_PROGRAM)


if __name__ == "__main__":
    unittest.main()
