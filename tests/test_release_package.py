"""Build and inspect the real identity package without a host installation."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "image/release_package.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("desos_release_package", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_builder()
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / "artifacts"
        cls.result = cls.builder.build(cls.output)
        cls.deb = Path(cls.result["package_path"])
        cls.extract = Path(cls.temp.name) / "extracted"
        subprocess.run(["dpkg-deb", "--raw-extract", str(cls.deb), str(cls.extract)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_archive_has_expected_identity_ownership_and_no_essential_replacement(self):
        fields = subprocess.check_output(["dpkg-deb", "--field", str(self.deb)], text=True)
        self.assertIn("Package: desos-release\n", fields)
        # The content changed (VERSION_CODENAME) after 0.1.0~preview1 shipped, so
        # the package version moved on; the DesOS release strings did not.
        self.assertIn("Version: 0.1.0~preview1+1\n", fields)
        subprocess.run(["dpkg", "--compare-versions", self.builder.VERSION, "gt", "0.1.0~preview1"], check=True)
        ledger = json.loads((SOURCE.parent / "package-ledger.json").read_text())
        shipped = [entry for entry in ledger["packages"] if entry["package"] == "desos-release"]
        self.assertIn("0.1.0~preview1", [entry["version"] for entry in shipped])
        for entry in shipped:
            if entry["version"] == self.builder.VERSION:
                self.assertEqual(entry["sha256"], self.result["sha256"], "same version, different bytes")
        self.assertIn("Architecture: all\n", fields)
        self.assertIn("Depends: base-files, dpkg", fields)
        self.assertNotIn("Replaces:", fields)
        self.assertNotIn("Breaks:", fields)
        data = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(self.deb)])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            names = archive.getnames()
            self.assertNotIn("./etc/os-release", names)
            release = archive.extractfile("./usr/lib/os-release").read().decode()
            self.assertIn("ID=desos\n", release)
            self.assertIn('ID_LIKE="ubuntu debian"\n', release)
            self.assertIn("UBUNTU_CODENAME=resolute\n", release)
            self.assertIn('VERSION="0.1.0-preview1"\n', release)
            self.assertIn('PRETTY_NAME="DesOS 0.1 Preview"\n', release)
            # Third-party tooling derives apt suites from the codename fields.
            self.assertIn("VERSION_CODENAME=resolute\n", release)
            self.assertIn('VERSION_ID="0.1.0"\n', release)
            self.assertNotIn("preview1\nUBUNTU", release)
            lsb = archive.extractfile("./etc/lsb-release").read().decode()
            self.assertIn("DISTRIB_CODENAME=resolute\n", lsb)
            self.assertIn("DISTRIB_ID=DesOS\n", lsb)
            for line in release.splitlines():
                self.assertRegex(line, r'^[A-Z_]+=("[^"$`\\]*"|[A-Za-z0-9._-]+)$')
            for entry in archive.getmembers():
                self.assertEqual((entry.uid, entry.gid), (0, 0))
                if entry.isfile():
                    self.assertEqual(entry.mode, 0o644)

    def test_public_record_matches_archive_and_rebuild_is_identical(self):
        record = json.loads(Path(self.result["record_path"]).read_text())
        self.assertEqual(record["package"], "desos-release")
        self.assertEqual(record["sha256"], hashlib.sha256(self.deb.read_bytes()).hexdigest())
        other = Path(self.temp.name) / "repeat"
        second = self.builder.build(other)
        self.assertEqual(self.deb.read_bytes(), Path(second["package_path"]).read_bytes())
        self.assertEqual(Path(self.result["record_path"]).read_bytes(), Path(second["record_path"]).read_bytes())
        self.assertNotIn(str(Path.home()), json.dumps(record))

    def test_refuses_unsafe_or_symlinked_output_and_conflicting_artifacts(self):
        with self.assertRaises(ValueError):
            self.builder.build(Path("/"))
        with self.assertRaises(ValueError):
            self.builder.build(Path("relative-output"))
        with self.assertRaises(ValueError):
            self.builder.build(Path("/usr/bin"))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "linked").symlink_to(self.output, target_is_directory=True)
            with self.assertRaises(ValueError):
                self.builder.build(base / "linked")
            bad = base / "conflict"
            bad.mkdir()
            package = bad / self.deb.name
            package.write_bytes(b"owner artifact")
            with self.assertRaises(ValueError):
                self.builder.build(bad)
            self.assertEqual(package.read_bytes(), b"owner artifact")

    def fixture(self, root):
        admin = root / "var/lib/dpkg"
        (admin / "info").mkdir(parents=True)
        (admin / "status").write_text("Package: base-files\nStatus: install ok installed\nPriority: required\nSection: admin\nInstalled-Size: 1\nMaintainer: Ubuntu Developers\nArchitecture: amd64\nVersion: 14.0\nDescription: fixture base files\n\n")
        (admin / "info/base-files.list").write_text("/usr/lib/os-release\n/etc/lsb-release\n")
        (root / "usr/lib").mkdir(parents=True)
        (root / "etc").mkdir()
        (root / "usr/lib/os-release").write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        (root / "etc/lsb-release").write_text('DISTRIB_ID=Ubuntu\nDISTRIB_RELEASE=26.04\n')
        (root / "etc/os-release").symlink_to("../usr/lib/os-release")
        return {**os.environ, "DPKG_ROOT": str(root), "DPKG_ADMINDIR": str(admin), "LC_ALL": "C.UTF-8"}

    def script(self, name, env, *args, check=True):
        return subprocess.run(["/bin/sh", str(self.extract / "DEBIAN" / name), *args], env=env, check=check, capture_output=True, text=True)

    def divert(self, env, *args):
        return subprocess.run(["/usr/bin/dpkg-divert", *args], env=env, check=True, capture_output=True, text=True)

    def test_real_diversions_preserve_ubuntu_upgrade_and_restore_on_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            self.script("preinst", env, "install")
            for filename in ("usr/lib/os-release", "etc/lsb-release"):
                self.assertFalse((root / filename).exists())
                self.assertTrue((root / f"{filename}.ubuntu").is_file())
                self.assertEqual(self.divert(env, "--listpackage", "/" + filename).stdout.strip(), "desos-release")
                (root / filename).write_bytes((self.extract / filename).read_bytes())
            self.script("preinst", env, "upgrade", "0.1.0~preview1")
            # A later base-files package is redirected to this vendor backup.
            true_name = self.divert(env, "--truename", "/usr/lib/os-release").stdout.strip()
            (root / true_name.lstrip("/")).write_text('ID=ubuntu\nVERSION_ID="26.04.1"\n')
            self.assertIn("ID=desos", (root / "etc/os-release").read_text())
            # dpkg removes non-conffile payloads before the postrm remove step.
            (root / "usr/lib/os-release").unlink()
            (root / "etc/lsb-release").unlink()
            self.script("postrm", env, "remove")
            self.assertIn('VERSION_ID="26.04.1"', (root / "etc/os-release").read_text())
            self.assertIn("DISTRIB_ID=Ubuntu", (root / "etc/lsb-release").read_text())
            self.assertEqual(self.divert(env, "--list").stdout, "")
            self.script("postrm", env, "purge")

    def test_preinst_refuses_another_diversion_without_partial_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            self.divert(env, "--package", "other-release", "--add", "--rename", "--divert", "/etc/lsb-release.other", "/etc/lsb-release")
            result = self.script("preinst", env, "install", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("other-release", result.stderr)
            self.assertIn("ID=ubuntu", (root / "usr/lib/os-release").read_text())
            self.assertFalse((root / "usr/lib/os-release.ubuntu").exists())

    def test_preinst_refuses_different_vendor_and_unclaimed_backup(self):
        for conflict in ("vendor", "backup"):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = self.fixture(root)
                if conflict == "vendor":
                    (root / "usr/lib/os-release").write_text("ID=otherlinux\n")
                else:
                    (root / "etc/lsb-release.ubuntu").write_text("owner backup")
                result = self.script("preinst", env, "install", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.divert(env, "--list").stdout, "")
                self.assertTrue((root / "usr/lib/os-release").exists())

    def test_postrm_does_not_overwrite_modified_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            self.script("preinst", env, "install")
            (root / "usr/lib/os-release").write_text("ID=owner-choice\n")
            result = self.script("postrm", env, "remove", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "usr/lib/os-release").read_text(), "ID=owner-choice\n")
            self.assertTrue((root / "usr/lib/os-release.ubuntu").exists())

    def test_missing_optional_lsb_file_and_failed_fresh_unpack_restore_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            (root / "etc/lsb-release").unlink()
            self.script("preinst", env, "install")
            for filename in ("usr/lib/os-release", "etc/lsb-release"):
                (root / filename).write_bytes((self.extract / filename).read_bytes())
            self.script("postrm", env, "abort-install")
            self.assertIn("ID=ubuntu", (root / "etc/os-release").read_text())
            self.assertFalse((root / "etc/lsb-release").exists())
            self.assertEqual(self.divert(env, "--list").stdout, "")

    @unittest.skipIf(os.geteuid() == 0, "Permission-based rollback fixture requires an unprivileged user")
    def test_preinst_rolls_back_first_diversion_if_second_rename_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.fixture(root)
            (root / "etc").chmod(0o500)
            try:
                result = self.script("preinst", env, "install", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ID=ubuntu", (root / "usr/lib/os-release").read_text())
                self.assertFalse((root / "usr/lib/os-release.ubuntu").exists())
                self.assertEqual(self.divert(env, "--list").stdout, "")
            finally:
                (root / "etc").chmod(0o700)


if __name__ == "__main__":
    unittest.main()
