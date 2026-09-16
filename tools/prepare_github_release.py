#!/usr/bin/env python3
"""Prepare an allowlisted source tree and optional macOS ZIP. Never uploads."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APPS = ['CBRAIN Device Setup', 'CBRAIN Studio', 'CBRAIN Firmware Builder', 'CBRAIN Recording Viewer']


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):digest.update(block)
    return digest.hexdigest()


def collect():
    paths = set()
    for line in (ROOT/'release/CORE_FILES.txt').read_text().splitlines():
        pattern=line.strip()
        if not pattern or pattern.startswith('#'):continue
        matches=[p for p in ROOT.glob(pattern) if p.is_file()]
        if not matches:raise ValueError(f'No files matched: {pattern}')
        for p in matches:
            if p.is_symlink() or not p.resolve().is_relative_to(ROOT):raise ValueError(f'Unexpected link: {p}')
            if p.suffix.lower() in {'.h5','.hdf5','.log','.pyc'} or any(x in p.parts for x in ('__pycache__','.git','.venv','backups')):
                raise ValueError(f'Unexpected local data: {p}')
            paths.add(p)
    return sorted(paths)


def prepare(output, apps=False):
    version=re.search(r"VERSION\s*=\s*['\"]([^'\"]+)", (ROOT/'cbrain_studio/suite/__init__.py').read_text())[1]
    paths=collect()
    for name in ('dongle_bridge','headstage_common'):
        folder=ROOT/'app/firmware'
        manifest=json.loads((folder/(name+'.manifest.json')).read_text())
        if sha(folder/(name+'.hex'))!=manifest['sha256']:raise ValueError(f'Firmware checksum mismatch: {name}')
    if apps:
        for name in APPS:
            app=ROOT/'app'/(name+'.app')
            info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
            if info['CFBundleShortVersionString']!=version:raise ValueError(f'App version mismatch: {name}')
            subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True)
    if output.exists():raise ValueError(f'Output already exists; choose another --output: {output}')
    output.mkdir(parents=True)
    source=output/'source';source.mkdir()
    records={}
    for p in paths:
        rel=p.relative_to(ROOT);dest=source/rel;dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(p,dest)
        records[rel.as_posix()]={'bytes':p.stat().st_size,'sha256':sha(dest)}
    (source/'release/PACKAGE_FILES.json').write_text(json.dumps(dict(version=version,files=records),ensure_ascii=False,indent=2)+'\n')
    stem='CBRAIN-studio-'+version
    archive=output/(stem+'-source.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(source.rglob('*')):
            if p.is_file():z.write(p,str(Path(stem)/p.relative_to(source)))
    archives=[archive]
    if apps:
        with tempfile.TemporaryDirectory(prefix='.macos-',dir=output) as tmp:
            package=Path(tmp)/stem
            shutil.copytree(source,package)
            for name in APPS:
                shutil.copytree(ROOT/'app'/(name+'.app'),package/'app'/(name+'.app'),symlinks=True)
            archive=output/(stem+'-macOS-arm64.zip')
            subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(package),str(archive)],check=True)
        archives.append(archive)
    (output/'SHA256SUMS.txt').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in archives))
    print(f'{len(paths)} reviewed files -> {source}')
    for p in archives:print(f'{p.name}: {p.stat().st_size/1024/1024:.1f} MiB')
    print('Local preparation complete. No git commit, remote creation, or upload was performed.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'build/github-ready')
    parser.add_argument('--apps',action='store_true',help='Include the four installed macOS apps in a separate ZIP')
    args=parser.parse_args()
    prepare(args.output.expanduser().resolve(),args.apps)


if __name__=='__main__':main()
