import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class KernelContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("kernel_tools", ROOT / "kernel/kernel_tools.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_release_rejects_eol_and_changed_source(self):
        pin = {"version": "6.18.62", "source_url": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.62.tar.xz", "signature_url": "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.62.tar.sign"}
        release = {"version": pin["version"], "moniker": "longterm", "iseol": False, "source": pin["source_url"], "pgp": pin["signature_url"]}
        self.mod.check_release({"releases": [release]}, pin)
        for field, value in (("iseol", True), ("source", "https://evil.invalid/linux.tar.xz"), ("moniker", "stable")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.mod.check_release({"releases": [dict(release, **{field: value})]}, pin)

    def test_signature_requires_exact_primary_fingerprint_and_no_bad_status(self):
        fingerprint = "647F28654894E3BD457199BE38DBBDC86092693E"
        status = f"[GNUPG:] VALIDSIG {'A' * 40} 2026-11-06 1793989063 0 4 0 1 8 00 {fingerprint}\n"
        self.assertEqual(self.mod.check_signature_status(status, [fingerprint]), fingerprint)
        for bad in (status.replace(fingerprint, 'B' * 40), status + "[GNUPG:] BADSIG 123 bad\n", "[GNUPG:] GOODSIG 123 somebody\n"):
            with self.assertRaises(ValueError):
                self.mod.check_signature_status(bad, [fingerprint])

    def test_config_guard_reports_missing_hardware_and_certificate_paths(self):
        required = self.mod.required_config()
        lines = [f"CONFIG_{key}={value}" for key, value in required.items()]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / '.config'
            path.write_text('\n'.join(lines) + '\n')
            self.mod.check_config(path)
            path.write_text(path.read_text().replace('CONFIG_DRM_I915=m', '# CONFIG_DRM_I915 is not set'))
            with self.assertRaisesRegex(ValueError, 'DRM_I915'):
                self.mod.check_config(path)
            path.write_text('\n'.join(lines) + '\nCONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"\n')
            with self.assertRaisesRegex(ValueError, 'SYSTEM_TRUSTED_KEYS'):
                self.mod.check_config(path)

    def test_source_filter_preserves_safe_link_spelling_and_refuses_escape(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            member = tarfile.TarInfo('linux-6.18.62/link')
            member.type = tarfile.SYMTYPE
            member.linkname = './target'
            result = self.mod.source_filter(member, tmp)
            self.assertEqual(result.linkname, './target')
            member.linkname = '/etc/passwd'
            with self.assertRaises(tarfile.FilterError):
                self.mod.source_filter(member, tmp)

    def test_package_payload_rejects_private_signing_material(self):
        import io
        import tarfile
        def payload(name, content):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode='w') as archive:
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            stream.seek(0)
            return stream
        self.mod.check_package_tar(payload('usr/src/linux-headers/certs/signing_key.x509', b'public certificate'))
        for name, content in [('usr/src/linux-headers/certs/signing_key.pem', b'key'),
                              ('usr/lib/hidden', b'-----BEGIN PRIVATE KEY-----\nprivate')]:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'private'):
                self.mod.check_package_tar(payload(name, content))

    def test_build_jobs_are_bounded_and_default_to_two(self):
        self.assertEqual(self.mod.build_jobs(None), 2)
        for value in ['1', '2', '3', '4']:
            self.assertEqual(self.mod.build_jobs(value), int(value))
        for value in ['', '0', '5', '-1', '04', ' 4', '4 ', '2;true', '999999']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mod.build_jobs(value)

    def test_invalid_build_jobs_refuse_before_guest_or_work_actions(self):
        environment = dict(os.environ, DESOS_BUILD_JOBS='5')
        result = subprocess.run(['bash', str(ROOT / 'kernel/build-guest.sh'), '--guest-build', '/must/not/be/created', '/missing/baseline'], env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DESOS_BUILD_JOBS', result.stderr)
        self.assertFalse(pathlib.Path('/must/not/be/created').exists())

    def test_provenance_records_the_explicit_build_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = pathlib.Path(temporary)
            build = work / 'build'
            (build / 'include/config').mkdir(parents=True)
            config = '\n'.join(f'CONFIG_{key}={value}' for key, value in self.mod.required_config().items()) + '\n'
            (build / '.config').write_text(config)
            (build / 'include/config/kernel.release').write_text('6.18.62-desos\n')
            (work / 'baseline.config').write_text(config)
            (work / 'source-provenance.json').write_text('{"version":"6.18.62"}')
            for name in ('signature-status.txt', 'signature-log.txt'):
                (work / name).write_text('public test fixture\n')
            packages = work / 'packages'
            packages.mkdir()
            result = subprocess.run(['python3', str(ROOT / 'kernel/kernel_tools.py'), 'provenance', '--work', str(work), '--baseline', str(work / 'baseline.config'), '--packages', str(packages), '--build-jobs', '4'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads((packages / 'kernel-provenance.json').read_text())
            self.assertEqual(evidence['build_jobs'], 4)

    def test_build_refuses_without_guest_opt_in(self):
        result = subprocess.run(['bash', str(ROOT / 'kernel/build-guest.sh')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--guest-build', result.stderr)

    def test_tree_verification_rejects_mutation_and_untracked_source(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            src = work / 'linux-6.18.62'
            src.mkdir()
            (src / 'Makefile').write_text('VERSION = 6\n')
            archive = work / 'linux.tar'
            with tarfile.open(archive, 'w') as tar:
                tar.add(src, arcname=src.name)
            self.mod.check_source_tree(archive, src)
            (src / 'extra.c').write_text('surprise')
            with self.assertRaisesRegex(ValueError, 'unexpected'):
                self.mod.check_source_tree(archive, src)
            (src / 'extra.c').unlink()
            (src / 'Makefile').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'modified'):
                self.mod.check_source_tree(archive, src)


if __name__ == '__main__':
    unittest.main()
