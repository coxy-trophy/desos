#!/usr/bin/env python3
"""Fail-closed, single-device DesOS USB writer. No implicit device or test bypass."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

TARGET = '/dev/disk/by-id/usb-VendorCo_ProductCode_6912801040691779938-0:0'
SERIAL = '6912801040691779938'
MODEL = 'ProductCode'
SIZE = 134217728000
REQUIRED_TESTS = ('bios_live', 'uefi_live', 'native_desktop', 'installer_empty_disk',
                  'installed_boot', 'live_account_cleanup', 'iso_integrity')
AUTH_FIELDS = {'usb_erase_approved': True, 'approved_answer': 'Erase this USB and write DesOS',
               'device_at_approval': '/dev/sdb', 'model': MODEL, 'serial': SERIAL,
               'size_bytes': SIZE, 'transport': 'usb', 'removable': True,
               'internal_disk_prohibited': '/dev/sda', 'prohibited_internal_serial': 'ES72N072110902A1X',
               'require_identity_revalidation_before_write': True, 'require_tested_iso_before_write': True}

class GuardError(Exception):
    pass

def require(condition, message):
    if not condition:
        raise GuardError(message)

def now():
    return datetime.now(timezone.utc).isoformat()

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def load_json(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)

def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get('children', []))

def block_identity(path):
    info = os.stat(path)
    require(stat.S_ISBLK(info.st_mode), 'target must be an actual block device')
    return os.major(info.st_rdev), os.minor(info.st_rdev)

def validate_device(auth, inventory, target, resolved, devnum, mountinfo, swaps):
    require(isinstance(auth, dict), 'authorization must be a JSON object')
    require(isinstance(inventory, dict), 'device inventory must be a JSON object')
    for key, expected in AUTH_FIELDS.items():
        require(type(auth.get(key)) is type(expected) and auth.get(key) == expected,
                'explicit authorization mismatch: ' + key)
    require(target == TARGET, 'only the exact approved by-id target is accepted')
    require(resolved.startswith('/dev/') and resolved != '/dev/sda', 'internal or invalid device prohibited')
    nodes = list(walk(inventory.get('blockdevices', [])))
    matches = [n for n in nodes if str(n.get('serial', '')).strip() == SERIAL]
    require(len(matches) == 1, 'approved serial missing or ambiguous')
    device = matches[0]
    require(device.get('path', device.get('name')) == resolved, 'by-id and inventory target disagree')
    require(device.get('type') == 'disk', 'target must be whole disk')
    require(str(device.get('model', '')).strip() == MODEL, 'wrong device model')
    require(device.get('size') == SIZE, 'wrong device size')
    require(device.get('tran') == 'usb', 'target is not USB transport')
    require(device.get('rm') is True or device.get('rm') == 1, 'target is not removable')
    require(device.get('ro') is False or device.get('ro') == 0, 'target is read-only or unknown')
    major_minor = '%d:%d' % devnum
    require(device.get('maj:min') == major_minor, 'block device number changed')
    descendants = list(walk([device]))
    names = set()
    numbers = set()
    for node in descendants:
        require(node.get('type') in ('disk', 'part'), 'target has active mapped descendants')
        require('mountpoints' in node and not any(node['mountpoints'] or []),
                'target or partition is mounted; unmount explicitly and retry')
        name = node.get('path', node.get('name'))
        require(isinstance(name, str) and name.startswith('/dev/'), 'invalid target child path')
        require(name != '/dev/sda', 'prohibited internal disk found')
        require(node.get('maj:min'), 'target child device number unavailable')
        names.add(name)
        numbers.add(node['maj:min'])
    for line in mountinfo.splitlines():
        fields = line.split()
        require(len(fields) >= 6, 'malformed host mount inventory')
        require(fields[2] not in numbers, 'host mountinfo reports mounted target')
    lines = swaps.splitlines()
    require(lines and lines[0].startswith('Filename'), 'swap inventory unavailable')
    for line in lines[1:]:
        fields = line.split()
        require(fields and os.path.realpath(fields[0]) not in names, 'target is active swap')
    return {'path': resolved, 'by_id': target, 'serial': SERIAL, 'model': MODEL,
            'size_bytes': SIZE, 'transport': 'usb', 'removable': True,
            'major_minor': major_minor, 'unmounted': True, 'checked_utc': now()}

def validate_source(source, tested_receipt, capacity):
    source = Path(source).resolve(strict=True)
    require(source.is_file(), 'ISO must be a regular file')
    size = source.stat().st_size
    require(size > 0 and size % 2048 == 0, 'ISO must be nonempty and aligned to 2048 bytes')
    require(size <= capacity, 'ISO exceeds actual approved device size')
    tested = load_json(tested_receipt)
    require(isinstance(tested, dict) and isinstance(tested.get('tests'), dict), 'tested receipt must contain a tests object')
    digest = sha256_file(source)
    require(tested.get('iso_path') == str(source), 'tested ISO path mismatch')
    require(tested.get('size_bytes') == size, 'tested ISO size mismatch')
    require(tested.get('sha256') == digest, 'tested ISO hash mismatch')
    require(all(tested.get('tests', {}).get(key) is True for key in REQUIRED_TESTS),
            'all required ISO and VM tests must have observed passing receipts')
    require(',' not in str(source) and '\n' not in str(source), 'unsupported source path')
    return {'path': str(source), 'size_bytes': size, 'sha256': digest,
            'tested_receipt': str(Path(tested_receipt).resolve()),
            'tested_receipt_sha256': sha256_file(tested_receipt), 'tests': tested['tests']}

def docker_command(image, target, source, helper, device, iso):
    require(re.fullmatch(r'(?:ubuntu|docker\.io/library/ubuntu)@sha256:[0-9a-f]{64}', image) is not None,
            'image must be a digest-pinned official ubuntu image')
    require(target == TARGET, 'unapproved Docker device')
    for path in (source, helper):
        require(path.startswith('/') and ',' not in path and '\n' not in path, 'unsupported bind path')
    require(re.fullmatch(r'\d+:\d+', device['major_minor']) is not None, 'invalid device number')
    require(re.fullmatch(r'/dev/sd[b-z]+', device.get('path', '')) is not None, 'invalid canonical USB device')
    require(type(iso['size_bytes']) is int and 0 < iso['size_bytes'] <= SIZE and iso['size_bytes'] % 2048 == 0,
            'invalid ISO size')
    require(re.fullmatch('[0-9a-f]{64}', iso['sha256']) is not None, 'invalid ISO hash')
    return ['docker', 'run', '--rm', '--pull=never', '--network=none', '--cap-drop=ALL',
            '--security-opt=no-new-privileges:true', '--read-only', '--pids-limit=32',
            '--memory=256m', '--cpus=1', '--user=0:0', '--log-driver=none',
            '--device=' + device['path'] + ':/dev/desos-target:rw',
            '--mount=type=bind,src=' + source + ',dst=/source.iso,readonly',
            '--mount=type=bind,src=' + helper + ',dst=/usb-helper.sh,readonly',
            '--entrypoint=/bin/bash', image, '/usb-helper.sh', device['major_minor'],
            str(iso['size_bytes']), iso['sha256']]

def validate_result(result, source):
    require(isinstance(result, dict), 'helper result must be an object')
    require(result.get('status') == 'verified', 'container helper did not verify write')
    require(result.get('source_sha256') == source['sha256'] and result.get('readback_sha256') == source['sha256'],
            'readback or source hash mismatch')
    require(result.get('bytes_verified') == source['size_bytes'], 'incomplete readback')
    for key in ('write_fsync', 'direct_read', 'source_unchanged'):
        require(result.get(key) is True, 'missing successful outcome: ' + key)

def run(command):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(result.returncode == 0, 'command failed (%d): %s' % (result.returncode, result.stderr.strip()[-2000:]))
    return result.stdout

def preflight(auth, target):
    devnum = block_identity(target)
    resolved = os.path.realpath(target)
    inventory = json.loads(run(['lsblk', '--json', '--bytes', '--paths', '--output',
        'NAME,KNAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MAJ:MIN,MOUNTPOINTS']))
    return validate_device(auth, inventory, target, resolved, devnum,
                           Path('/proc/self/mountinfo').read_text(), Path('/proc/swaps').read_text())

def write_receipt(path, receipt):
    # The original file is reserved O_EXCL; replacement is atomic and restricted.
    temporary = path.with_name(path.name + '.updating')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, sort_keys=True, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temporary.exists(): temporary.unlink()

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'flash'))
    for name in ('authorization', 'target', 'iso', 'tested-receipt', 'image', 'receipt'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    receipt_path = Path(args.receipt).absolute()
    receipt = {'schema': 'desos-usb-receipt-v1', 'action': args.action, 'started_utc': now(),
               'status': 'preflight', 'write_attempted': False,
               'guarantee': 'Full source ISO byte readback only; not whole device capacity certification.'}
    reserved = False
    try:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd); reserved = True
        write_receipt(receipt_path, receipt)
        auth = load_json(args.authorization)
        receipt['authorization_sha256'] = sha256_file(args.authorization)
        receipt['device'] = preflight(auth, args.target)
        receipt['source'] = validate_source(args.iso, args.tested_receipt, receipt['device']['size_bytes'])
        helper = str(Path(__file__).with_name('usb_helper.sh').resolve(strict=True))
        receipt['helper_sha256'] = sha256_file(helper)
        command = docker_command(args.image, args.target, receipt['source']['path'], helper,
                                 receipt['device'], receipt['source'])
        receipt['container_image'] = args.image
        receipt['container_command'] = command
        if args.action == 'preflight':
            receipt['status'] = 'ready'
        else:
            # Check digest exists locally before final identity/mount revalidation.
            image = json.loads(run(['docker', 'image', 'inspect', args.image]))
            require(len(image) == 1, 'ambiguous container image')
            receipt['container_image_id'] = image[0]['Id']
            # Persist intent BEFORE last check so failures/interruption remain auditable.
            receipt['status'] = 'write_pending'
            write_receipt(receipt_path, receipt)
            auth_again = load_json(args.authorization)
            require(sha256_file(args.authorization) == receipt['authorization_sha256'], 'authorization changed')
            receipt['device_immediately_before_write'] = preflight(auth_again, args.target)
            require(receipt['device_immediately_before_write']['major_minor'] == receipt['device']['major_minor'],
                    'target device changed during preflight')
            receipt['write_attempted'] = True
            receipt['status'] = 'writing'
            write_receipt(receipt_path, receipt)
            # No commands or waits between this final host identity check and Docker run.
            final_device = preflight(auth_again, args.target)
            require(final_device['major_minor'] == receipt['device']['major_minor'] and
                    final_device['path'] == receipt['device']['path'], 'target changed before launch')
            receipt['final_host_check'] = final_device
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            receipt['container_exit_code'] = result.returncode
            receipt['container_stderr'] = result.stderr.strip()[-2000:]
            try:
                receipt['helper_result'] = json.loads(result.stdout)
            except (ValueError, TypeError):
                raise GuardError('helper produced no valid JSON result')
            require(result.returncode == 0, 'USB helper failed; target may contain a partial ISO')
            validate_result(receipt['helper_result'], receipt['source'])
            receipt['status'] = 'verified'
        receipt['finished_utc'] = now()
        write_receipt(receipt_path, receipt)
        print(json.dumps({'status': receipt['status'], 'receipt': str(receipt_path)}))
        return 0
    except (GuardError, OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as error:
        receipt.update(status='failed', error=str(error) or 'interrupted', finished_utc=now())
        if reserved:
            try: write_receipt(receipt_path, receipt)
            except OSError: pass
        print(json.dumps({'status': 'failed', 'error': receipt['error'],
                          'write_attempted': receipt['write_attempted'],
                          'receipt': str(receipt_path) if reserved else None}))
        return 1

if __name__ == '__main__':
    sys.exit(main())
