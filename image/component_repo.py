#!/usr/bin/env python3
"""Build an offline, signed DesOS development component APT repository.

No packages are installed and no keys are generated. A dedicated existing GPG
home and exact primary signing fingerprint are mandatory; the default GPG home
is never used. This supplies development provenance, not production key policy.

Example (operator supplies an existing development signing key):
  python3 image/component_repo.py --deb-input /abs/desktop-debs \\
    --deb-input /abs/kernel-debs --deb-input /abs/desos-release.deb \\
    --output /abs/new-component-repo --gpg-home /abs/dedicated-gnupg \\
    --key-fingerprint FULL_PRIMARY_FINGERPRINT

Then independently verify using the explicitly selected home:
  gpg --no-options --homedir /abs/dedicated-gnupg --batch \\
    --verify /abs/new-component-repo/dists/desos/InRelease
  gpg --no-options --homedir /abs/dedicated-gnupg --batch \\
    --verify /abs/new-component-repo/manifest.json.asc \\
    /abs/new-component-repo/manifest.json

Deploy the repository and desos-archive-keyring.asc into a guest/rootfs only
through a separate operator workflow. The APT source uses suite desos/component
main and must explicitly trust that exported key with signed-by. An .incomplete
marker means construction failed; do not deploy it. Existing output is refused.
"""
import argparse
from datetime import datetime, timezone
from email.parser import Parser
from email.utils import format_datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

class RepoError(Exception):
    pass

def require(condition, message):
    if not condition:
        raise RepoError(message)

def sha256(path):
    value=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):
            value.update(block)
    return value.hexdigest()

def run(command, cwd=None):
    env=os.environ.copy(); env.update(LC_ALL='C',TZ='UTC')
    result=subprocess.run(command,cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,check=False)
    require(result.returncode==0, '%s failed (%d): %s' %
            (command[0],result.returncode,result.stderr.strip()[-2000:]))
    return result.stdout

def gpg_run(home, arguments):
    return run(['gpg','--no-options','--homedir',str(home),'--batch','--no-tty',
                '--pinentry-mode','error']+arguments)

def collect_inputs(inputs):
    require(bool(inputs),'at least one explicit .deb input is required')
    files=[]
    for value in inputs:
        path=Path(value).absolute()
        require(not path.is_symlink(),'symlink input is refused: '+str(path))
        require(path.exists(),'input does not exist: '+str(path))
        candidates=sorted(path.glob('*.deb')) if path.is_dir() else [path]
        for candidate in candidates:
            require(not candidate.is_symlink() and candidate.is_file() and candidate.suffix=='.deb',
                    'input must be a regular .deb file: '+str(candidate))
            files.append(candidate.resolve(strict=True))
    require(bool(files),'explicit input directories contain no .deb files')
    return sorted(set(files))

def package_metadata(path):
    fields=Parser().parsestr(run(['dpkg-deb','--field',str(path),'Package','Version','Architecture']))
    package=fields.get('Package',''); version=fields.get('Version',''); arch=fields.get('Architecture','')
    require(re.fullmatch(r'[a-z0-9][a-z0-9+.-]+',package) is not None,'invalid Debian package name')
    require(re.fullmatch(r'[0-9][A-Za-z0-9.+:~\-]*',version) is not None,'unsafe Debian package version')
    require(arch in ('amd64','all'),'component repository supports only amd64/all packages')
    return {'package':package,'version':version,'architecture':arch}

def validate_home(home, fingerprint):
    require(re.fullmatch(r'(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})',fingerprint) is not None,
            'full primary key fingerprint is required')
    home=Path(home).absolute()
    require(not home.is_symlink() and home.is_dir(),'dedicated GPG homedir must already exist without symlink')
    home=home.resolve(strict=True)
    require(home!=(Path.home()/'.gnupg').resolve(), 'default user GPG homedir is prohibited; use a dedicated home')
    info=home.stat()
    require(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)&0o077==0,
            'dedicated GPG homedir must be owned by this user with mode 0700 or stricter')
    return home,fingerprint.upper()

def verify_signature(home, fingerprint, signature, source=None):
    args=['--status-fd','1','--verify',str(signature)]
    if source is not None: args.append(str(source))
    status=gpg_run(home,args)
    valid=[line.split()[2] for line in status.splitlines()
           if line.startswith('[GNUPG:] VALIDSIG ') and len(line.split())>=3]
    require(valid==[fingerprint],'signature did not validate with the exact selected signing key')

def sign(home, fingerprint, source, destination, clear=False):
    args=['--status-fd','1','--local-user',fingerprint+'!','--armor',
          '--output',str(destination),'--clearsign' if clear else '--detach-sign',str(source)]
    gpg_run(home,args)
    require(destination.is_file() and destination.stat().st_size>0,'signer produced no signature')
    verify_signature(home,fingerprint,destination,None if clear else source)

def make_release(index_paths, directory, epoch):
    date=format_datetime(datetime.fromtimestamp(epoch,timezone.utc),usegmt=True)
    lines=['Origin: DesOS','Label: DesOS','Suite: desos','Codename: desos',
           'Date: '+date,'Architectures: amd64','Components: main',
           'Description: DesOS offline development components']
    for algorithm,label in [('sha256','SHA256'),('sha512','SHA512')]:
        lines.append(label+':')
        for path in index_paths:
            digest=hashlib.new(algorithm,path.read_bytes()).hexdigest()
            lines.append(' %s %16d %s' % (digest,path.stat().st_size,path.relative_to(directory).as_posix()))
    return '\n'.join(lines)+'\n'

def build_repository(inputs, output, gpg_home, fingerprint, epoch=None):
    files=collect_inputs(inputs)
    home,fingerprint=validate_home(gpg_home,fingerprint)
    require(shutil.which('dpkg-deb') is not None,'dpkg-deb is required')
    tool='apt-ftparchive' if shutil.which('apt-ftparchive') else 'dpkg-scanpackages'
    require(shutil.which(tool) is not None,'apt-ftparchive or dpkg-scanpackages is required')
    require(shutil.which('gpg') is not None,'gpg is required')
    output=Path(output).absolute()
    require(output.name not in ('','.','..'),'explicit new output directory is required')
    require(output.parent.is_dir(),'output parent must already exist')
    output=output.parent.resolve(strict=True)/output.name
    require(not os.path.lexists(output),'output already exists; refusing overwrite')
    require(home!=output and output not in home.parents and home not in output.parents,
            'repository output and dedicated key home must be disjoint')
    timestamp=int(time.time()) if epoch is None else epoch
    require(type(timestamp) is int and 0<=timestamp<=253402300799,'invalid source date epoch')
    # Atomic directory reservation: never replace an existing directory or file.
    try: output.mkdir(mode=0o755)
    except FileExistsError: raise RepoError('output appeared concurrently; refusing overwrite')
    incomplete=output/'.incomplete'
    incomplete.write_text('Repository build in progress; do not deploy.\n')
    # Listing is deliberately scoped to the dedicated home and exact fingerprint.
    keys=gpg_run(home,['--with-colons','--fingerprint','--list-secret-keys',fingerprint])
    primary=[]; pending=False
    for line in keys.splitlines():
        fields=line.split(':')
        if fields[0]=='sec': pending=True
        elif fields[0]=='ssb': pending=False
        elif fields[0]=='fpr' and pending:
            require(len(fields)>9,'malformed GPG fingerprint response')
            primary.append(fields[9]); pending=False
    require(primary==[fingerprint],'exact primary secret signing key not found in dedicated home')
    packages=[]; identities={}
    for source in files:
        metadata=package_metadata(source)
        digest=sha256(source)
        identity=(metadata['package'],metadata['version'],metadata['architecture'])
        if identity in identities:
            require(identities[identity]==digest,'conflicting bytes for the same package/version/architecture')
            continue
        identities[identity]=digest
        package=metadata['package']
        pool=Path('pool/main')/package[0]/package/('%s_%s_%s.deb'%identity)
        destination=output/pool; destination.parent.mkdir(parents=True,exist_ok=True)
        # Copy bytes, never link a repository payload to mutable source artifacts.
        shutil.copyfile(source,destination)
        require(sha256(destination)==digest and sha256(source)==digest,'package input changed during copy')
        destination.chmod(0o644)
        packages.append(dict(metadata,source_path=str(source),pool_path=pool.as_posix(),
                             size_bytes=destination.stat().st_size,sha256=digest))
    index=output/'dists/desos/main/binary-amd64/Packages'; index.parent.mkdir(parents=True)
    if tool=='apt-ftparchive':
        text=run([tool,'packages','pool'],cwd=output)
    else:
        text=run([tool,'--multiversion','pool','/dev/null'],cwd=output)
    require(bool(text.strip()),'package index is unexpectedly empty')
    index.write_text(text)
    compressed=index.with_name('Packages.gz')
    compressed.write_bytes(gzip.compress(text.encode(),mtime=0))
    release=index.parents[2]/'Release'
    release.write_text(make_release([index,compressed],release.parent,timestamp))
    release_sig=release.with_name('Release.gpg'); inrelease=release.with_name('InRelease')
    sign(home,fingerprint,release,release_sig)
    sign(home,fingerprint,release,inrelease,clear=True)
    public_key=output/'desos-archive-keyring.asc'
    exported=gpg_run(home,['--armor','--export-options','export-minimal','--export',fingerprint])
    require(exported.startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----'),'public key export failed')
    public_key.write_text(exported)
    artifacts=[index,compressed,release,release_sig,inrelease,public_key]
    manifest={'schema':'desos-component-repository-v1','origin':'DesOS','suite':'desos',
              'component':'main','architectures':['amd64'],'source_date_epoch':timestamp,
              'signing_fingerprint':fingerprint,'signature_scope':'development provenance; not production key management',
              'metadata_tool':tool,'packages':packages,
              'artifacts':{p.relative_to(output).as_posix():{'sha256':sha256(p),'size_bytes':p.stat().st_size} for p in artifacts},
              'signature_verification':{'release':True,'inrelease':True}}
    manifest_path=output/'manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    sign(home,fingerprint,manifest_path,output/'manifest.json.asc')
    incomplete.unlink()
    return {'status':'built','output':str(output),'package_count':len(packages),
            'signing_fingerprint':fingerprint,'manifest':str(manifest_path)}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--deb-input',action='append',required=True,help='explicit .deb file or nonrecursive directory; repeatable')
    parser.add_argument('--output',required=True,help='new output directory; parent must exist')
    parser.add_argument('--gpg-home',required=True,help='dedicated existing GPG homedir, mode0700, owned by current user')
    parser.add_argument('--key-fingerprint',required=True,help='full primary signing key fingerprint; exact key forced')
    parser.add_argument('--source-date-epoch',type=int,help='optional explicit Release timestamp')
    args=parser.parse_args(argv)
    try:
        result=build_repository(args.deb_input,args.output,args.gpg_home,args.key_fingerprint,args.source_date_epoch)
        print(json.dumps(result,sort_keys=True)); return 0
    except (RepoError,OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'failed','error':str(error),'output':str(Path(args.output).absolute())})); return 1

if __name__=='__main__': sys.exit(main())
