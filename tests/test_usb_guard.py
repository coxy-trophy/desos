import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout
import io

SPEC = importlib.util.spec_from_file_location('usb_guard', Path(__file__).parents[1] / 'tools/usb_guard.py')
if SPEC is not None and SPEC.loader is not None and Path(SPEC.origin).exists():
    usb = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(usb)
else:
    usb = None

AUTH = {'usb_erase_approved': True, 'approved_answer': 'Erase this USB and write DesOS',
        'device_at_approval': '/dev/sdb', 'model': 'ProductCode', 'serial': '6912801040691779938',
        'size_bytes': 134217728000, 'transport': 'usb', 'removable': True,
        'internal_disk_prohibited': '/dev/sda', 'prohibited_internal_serial': 'ES72N072110902A1X',
        'require_identity_revalidation_before_write': True, 'require_tested_iso_before_write': True}
TARGET = '/dev/disk/by-id/usb-VendorCo_ProductCode_6912801040691779938-0:0'

def inventory():
    return {'blockdevices': [
        {'name': '/dev/sda', 'path': '/dev/sda', 'type': 'disk', 'size': 512000000000,
         'serial': 'ES72N072110902A1X', 'tran': 'sata', 'rm': False, 'ro': False,
         'children': [{'name': '/dev/sda1', 'path': '/dev/sda1', 'type': 'part', 'mountpoints': ['/']}]},
        {'name': '/dev/sdb', 'path': '/dev/sdb', 'type': 'disk', 'size': 134217728000,
         'model': 'ProductCode', 'serial': '6912801040691779938', 'tran': 'usb',
         'rm': True, 'ro': False, 'maj:min': '8:16', 'mountpoints': [None],
         'children': [{'name': '/dev/sdb1', 'path': '/dev/sdb1', 'type': 'part', 'maj:min': '8:17', 'mountpoints': [None]}]}]}

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(usb, 'USB guard module must exist')

    def validate(self, inv=None, auth=None, target=TARGET, resolved='/dev/sdb', mountinfo='', swaps='Filename Type Size Used Priority\n'):
        return usb.validate_device(auth or AUTH, inv or inventory(), target, resolved, (8, 16), mountinfo, swaps)

    def test_only_explicit_approved_unmounted_usb_is_accepted(self):
        self.assertEqual(self.validate()['serial'], AUTH['serial'])

    def test_identity_authorization_and_target_mismatches_fail_closed(self):
        for field, bad in [('serial', 'other'), ('size', 1), ('tran', 'sata'), ('rm', False), ('ro', True), ('model', 'other'), ('type', 'part')]:
            inv = inventory(); inv['blockdevices'][1][field] = bad
            with self.subTest(field=field), self.assertRaises(usb.GuardError): self.validate(inv=inv)
        for field, bad in [('usb_erase_approved', False), ('size_bytes', 1), ('require_tested_iso_before_write', False), ('approved_answer', 'yes')]:
            auth = dict(AUTH); auth[field] = bad
            with self.subTest(field=field), self.assertRaises(usb.GuardError): self.validate(auth=auth)
        for target, resolved in [('/dev/sdb','/dev/sdb'),(TARGET,'/dev/sda'),(TARGET,'/dev/sdb1')]:
            with self.subTest(target=target,resolved=resolved), self.assertRaises(usb.GuardError): self.validate(target=target,resolved=resolved)

    def test_mounts_swap_system_and_duplicate_identity_are_rejected(self):
        for mount in ['/media/USB60GB', '/', '/home', '[SWAP]']:
            inv = inventory(); inv['blockdevices'][1]['children'][0]['mountpoints'] = [mount]
            with self.subTest(mount=mount), self.assertRaises(usb.GuardError): self.validate(inv=inv)
        with self.assertRaises(usb.GuardError): self.validate(mountinfo='1 2 8:17 / /media/USB rw - exfat /dev/sdb1 rw\n')
        with self.assertRaises(usb.GuardError): self.validate(swaps='Filename Type Size Used Priority\n/dev/sdb1 partition 100 0 -2\n')
        inv = inventory(); inv['blockdevices'].append(copy.deepcopy(inv['blockdevices'][1]))
        with self.assertRaises(usb.GuardError): self.validate(inv=inv)

    def test_nonblock_regular_file_rejected_by_real_preflight(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(usb.GuardError): usb.block_identity(f.name)

    def test_source_receipt_requires_matching_actual_bytes_and_all_tests(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'test.iso'; source.write_bytes(b'x'*4096)
            digest = usb.sha256_file(source)
            receipt = {'iso_path': str(source), 'sha256': digest, 'size_bytes': 4096,
                       'tests': {name: True for name in usb.REQUIRED_TESTS}}
            rp = Path(folder)/'tested.json'; rp.write_text(json.dumps(receipt))
            self.assertEqual(usb.validate_source(source,rp,134217728000)['sha256'],digest)
            for mutate in ['hash', 'test', 'size', 'path']:
                bad = copy.deepcopy(receipt)
                if mutate == 'hash': bad['sha256']='0'*64
                elif mutate == 'test': bad['tests']['installed_boot']=False
                elif mutate == 'size': bad['size_bytes']=2048
                else: bad['iso_path']='/another.iso'
                rp.write_text(json.dumps(bad))
                with self.subTest(mutate=mutate), self.assertRaises(usb.GuardError): usb.validate_source(source,rp,134217728000)
            rp.write_text(json.dumps(receipt))
            with self.assertRaises(usb.GuardError): usb.validate_source(source,rp,2048)
            source.write_bytes(b'x'*4097)
            with self.assertRaises(usb.GuardError): usb.validate_source(source,rp,134217728000)

    def test_docker_command_is_bounded_and_pinned(self):
        cmd = usb.docker_command('ubuntu@sha256:'+'a'*64, TARGET, '/tmp/test.iso', '/tmp/usb_helper.sh',
                                 {'major_minor':'8:16','path':'/dev/sdb'}, {'size_bytes':4096,'sha256':'b'*64})
        self.assertIn('--network=none',cmd); self.assertIn('--cap-drop=ALL',cmd)
        self.assertIn('--read-only',cmd); self.assertIn('--pull=never',cmd)
        self.assertIn('--device=/dev/sdb:/dev/desos-target:rw',cmd)
        self.assertEqual(sum(x.startswith('--device=') for x in cmd),1)
        self.assertTrue(all('privileged' not in x and 'pid=host' not in x and 'SYS_ADMIN' not in x for x in cmd))
        self.assertEqual(sum(x.startswith('--mount=') for x in cmd),2)
        for image in ['ubuntu:26.04','evil/ubuntu@sha256:'+'a'*64]:
            with self.assertRaises(usb.GuardError): usb.docker_command(image,TARGET,'/tmp/x','/tmp/y',{'major_minor':'8:16','path':'/dev/sdb'},{'size_bytes':4096,'sha256':'b'*64})

    def test_helper_result_requires_matching_full_readback_and_flush(self):
        source = {'size_bytes':4096,'sha256':'a'*64}
        good = {'status':'verified','source_sha256':'a'*64,'readback_sha256':'a'*64,'bytes_verified':4096,'write_fsync':True,'direct_read':True,'source_unchanged':True}
        usb.validate_result(good,source)
        for field,bad in [('status','failed'),('readback_sha256','b'*64),('bytes_verified',2048),('write_fsync',False),('direct_read',False),('source_unchanged',False)]:
            result = dict(good); result[field]=bad
            with self.subTest(field=field), self.assertRaises(usb.GuardError): usb.validate_result(result,source)

    def test_helper_pins_gnu_dd_before_any_device_operation(self):
        # Ubuntu 26.04's default dd is uutils. Keep the destructive path tied
        # to the explicitly supplied GNU implementation and fail before opening
        # the target if that implementation is absent or unexpectedly replaced.
        helper = (Path(SPEC.origin).with_name('usb_helper.sh')).read_text()
        version = helper.index('/usr/bin/gnudd --version')
        self.assertLess(version, helper.index('[[ -b /dev/desos-target'))
        self.assertIn('Free Software Foundation', helper[:helper.index('stage=identity')])
        write = next(line for line in helper.splitlines()
                     if line.startswith('/usr/bin/gnudd if=/source.iso of=/dev/desos-target '))
        self.assertIn('oflag=direct', write.split())
        self.assertIn('conv=fsync', write.split())
        self.assertIn('/usr/bin/gnudd if=/dev/desos-target', helper)
        self.assertNotRegex(helper, r'(?m)^dd\s')
        self.assertNotRegex(helper, r'\$\(dd\s')


    def test_failure_after_write_is_audited_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            source=folder/'source.iso'; source.write_bytes(b'z'*4096)
            tested=folder/'tested.json'
            tested.write_text(json.dumps({'iso_path':str(source),'sha256':usb.sha256_file(source),
                'size_bytes':4096,'tests':{name:True for name in usb.REQUIRED_TESTS}}))
            auth=folder/'auth.json'; auth.write_text(json.dumps(AUTH))
            receipt=folder/'write.json'
            calls=[]
            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[:3]==['docker','image','inspect']:
                    return SimpleNamespace(returncode=0,stdout='[{"Id":"sha256:pinned"}]',stderr='')
                return SimpleNamespace(returncode=1,stdout='{"status":"failed","stage":"direct_readback"}',stderr='read error')
            argv=['flash','--authorization',str(auth),'--target',TARGET,'--iso',str(source),
                  '--tested-receipt',str(tested),'--image','ubuntu@sha256:'+'a'*64,'--receipt',str(receipt)]
            with patch.object(usb,'preflight',return_value={'path':'/dev/sdb','major_minor':'8:16','size_bytes':134217728000}), patch.object(usb.subprocess,'run',side_effect=fake_run):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(usb.main(argv),1)
            audit=json.loads(receipt.read_text())
            self.assertTrue(audit['write_attempted'])
            self.assertEqual(audit['status'],'failed')
            self.assertEqual(audit['helper_result']['stage'],'direct_readback')
            self.assertEqual(audit['container_exit_code'],1)
            self.assertEqual(len(calls),2)

    def test_device_change_before_launch_never_runs_write(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder); source=folder/'source.iso'; source.write_bytes(b'z'*4096)
            tested=folder/'tested.json'; tested.write_text(json.dumps({'iso_path':str(source),
                'sha256':usb.sha256_file(source),'size_bytes':4096,
                'tests':{name:True for name in usb.REQUIRED_TESTS}}))
            auth=folder/'auth.json'; auth.write_text(json.dumps(AUTH)); receipt=folder/'write.json'
            states=[{'path':'/dev/sdb','major_minor':'8:16','size_bytes':134217728000},
                    {'path':'/dev/sdc','major_minor':'8:32','size_bytes':134217728000}]
            argv=['flash','--authorization',str(auth),'--target',TARGET,'--iso',str(source),
                  '--tested-receipt',str(tested),'--image','ubuntu@sha256:'+'a'*64,'--receipt',str(receipt)]
            with patch.object(usb,'preflight',side_effect=states), patch.object(usb.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='[{"Id":"pinned"}]',stderr='')) as runner:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(usb.main(argv),1)
                self.assertEqual(runner.call_count,1)
            self.assertFalse(json.loads(receipt.read_text())['write_attempted'])


    def test_success_receipt_records_verified_span_and_failure_never_overwrites_it(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder); source=folder/'source.iso'; source.write_bytes(b'z'*4096)
            digest=usb.sha256_file(source)
            tested=folder/'tested.json'; tested.write_text(json.dumps({'iso_path':str(source),
                'sha256':digest,'size_bytes':4096,'tests':{name:True for name in usb.REQUIRED_TESTS}}))
            auth=folder/'auth.json'; auth.write_text(json.dumps(AUTH)); receipt=folder/'write.json'
            result={'status':'verified','source_sha256':digest,'readback_sha256':digest,
                    'bytes_verified':4096,'write_fsync':True,'direct_read':True,'source_unchanged':True}
            calls=[]
            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                output='[{"Id":"sha256:pinned"}]' if cmd[:3]==['docker','image','inspect'] else json.dumps(result)
                return SimpleNamespace(returncode=0,stdout=output,stderr='')
            argv=['flash','--authorization',str(auth),'--target',TARGET,'--iso',str(source),
                  '--tested-receipt',str(tested),'--image','ubuntu@sha256:'+'a'*64,'--receipt',str(receipt)]
            with patch.object(usb,'preflight',return_value={'path':'/dev/sdb','major_minor':'8:16','size_bytes':134217728000}) as check, patch.object(usb.subprocess,'run',side_effect=fake_run), redirect_stdout(io.StringIO()):
                self.assertEqual(usb.main(argv),0)
                self.assertEqual(check.call_count,3)
                before=receipt.read_bytes()
                self.assertEqual(usb.main(argv),1)
                self.assertEqual(receipt.read_bytes(),before)
            audit=json.loads(before)
            self.assertEqual(audit['status'],'verified')
            self.assertEqual(audit['helper_result']['bytes_verified'],4096)
            self.assertEqual(audit['source']['sha256'],digest)
            self.assertIn('not whole device capacity',audit['guarantee'])
            self.assertEqual(len(calls),2)

    def test_malformed_receipts_fail_closed_with_auditable_json(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder); auth=folder/'auth.json'; auth.write_text('[]')
            receipt=folder/'result.json'
            argv=['preflight','--authorization',str(auth),'--target',TARGET,'--iso','/absent.iso',
                  '--tested-receipt','/absent.json','--image','ubuntu@sha256:'+'a'*64,'--receipt',str(receipt)]
            with patch.object(usb,'preflight',side_effect=lambda auth,target:usb.validate_device(auth,inventory(),target,'/dev/sdb',(8,16),'','Filename Type Size Used Priority\n')), redirect_stdout(io.StringIO()):
                self.assertEqual(usb.main(argv),1)
            self.assertEqual(json.loads(receipt.read_text())['status'],'failed')

if __name__ == '__main__': unittest.main()
