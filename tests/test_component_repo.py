import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

PATH = Path(__file__).parents[1] / 'image/component_repo.py'
repo = None
if PATH.exists():
    spec = importlib.util.spec_from_file_location('component_repo', PATH)
    repo = importlib.util.module_from_spec(spec); spec.loader.exec_module(repo)

FINGERPRINT = 'A' * 40

class ComponentRepoTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(repo, 'component repository builder must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root/'dedicated-gpg'; self.home.mkdir(mode=0o700)
        self.commands = []

    def tearDown(self):
        self.tmp.cleanup()

    def deb(self, name='desos-desktop', version='0.1', arch='amd64'):
        if not shutil.which('dpkg-deb'):
            self.skipTest('dpkg-deb not installed')
        tree=self.root/(name+'-tree'); (tree/'DEBIAN').mkdir(parents=True)
        (tree/'DEBIAN/control').write_text(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: DesOS Test <test@example.invalid>\nDescription: Temporary repository contract fixture\n')
        (tree/'usr/share'/name).mkdir(parents=True)
        (tree/'usr/share'/name/'fixture').write_text('temporary test package\n')
        output=self.root/(name+'.deb')
        subprocess.run(['dpkg-deb','--build','--root-owner-group',str(tree),str(output)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return output

    def fake_gpg(self, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(command[:4],['gpg','--no-options','--homedir',str(self.home)])
        self.assertIn('--batch',command); self.assertIn('--no-tty',command)
        if '--list-secret-keys' in command:
            return 'sec:-:3072:1:TEST:0:0:::::sc:\nfpr:::::::::'+FINGERPRINT+':\n'
        if '--export' in command:
            return '-----BEGIN PGP PUBLIC KEY BLOCK-----\nTEST ONLY\n-----END PGP PUBLIC KEY BLOCK-----\n'
        if '--verify' in command:
            return '[GNUPG:] VALIDSIG '+FINGERPRINT+' 2026-11-10 0 0 4 0 1 10 00 '+FINGERPRINT+'\n'
        if '--output' in command:
            self.assertIn(FINGERPRINT+'!',command)
            dest=Path(command[command.index('--output')+1]); dest.write_text('test-only fake signature\n')
            return '[GNUPG:] SIG_CREATED D 1 10 00 0 '+FINGERPRINT+'\n'
        self.fail('unexpected GPG operation: '+str(command))

    def build(self, inputs, output=None):
        output=output or self.root/'repository'
        real_run=repo.run
        def runner(command, **kwargs):
            if command[0]=='gpg': return self.fake_gpg(command,**kwargs)
            return real_run(command,**kwargs)
        with patch.object(repo,'run',side_effect=runner):
            return repo.build_repository(inputs,output,self.home,FINGERPRINT,epoch=1794268800)

    def test_real_debs_form_apt_repository_and_signed_provenance_contract(self):
        if not (shutil.which('apt-ftparchive') or shutil.which('dpkg-scanpackages')):
            self.skipTest('APT metadata tooling not installed')
        desktop=self.deb(); kernel=self.deb('linux-image-desos','6.18.62','amd64')
        release=self.deb('desos-release','0.1','all')
        result=self.build([desktop,kernel,release])
        output=self.root/'repository'
        manifest=json.loads((output/'manifest.json').read_text())
        self.assertEqual(result['status'],'built')
        self.assertEqual(manifest['signing_fingerprint'],FINGERPRINT)
        self.assertEqual(len(manifest['packages']),3)
        packages=(output/'dists/desos/main/binary-amd64/Packages').read_text()
        for name in ('desos-desktop','linux-image-desos','desos-release'):
            self.assertIn('Package: '+name+'\n',packages)
        self.assertIn('Filename: pool/main/',packages)
        import gzip
        self.assertEqual(gzip.decompress((output/'dists/desos/main/binary-amd64/Packages.gz').read_bytes()),packages.encode())
        release_text=(output/'dists/desos/Release').read_text()
        self.assertIn('Origin: DesOS\n',release_text)
        self.assertIn(hashlib.sha256(packages.encode()).hexdigest(),release_text)
        self.assertTrue((output/'dists/desos/InRelease').is_file())
        self.assertTrue((output/'dists/desos/Release.gpg').is_file())
        self.assertTrue((output/'desos-archive-keyring.asc').is_file())
        self.assertTrue((output/'manifest.json.asc').is_file())
        self.assertFalse((output/'.incomplete').exists())
        for package in manifest['packages']:
            self.assertEqual(repo.sha256(output/package['pool_path']),package['sha256'])
        self.assertEqual(sum('--verify' in command for command in self.commands),3)

    def test_refuses_existing_output_without_changes(self):
        package=self.deb(); output=self.root/'existing'; output.mkdir(); sentinel=output/'keep'; sentinel.write_text('unchanged')
        with self.assertRaises(repo.RepoError): self.build([package],output)
        self.assertEqual(sentinel.read_text(),'unchanged')
        self.assertEqual(list(output.iterdir()),[sentinel])

    def test_rejects_symlink_empty_inputs_unsafe_key_home_and_foreign_arch(self):
        package=self.deb(); link=self.root/'link.deb'; link.symlink_to(package)
        for inputs in [[],[link],[self.root/'missing.deb']]:
            with self.subTest(inputs=inputs),self.assertRaises(repo.RepoError): self.build(inputs)
        self.home.chmod(0o755)
        with self.assertRaises(repo.RepoError): self.build([package])
        self.home.chmod(0o700)
        with self.assertRaises(repo.RepoError): repo.build_repository([package],self.root/'badfp',self.home,'short')
        foreign=self.deb('foreign-package','1.0','arm64')
        with self.assertRaises(repo.RepoError): self.build([foreign],self.root/'foreign')

    def test_signing_failure_leaves_incomplete_repository_and_nonzero_json(self):
        package=self.deb(); output=self.root/'failed'
        def reject(*args,**kwargs): raise repo.RepoError('test signer refused')
        with patch.object(repo,'gpg_run',side_effect=reject),redirect_stdout(io.StringIO()) as captured:
            code=repo.main(['--deb-input',str(package),'--output',str(output),'--gpg-home',str(self.home),'--key-fingerprint',FINGERPRINT])
        self.assertEqual(code,1)
        self.assertEqual(json.loads(captured.getvalue())['status'],'failed')
        self.assertTrue((output/'.incomplete').exists())
        self.assertFalse((output/'manifest.json').exists())

    def test_directory_inputs_are_explicit_and_duplicate_packages_deduplicated(self):
        package=self.deb()
        result=self.build([self.root,package])
        self.assertEqual(result['package_count'],1)


    def test_default_gpg_home_is_refused_even_when_explicit(self):
        default=self.root/'.gnupg'; default.mkdir(mode=0o700)
        with patch.object(repo.Path,'home',return_value=self.root):
            with self.assertRaises(repo.RepoError): repo.validate_home(default,FINGERPRINT)

    def test_wrong_signature_identity_and_conflicting_package_bytes_fail(self):
        with patch.object(repo,'gpg_run',return_value='[GNUPG:] VALIDSIG '+'B'*40+' more fields\n'):
            with self.assertRaises(repo.RepoError): repo.verify_signature(self.home,FINGERPRINT,self.root/'sig')
        package=self.deb(); copied=self.root/'first.deb'; shutil.copyfile(package,copied)
        (self.root/'desos-desktop-tree/usr/share/desos-desktop/fixture').write_text('different package bytes\n')
        subprocess.run(['dpkg-deb','--build','--root-owner-group',str(self.root/'desos-desktop-tree'),str(package)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        with self.assertRaises(repo.RepoError): self.build([copied,package])
        self.assertTrue((self.root/'repository/.incomplete').exists())

    def test_dpkg_scanpackages_fallback_builds_real_indexes(self):
        if not shutil.which('dpkg-scanpackages'): self.skipTest('dpkg-scanpackages unavailable')
        package=self.deb(); actual_which=shutil.which
        def which(name): return None if name=='apt-ftparchive' else actual_which(name)
        with patch.object(repo.shutil,'which',side_effect=which): self.build([package])
        manifest=json.loads((self.root/'repository/manifest.json').read_text())
        self.assertEqual(manifest['metadata_tool'],'dpkg-scanpackages')
        packages=(self.root/'repository/dists/desos/main/binary-amd64/Packages').read_text()
        self.assertIn('Package: desos-desktop\n',packages)

if __name__=='__main__': unittest.main()
