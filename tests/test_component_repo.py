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

    def build(self, inputs, output=None, **options):
        output=output or self.root/'repository'
        real_run=repo.run
        def runner(command, **kwargs):
            if command[0]=='gpg': return self.fake_gpg(command,**kwargs)
            return real_run(command,**kwargs)
        with patch.object(repo,'run',side_effect=runner):
            return repo.build_repository(inputs,output,self.home,FINGERPRINT,epoch=1794268800,**options)

    def ledger(self, entries=()):
        path=self.root/'ledger.json'
        path.write_text(json.dumps({'schema':'desos-package-ledger-v1','packages':list(entries)}))
        return path

    def rebuilt(self, name='desos-desktop'):
        """The same package name and version again, with other content."""
        first=self.deb(name); kept=self.root/'first.deb'; shutil.copyfile(first,kept)
        (self.root/(name+'-tree')/'usr/share'/name/'fixture').write_text('different package bytes\n')
        subprocess.run(['dpkg-deb','--build','--root-owner-group',str(self.root/(name+'-tree')),str(first)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return kept,first

    def test_manifest_carries_a_signed_list_of_every_packaged_file(self):
        package=self.deb()
        tree=self.root/'desos-desktop-tree'
        (tree/'usr/bin').mkdir(); (tree/'usr/bin/tool').write_text('#!/bin/sh\n'); (tree/'usr/bin/tool').chmod(0o755)
        os.symlink('tool',tree/'usr/bin/alias'); os.link(tree/'usr/bin/tool',tree/'usr/bin/hard')
        subprocess.run(['dpkg-deb','--build','--root-owner-group',str(tree),str(package)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.build([package]); output=self.root/'repository'
        manifest=json.loads((output/'manifest.json').read_text())
        listing=manifest['packages'][0]['file_list']
        self.assertEqual(listing['path'],'file-lists/desos-desktop_0.1_amd64.sha256')
        text=(output/listing['path']).read_text()
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),listing['sha256'])
        # Covered twice: by the package entry and as a repository artifact.
        self.assertEqual(manifest['artifacts'][listing['path']]['sha256'],listing['sha256'])
        tool=hashlib.sha256(b'#!/bin/sh\n').hexdigest()
        self.assertEqual(text.splitlines(),[
            tool+'  usr/bin/hard', tool+'  usr/bin/tool',
            hashlib.sha256(b'temporary test package\n').hexdigest()+'  usr/share/desos-desktop/fixture'])
        self.assertEqual(listing['entries'],3)
        # sha256sum itself accepts the format against an unpacked tree.
        unpacked=self.root/'unpacked'
        subprocess.run(['dpkg-deb','-x',str(package),str(unpacked)],check=True)
        subprocess.run(['sha256sum','--check','--strict','--quiet',str(output/listing['path'])],cwd=unpacked,check=True)

    def test_build_evidence_is_copied_and_covered_by_a_signature_made_here(self):
        package=self.deb(); source=self.root/'evidence'; source.mkdir()
        (source/'kernel-provenance.json').write_text('{"kernel_release": "x"}\n'); (source/'config-6.18.62-desos').write_text('CONFIG=y\n')
        self.build([package],evidence=sorted(source.iterdir())); output=self.root/'repository'
        directory=output/'build-evidence'
        self.assertEqual(sorted(p.name for p in directory.iterdir()),['SHA256SUMS','SHA256SUMS.asc','config-6.18.62-desos','kernel-provenance.json'])
        sums=(directory/'SHA256SUMS').read_text()
        self.assertEqual(sums,''.join('%s  %s\n'%(hashlib.sha256((source/name).read_bytes()).hexdigest(),name)
                                      for name in ('config-6.18.62-desos','kernel-provenance.json')))
        manifest=json.loads((output/'manifest.json').read_text())
        self.assertEqual(manifest['build_evidence']['sha256sums_sha256'],hashlib.sha256(sums.encode()).hexdigest())
        self.assertEqual(sorted(manifest['build_evidence']['files']),['config-6.18.62-desos','kernel-provenance.json'])
        self.assertIn('build-evidence/SHA256SUMS.asc',manifest['artifacts'])
        signing=[c for c in self.commands if '--detach-sign' in c and c[-1].endswith('SHA256SUMS')]
        self.assertEqual(len(signing),1); self.assertIn(FINGERPRINT+'!',signing[0])
        # Without evidence the manifest says nothing about it.
        self.build([package],self.root/'plain')
        self.assertNotIn('build_evidence',json.loads((self.root/'plain/manifest.json').read_text()))
        self.assertFalse((self.root/'plain/build-evidence').exists())

    def test_unsuitable_evidence_is_refused_before_anything_is_written(self):
        package=self.deb(); good=self.root/'note.txt'; good.write_text('x')
        link=self.root/'link.txt'; link.symlink_to(good)
        other=self.root/'other'; other.mkdir(); (other/'note.txt').write_text('y')
        (self.root/'SHA256SUMS').write_text('x'); (self.root/'bad name').write_text('x')
        for evidence in ([link],[self.root/'absent'],[self.root],[good,other/'note.txt'],[self.root/'SHA256SUMS'],[self.root/'bad name']):
            with self.subTest(evidence=evidence),self.assertRaises(repo.RepoError): self.build([package],evidence=evidence)
        self.assertFalse((self.root/'repository').exists())

    def test_ledger_refuses_a_recorded_version_with_other_bytes(self):
        kept,changed=self.rebuilt()
        ledger=self.ledger()
        result=self.build([kept],ledger=ledger)
        self.assertEqual(result['ledger'],{'path':str(ledger),'new_identities':['desos-desktop 0.1 amd64'],'recorded':True})
        entries=json.loads(ledger.read_text())['packages']
        self.assertEqual([(e['package'],e['version'],e['architecture'],e['sha256']) for e in entries],
                         [('desos-desktop','0.1','amd64',repo.sha256(kept))])
        self.assertEqual(entries[0]['recorded'],'repository build 2026-11-10')
        before=ledger.read_bytes()
        # The same bytes again are fine and change nothing.
        self.assertEqual(self.build([kept],self.root/'again',ledger=ledger)['ledger']['new_identities'],[])
        self.assertEqual(ledger.read_bytes(),before)
        with self.assertRaises(repo.RepoError) as refused: self.build([changed],self.root/'reused',ledger=ledger)
        self.assertIn('never be reused for different bytes',str(refused.exception))
        self.assertTrue((self.root/'reused/.incomplete').exists())
        self.assertFalse((self.root/'reused/manifest.json').exists())
        self.assertEqual(ledger.read_bytes(),before)

    def test_ledger_can_be_checked_without_recording_and_must_be_well_formed(self):
        package=self.deb(); ledger=self.ledger(); before=ledger.read_bytes()
        result=self.build([package],ledger=ledger,record_ledger_entries=False)
        self.assertEqual((result['ledger']['new_identities'],result['ledger']['recorded']),(['desos-desktop 0.1 amd64'],False))
        self.assertEqual(ledger.read_bytes(),before)
        entry={'package':'p','version':'1','architecture':'all','sha256':'a'*64}
        for index,text in enumerate(['not json','{"schema": "other", "packages": []}',json.dumps({'schema':'desos-package-ledger-v1','packages':[entry,entry]}),
                     json.dumps({'schema':'desos-package-ledger-v1','packages':[dict(entry,sha256='short')]})]):
            ledger.write_text(text)
            with self.subTest(text=text),self.assertRaises(repo.RepoError): self.build([package],self.root/('bad-%d'%index),ledger=ledger)
            self.assertFalse((self.root/('bad-%d'%index)).exists())
        with self.assertRaises(repo.RepoError): self.build([package],self.root/'missing',ledger=self.root/'no-ledger.json')

    def test_shipped_ledger_holds_the_first_image_and_the_command_line_uses_it(self):
        known=repo.load_ledger(repo.LEDGER)
        self.assertEqual(repo.LEDGER,PATH.parent/'package-ledger.json')
        # What went out in the 0.1 image is on record, including the identity package whose content later changed.
        self.assertIn(('desos-release','0.1.0~preview1','all'),known)
        self.assertIn(('des-ui','0.1.1','amd64'),known)
        self.assertIn(('linux-image-6.18.62-desos','6.18.62-1desos1','amd64'),known)
        pin=json.loads((PATH.parent/'desktop-pin.json').read_text())
        for item in pin['packages']:
            name,version,arch=item['file'][:-4].split('_')
            self.assertEqual(known[(name,version,arch)],item['sha256'])
        # A different des-ui 0.1.1 (the one in the public des-ui apt repository) cannot be assembled into a DesOS repository.
        other=self.deb('des-ui','0.1.1','amd64'); before=repo.LEDGER.read_bytes()
        real_run=repo.run
        def runner(command, **kwargs):
            return self.fake_gpg(command,**kwargs) if command[0]=='gpg' else real_run(command,**kwargs)
        with patch.object(repo,'run',side_effect=runner),redirect_stdout(io.StringIO()) as captured:
            code=repo.main(['--deb-input',str(other),'--output',str(self.root/'cli'),'--gpg-home',str(self.home),'--key-fingerprint',FINGERPRINT])
        self.assertEqual(code,1)
        self.assertIn('des-ui 0.1.1',json.loads(captured.getvalue())['error'])
        self.assertEqual(repo.LEDGER.read_bytes(),before,'a test must never write the real ledger')

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
