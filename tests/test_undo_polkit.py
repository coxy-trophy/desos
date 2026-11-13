"""The polkit action the desktop uses to run desos-undo as root through pkexec.

pkexec picks an action by program path alone, so this one applies to every
desos-undo command, "run -- CMD" included. These tests pin it to the launcher
and keep it from ever asking for less than an administrator's password.
"""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True  # the component tree must never gain __pycache__
REPO = Path(__file__).resolve().parents[1]
ROOTFS = REPO / 'undo' / 'rootfs'
POLICY = ROOTFS / 'usr/share/polkit-1/actions/org.desos.undo.policy'
EXEC_PATH = 'org.freedesktop.policykit.exec.path'


class UndoPolkitPolicy(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(POLICY).getroot()
        self.actions = self.root.findall('action')

    def test_is_a_policy_file_with_one_action(self):
        self.assertEqual(self.root.tag, 'policyconfig')
        self.assertEqual([action.get('id') for action in self.actions], ['org.desos.undo.manage'])
        self.assertTrue(POLICY.name.startswith('org.desos.undo'))
        head = POLICY.read_text(encoding='utf-8')[:400]
        self.assertIn('PolicyKit Policy Configuration 1.0', head)

    def test_names_the_shipped_launcher_by_absolute_path(self):
        (action,) = self.actions
        annotations = {item.get('key'): (item.text or '').strip() for item in action.findall('annotate')}
        self.assertEqual(annotations.get(EXEC_PATH), '/usr/sbin/desos-undo')
        launcher = ROOTFS / annotations[EXEC_PATH].lstrip('/')
        self.assertTrue(launcher.is_file(), 'the path must be the launcher this component ships')

    def test_always_asks_for_an_administrator_and_never_remembers(self):
        (action,) = self.actions
        defaults = action.find('defaults')
        self.assertIsNotNone(defaults)
        found = {item.tag: (item.text or '').strip() for item in defaults}
        self.assertEqual(found, {'allow_any': 'auth_admin', 'allow_inactive': 'auth_admin', 'allow_active': 'auth_admin'})

    def test_adds_nothing_beyond_the_path(self):
        (action,) = self.actions
        keys = [item.get('key') for item in action.findall('annotate')]
        # No allow_gui (the tool has no windows), no argv1 (it would only narrow by
        # first argument, which is "--json" for every command the desktop runs).
        self.assertEqual(keys, [EXEC_PATH])

    def test_says_what_is_being_authorised(self):
        (action,) = self.actions
        message = action.findtext('message') or ''
        self.assertIn('snapshot', message)
        self.assertIn('earlier system state', message)
        self.assertTrue(action.findtext('description'))


if __name__ == '__main__':
    unittest.main()
