import tempfile
import unittest
from pathlib import Path

from image.paths import validate_rootfs


class RootfsPathTests(unittest.TestCase):
    def test_accepts_only_a_separate_child_of_build_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            self.assertEqual(validate_rootfs(parent / 'rootfs', parent), parent / 'rootfs')

    def test_refuses_root_parent_and_escape_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for target in [Path('/'), parent, parent / '..' / 'outside']:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    validate_rootfs(target, parent)

    def test_refuses_symlink_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / 'real').mkdir()
            (parent / 'rootfs').symlink_to(parent / 'real', target_is_directory=True)
            with self.assertRaises(ValueError):
                validate_rootfs(parent / 'rootfs', parent)

    def test_refuses_parent_that_is_the_system_root(self):
        with self.assertRaises(ValueError):
            validate_rootfs(Path('/etc'), Path('/'))
