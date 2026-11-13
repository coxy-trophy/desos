"""The kernel metapackages and the desos-verify package: built for real, never installed."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from image import components, kernel_metapackage, verify_package

REPO = Path(__file__).resolve().parents[1]


def fields(deb):
    text = subprocess.check_output(['dpkg-deb', '--field', str(deb)], text=True)
    return dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line and not line.startswith(' '))


def payload(deb):
    data = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(deb)])
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        return {member.name[2:]: (archive.extractfile(member).read(), member.mode, member.uid, member.gid)
                for member in archive.getmembers() if member.isfile()}


@unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is needed to build packages')
class KernelMetapackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / 'kernel-meta'
        cls.result = kernel_metapackage.build(cls.output)
        cls.pin = json.loads((REPO / 'kernel/source-pin.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_each_metapackage_depends_on_exactly_the_pinned_versioned_package(self):
        version = self.pin['package_version']
        release = self.pin['version'] + self.pin['localversion']
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), sorted(
            'linux-%s-desos_%s_amd64.%s' % (kind, version, suffix) for kind in ('image', 'headers') for suffix in ('deb', 'json')))
        for kind in ('image', 'headers'):
            deb = self.output / ('linux-%s-desos_%s_amd64.deb' % (kind, version))
            control = fields(deb)
            self.assertEqual(control['Package'], 'linux-%s-desos' % kind)
            self.assertEqual(control['Version'], version)
            self.assertEqual(control['Architecture'], 'amd64')
            self.assertEqual(control['Depends'], 'linux-%s-%s (= %s)' % (kind, release, version))
            # No kernel, no maintainer scripts: only a note saying what it is.
            self.assertEqual(list(payload(deb)), ['usr/share/doc/linux-%s-desos/README' % kind])
            scripts = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(deb)])
            with tarfile.open(fileobj=io.BytesIO(scripts), mode='r:') as archive:
                self.assertEqual(sorted(name.lstrip('./') for name in archive.getnames() if name.lstrip('./')),
                                 ['control', 'md5sums'])
            record = json.loads(deb.with_suffix('.json').read_text())
            self.assertEqual(record['sha256'], hashlib.sha256(deb.read_bytes()).hexdigest())
            self.assertEqual(record['depends'], {'package': 'linux-%s-%s' % (kind, release), 'version': version})
            self.assertNotIn(str(Path.home()), json.dumps(record))

    def test_the_named_versioned_packages_are_the_ones_the_kernel_build_produced(self):
        built = REPO / 'output/kernel'
        if not built.is_dir():
            self.skipTest('no built kernel packages on this machine')
        identity = kernel_metapackage.kernel_identity()
        for kind in ('image', 'headers'):
            self.assertTrue((built / ('linux-%s-%s_%s_amd64.deb' % (kind, identity['release'], identity['package_version']))).is_file())

    def test_rebuild_is_identical_and_conflicting_output_is_kept(self):
        again = kernel_metapackage.build(Path(self.temp.name) / 'again')
        self.assertEqual(again['packages'], self.result['packages'])
        self.assertEqual(kernel_metapackage.build(self.output)['packages'], self.result['packages'])
        conflict = Path(self.temp.name) / 'conflict'
        conflict.mkdir()
        owner = conflict / Path(self.result['package_paths'][0]).name
        owner.write_bytes(b'owner artifact')
        with self.assertRaises(ValueError):
            kernel_metapackage.build(conflict)
        self.assertEqual(owner.read_bytes(), b'owner artifact')
        self.assertEqual(list(conflict.iterdir()), [owner])
        for unsafe in (Path('/'), Path('relative'), Path('/usr/bin')):
            with self.assertRaises(ValueError):
                kernel_metapackage.build(unsafe)

    def test_a_newer_pin_gives_a_higher_version_that_follows_the_new_kernel(self):
        pin = Path(self.temp.name) / 'pin.json'
        pin.write_text(json.dumps(dict(self.pin, version='6.18.70', package_version='6.18.70-1desos1')))
        self.assertEqual(kernel_metapackage.names(pin), ['linux-image-desos=6.18.70-1desos1', 'linux-headers-desos=6.18.70-1desos1'])
        result = kernel_metapackage.build(Path(self.temp.name) / 'next', pin)
        control = fields(result['package_paths'][0])
        self.assertEqual(control['Depends'], 'linux-image-6.18.70-desos (= 6.18.70-1desos1)')
        subprocess.run(['dpkg', '--compare-versions', control['Version'], 'gt', self.pin['package_version']], check=True)
        for broken in (dict(self.pin, package_version='7.0-1'), dict(self.pin, localversion='desos; rm'),
                       dict(self.pin, version='6.18.62 '), {k: v for k, v in self.pin.items() if k != 'source_date_epoch'}):
            pin.write_text(json.dumps(broken))
            with self.assertRaises((ValueError, KeyError)):
                kernel_metapackage.kernel_identity(pin)


@unittest.skipUnless(shutil.which('dpkg-deb'), 'dpkg-deb is needed to build packages')
class VerifyPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.result = verify_package.build(Path(cls.temp.name) / 'packages')
        cls.deb = Path(cls.result['package_path'])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def tree(self):
        root = REPO / 'verify/rootfs'
        return {path.relative_to(root).as_posix(): path for path in root.rglob('*')
                if path.is_file() and '__pycache__' not in path.parts}

    def test_package_holds_exactly_the_component_tree_owned_by_root(self):
        control = fields(self.deb)
        self.assertEqual((control['Package'], control['Version'], control['Architecture']),
                         ('desos-verify', verify_package.VERSION, 'all'))
        self.assertEqual(control['Depends'], 'python3, gpgv, dpkg')
        packaged, tree = payload(self.deb), self.tree()
        self.assertEqual(sorted(packaged), sorted(tree))
        for name, (data, mode, uid, gid) in packaged.items():
            self.assertEqual(data, tree[name].read_bytes(), name)
            self.assertEqual((uid, gid), (0, 0))
            self.assertEqual(mode, 0o755 if name == 'usr/bin/desos-verify' else 0o644, name)
        record = json.loads(Path(self.result['record_path']).read_text())
        self.assertEqual(record['sha256'], hashlib.sha256(self.deb.read_bytes()).hexdigest())
        again = verify_package.build(Path(self.temp.name) / 'again')
        self.assertEqual(again['sha256'], self.result['sha256'])

    def test_the_component_is_delivered_by_the_package_and_not_copied_in(self):
        component = components.load_manifest(REPO / 'verify', 'verify')
        self.assertEqual(component.package, 'desos-verify')
        self.assertNotIn('desos-verify', component.packages, 'the Ubuntu archive has no such package')
        self.assertEqual(components.source_entries(component), ([], []))
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / 'rootfs').mkdir()
            result = components.install(parent / 'rootfs', [component], parent)
            self.assertEqual(result['files'], 0)
            self.assertEqual(list((parent / 'rootfs').iterdir()), [])
        # Its units are still enabled by the image build, and every unit is in the package.
        for unit in component.units:
            self.assertIn('usr/lib/systemd/system/' + unit, payload(self.deb))
        done = subprocess.run(['python3', str(REPO / 'image/components.py'), 'packaged'], capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.split(), ['desos-verify'])
        packages = subprocess.run(['python3', str(REPO / 'image/components.py'), 'packages'], capture_output=True, text=True, check=True)
        self.assertNotIn('desos-verify', packages.stdout.split())

    def test_both_rootfs_stages_take_the_package_from_desos_inputs(self):
        build = (REPO / 'image/build-rootfs.sh').read_text()
        self.assertIn('components.py" packaged --recipes "$recipes"', build)
        self.assertLess(build.index('components.py" packaged'), build.index('systemctl enable "${units[@]}"'))
        finalize = (REPO / 'image/finalize-rootfs.sh').read_text()
        self.assertIn('components.py" packaged --recipes "$recipes"', finalize)
        self.assertIn('mapfile -t -O "${#desos_packages[@]}" desos_packages <<< "$packaged_components"', finalize)

    def test_package_version_follows_the_tool_and_the_ledger(self):
        tool = (REPO / 'verify/rootfs/usr/bin/desos-verify').read_text()
        self.assertIn('TOOL_VERSION = %s\n' % verify_package.VERSION.split('.')[0], tool)
        ledger = json.loads((REPO / 'image/package-ledger.json').read_text())
        for entry in ledger['packages']:
            if (entry['package'], entry['version']) == ('desos-verify', verify_package.VERSION):
                self.assertEqual(entry['sha256'], self.result['sha256'],
                                 'verify/rootfs changed: raise VERSION in image/verify_package.py')

    def test_optional_package_key_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'undo'
            base.mkdir()
            good = {'name': 'undo', 'packages': [], 'enable_units': [], 'notes': ''}
            for bad in (dict(good, package='Bad Name'), dict(good, package=5), dict(good, packages=['x-y'], package='x-y'),
                        dict(good, extra=1)):
                (base / 'component.json').write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    components.load_manifest(base, 'undo')
            (base / 'component.json').write_text(json.dumps(dict(good, package='desos-undo')))
            self.assertEqual(components.load_manifest(base, 'undo').package, 'desos-undo')
            (base / 'component.json').write_text(json.dumps(good))
            self.assertIsNone(components.load_manifest(base, 'undo').package)


if __name__ == '__main__':
    unittest.main()
