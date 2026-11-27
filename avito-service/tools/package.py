#!/usr/bin/env python3
"""Deterministic ZIPs for Cloudflare Direct Upload and source handoff (stdlib only)."""
import argparse, pathlib, zipfile
ROOT=pathlib.Path(__file__).resolve().parents[2]
def write_zip(target,entries):
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for source,name in sorted(entries,key=lambda pair:pair[1]):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            archive.writestr(info,source.read_bytes(),compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out',type=pathlib.Path,default=ROOT);args=parser.parse_args()
    pages=ROOT/'avito-service/pages'
    write_zip(args.out/'photostyle-avito-pages.zip',[(p,p.relative_to(pages).as_posix()) for p in pages.rglob('*') if p.is_file()])
    source=[p for p in (ROOT/'avito-service').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    workflow=ROOT/'.github/workflows/photostyle-avito.yml';source.append(workflow)
    reports=ROOT/'reports'
    if reports.exists():source.extend(p for p in reports.rglob('*') if p.is_file())
    write_zip(args.out/'photostyle-avito-source.zip',[(p,p.relative_to(ROOT).as_posix()) for p in source])
    print('Built photostyle-avito-pages.zip and photostyle-avito-source.zip')
if __name__=='__main__':main()
