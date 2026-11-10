"""DesOS installer integration contracts; never partition or chroot a host disk."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

BASE = Path(__file__).resolve().parents[1] / "installer"
# Observed in the pinned Ubuntu 26.04 greetd package's
# /usr/lib/systemd/system/greetd.service; this is the service's VT contract,
# not a complete replacement unit. It excludes tty7, rather than tty1.
UBUNTU_GREETD_UNIT_VT_CONTRACT = """[Unit]
After=getty@tty7.service
Conflicts=getty@tty7.service
"""


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.module = load("desos_cleanup", BASE / "modules/desoscleanup/cleanup.py")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.write("etc/os-release", 'ID=desos\nID_LIKE="ubuntu debian"\n')
        self.write("etc/passwd", "root:x:0:0:root:/root:/bin/bash\ndesos:x:1000:1000:Live:/home/desos:/bin/bash\nalex:x:1001:1001:Alex:/home/alex:/bin/bash\n")
        self.write("etc/shadow", "root:!:1:0:99999:7:::\ndesos:!:1:0:99999:7:::\nalex:$y$j9T$testhash:1:0:99999:7:::\n")
        self.write("home/alex/keep", "chosen user's file")
        self.commands = []

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def run_target(self, args):
        self.commands.append(args)
        if args == ["/usr/sbin/userdel", "desos"]:
            for name in ("passwd", "shadow"):
                path = self.root / "etc" / name
                path.write_text("".join(line for line in path.read_text().splitlines(True) if not line.startswith("desos:")))

    def clean(self, user="alex"):
        with patch.object(self.module.os.path, "ismount", return_value=True):
            self.module.cleanup_target(self.root, user, self.run_target)

    def test_removes_live_privileges_and_account_preserves_new_user(self):
        for name in ("etc/sudoers.d/90-desos-live", "etc/sudoers.d/casper", "etc/polkit-1/rules.d/49-desos-live-installer.rules", "etc/casper.conf", "usr/lib/desos/live-session", "etc/initramfs-tools/conf.d/desos-live", "usr/share/initramfs-tools/scripts/casper-bottom/99-desos-live", "home/desos/.ssh/authorized_keys", "root/.ssh/authorized_keys", "etc/ssh/ssh_host_ed25519_key", "etc/systemd/system/getty@tty1.service.d/autologin.conf"):
            self.write(name, "live state")
        self.write("etc/greetd/config.toml", '[initial_session]\nuser="desos"\n')
        self.clean()
        self.assertNotIn("desos:", (self.root / "etc/passwd").read_text())
        self.assertNotIn("desos:", (self.root / "etc/shadow").read_text())
        self.assertFalse((self.root / "home/desos").exists())
        self.assertFalse((self.root / "etc/sudoers.d/90-desos-live").exists())
        self.assertFalse((self.root / "etc/sudoers.d/casper").exists())
        self.assertFalse((self.root / "etc/polkit-1/rules.d/49-desos-live-installer.rules").exists())
        self.assertFalse((self.root / "etc/casper.conf").exists())
        self.assertFalse((self.root / "usr/lib/desos/live-session").exists())
        self.assertFalse((self.root / "etc/initramfs-tools/conf.d/desos-live").exists())
        self.assertFalse((self.root / "usr/share/initramfs-tools/scripts/casper-bottom/99-desos-live").exists())
        self.assertFalse((self.root / "root/.ssh").exists())
        self.assertFalse((self.root / "etc/ssh/ssh_host_ed25519_key").exists())
        self.assertFalse((self.root / "etc/systemd/system/getty@tty1.service.d/autologin.conf").exists())
        self.assertEqual((self.root / "home/alex/keep").read_text(), "chosen user's file")
        greetd = (self.root / "etc/greetd/config.toml").read_text()
        self.assertIn('[default_session]', greetd)
        self.assertIn('/usr/bin/des-ui-greeter', greetd)
        self.assertNotIn('initial_session', greetd)
        self.assertIn(["/usr/bin/apt-get", "-y", "purge", "casper"], self.commands)

    def test_refuses_host_root_unmounted_tree_and_live_username(self):
        for path, user in ((Path("/"), "alex"), (self.root, "desos")):
            with self.subTest(path=path, user=user), self.assertRaises(ValueError):
                self.module.cleanup_target(path, user, self.run_target)
        with patch.object(self.module.os.path, "ismount", return_value=False), self.assertRaises(ValueError):
            self.module.cleanup_target(self.root, "alex", self.run_target)
        self.assertEqual(self.commands, [])

    def test_refuses_wrong_identity_missing_user_or_unset_password(self):
        for identity, passwd, shadow in (("ubuntu", True, True), ("desos", False, True), ("desos", True, False)):
            self.write("etc/os-release", f"ID={identity}\n")
            self.write("etc/passwd", "alex:x:1001:1001:Alex:/home/alex:/bin/bash\n" if passwd else "root:x:0:0::/root:/bin/bash\n")
            self.write("etc/shadow", "alex:$y$hash:1:0:99999:7:::\n" if shadow else "alex:!:1:0:99999:7:::\n")
            with self.subTest(identity=identity, passwd=passwd, shadow=shadow), self.assertRaises(ValueError):
                self.clean()
        self.assertEqual(self.commands, [])

    def test_refuses_symlink_escape_before_mutating(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside) / "90-desos-live"
            sentinel.write_text("must survive")
            (self.root / "etc/sudoers.d").symlink_to(outside)
            with self.assertRaises(ValueError):
                self.clean()
            self.assertEqual(sentinel.read_text(), "must survive")
            self.assertEqual(self.commands, [])

    def test_new_username_containing_desos_is_preserved(self):
        for filename in ("passwd", "shadow"):
            path = self.root / "etc" / filename
            path.write_text(path.read_text().replace("alex:", "mydesos:"))
        self.clean("mydesos")
        self.assertIn("mydesos:", (self.root / "etc/passwd").read_text())

    def test_installed_greetd_vt_matches_pinned_service_getty_conflict(self):
        self.write("usr/lib/systemd/system/greetd.service", UBUNTU_GREETD_UNIT_VT_CONTRACT)
        self.clean()
        service = (self.root / "usr/lib/systemd/system/greetd.service").read_text()
        excluded_getty = next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("Conflicts="))
        configuration = tomllib.loads((self.root / "etc/greetd/config.toml").read_text())
        configured_getty = f"getty@tty{configuration['terminal']['vt']}.service"
        self.assertIn(configured_getty, excluded_getty.split())
        self.assertNotIn("initial_session", configuration)
        self.assertEqual(configuration["default_session"]["command"], "/usr/bin/des-ui-greeter")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.helper = load("desos_helper", BASE / "bin/desos-installer-helper.py")

    def test_requires_live_casper_boot_and_real_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "cmdline").write_text("quiet boot=casper maybe-ubiquity")
            (base / "source.squashfs").write_bytes(b"hsqs")
            self.helper.validate_live_media(base / "cmdline", base / "source.squashfs")
            (base / "cmdline").write_text("quiet root=UUID=installed")
            with self.assertRaises(ValueError):
                self.helper.validate_live_media(base / "cmdline", base / "source.squashfs")
            (base / "cmdline").write_text("boot=casper")
            (base / "source.squashfs").unlink()
            with self.assertRaises(ValueError):
                self.helper.validate_live_media(base / "cmdline", base / "source.squashfs")

    def test_rejects_remote_display_and_wayland_path_traversal(self):
        for display, wayland in (("evil.example:0", ""), (":0;sh", ""), ("", "../../tmp/socket"), ("", "/tmp/socket")):
            with self.subTest(display=display, wayland=wayland), self.assertRaises(ValueError):
                self.helper.display_environment(1000, display, wayland, "")

    def test_child_environment_drops_untrusted_qt_python_and_loader_values(self):
        with patch.dict(os.environ, {"LD_PRELOAD": "/tmp/a", "QT_PLUGIN_PATH": "/tmp/b", "PYTHONPATH": "/tmp/c", "PATH": "/tmp"}):
            env = self.helper.base_environment()
        self.assertEqual(env["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")
        for name in ("LD_PRELOAD", "QT_PLUGIN_PATH", "PYTHONPATH"):
            self.assertNotIn(name, env)

    def test_launch_uses_standard_root_owned_config_and_branding_search(self):
        # -c /etc/calamares overrides the *data* directory too, preventing
        # Calamares from finding /usr/share/calamares/branding/desos.
        with patch.object(self.helper.sys, "argv", ["helper", "--display", "", "--wayland-display", "wayland-1", "--xauthority", ""]), \
             patch.dict(os.environ, {"PKEXEC_UID": "1000"}), \
             patch.object(self.helper.os, "geteuid", return_value=0), \
             patch.object(self.helper.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="desos")), \
             patch.object(self.helper.subprocess, "check_output", return_value="active\n"), \
             patch.object(self.helper, "validate_live_media"), \
             patch.object(self.helper, "display_environment", return_value={"QT_QPA_PLATFORM": "wayland"}), \
             patch.object(self.helper.os, "chdir"), \
             patch.object(self.helper.os, "umask"), \
             patch.object(self.helper.os, "execve") as execute:
            self.helper.main()
        self.assertEqual(execute.call_args.args[0:2], ("/usr/bin/calamares", ["calamares"]))


class ConfigurationTests(unittest.TestCase):
    def test_real_backend_modules_and_cleanup_run_before_initramfs_and_bootloader(self):
        settings = yaml.safe_load((BASE / "calamares/settings.conf").read_text())
        show = settings["sequence"][0]["show"]
        jobs = settings["sequence"][1]["exec"]
        self.assertEqual(show, ["welcome", "locale", "keyboard", "partition", "users", "summary"])
        self.assertTrue({"partition", "mount", "unpackfs", "users", "fstab", "initramfs", "bootloader", "umount"}.issubset(jobs))
        self.assertLess(jobs.index("users"), jobs.index("desoscleanup"))
        self.assertLess(jobs.index("desoscleanup"), jobs.index("initramfs"))
        self.assertLess(jobs.index("initramfs"), jobs.index("bootloader"))
        self.assertLess(jobs.index("bootloader"), jobs.index("shellprocess"))
        self.assertLess(jobs.index("shellprocess"), jobs.index("umount"))
        final = yaml.safe_load((BASE / "calamares/modules/shellprocess.conf").read_text())
        self.assertFalse(final["dontChroot"])
        self.assertIn("/usr/lib/desos/select-upstream-kernel", final["script"])
        self.assertTrue(settings["prompt-install"])
        self.assertFalse(settings["dont-chroot"])
        partition = yaml.safe_load((BASE / "calamares/modules/partition.conf").read_text())
        self.assertEqual(partition["initialPartitioningChoice"], "none")
        self.assertFalse(partition["enableLuksAutomatedPartitioning"])
        users = yaml.safe_load((BASE / "calamares/modules/users.conf").read_text())
        self.assertIn("desos", users["user"]["forbidden_names"])
        self.assertFalse(users["doAutologin"])
        self.assertFalse(users["setRootPassword"])
        unpack = yaml.safe_load((BASE / "calamares/modules/unpackfs.conf").read_text())
        self.assertEqual(unpack["unpack"][0]["source"], "/cdrom/casper/filesystem.squashfs")

    def test_final_boot_check_rejects_missing_or_empty_grub_and_selector_failure(self):
        final = yaml.safe_load((BASE / "calamares/modules/shellprocess.conf").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            grub = root / "grub.cfg"
            selector = root / "select-upstream-kernel"
            called = root / "selector-called"
            selector.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 0\n')
            selector.chmod(0o755)
            # Run the actual Calamares shell fragment, relocating only its two
            # target paths into the fixture; no host /boot or chroot is touched.
            command = final["script"].replace("/boot/grub/grub.cfg", str(grub)).replace("/usr/lib/desos/select-upstream-kernel", str(selector))
            missing = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse(called.exists())
            grub.write_text("")
            empty = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
            self.assertNotEqual(empty.returncode, 0)
            self.assertFalse(called.exists())
            grub.write_text("menuentry DesOS {}\n")
            valid = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
            self.assertEqual(valid.returncode, 0)
            self.assertTrue(called.exists())
            selector.write_text("#!/bin/sh\nexit 23\n")
            failed = subprocess.run(["/bin/sh", "-c", command], capture_output=True)
            self.assertEqual(failed.returncode, 23)

    def test_rootfs_copy_guard_refuses_host_root_and_destination_symlink(self):
        install = load("desos_install", BASE / "install_into_rootfs.py")
        with self.assertRaises(ValueError):
            install.validate_root(Path("/"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("ID=ubuntu\n")
            with self.assertRaises(ValueError):
                install.validate_root(root)
            (root / "etc/os-release").write_text("ID=desos\n")
            (root / "usr").symlink_to("/usr")
            with self.assertRaises(ValueError):
                install.safe_destination(root, "usr/bin/desos-installer")


if __name__ == "__main__":
    unittest.main()
