#!/usr/bin/env python3
"""Deterministic public handoff ZIPs built from an explicit source allowlist."""
import argparse
import pathlib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
PAGES_FILES = ('_worker.js', '_routes.json', 'index.html')
# Never recurse through runtime directories: plans, saved profiles, catalogs,
# credentials, reports and account responses must not enter a public package.
SOURCE_FILES = (
    'converter.py', 'account.py', 'config.json', 'README.md',
    'tools/snapshot.py', 'tools/package.py', 'tools/check_publication.py',
    'tools/check_real_relay.mjs',
    'tests/test_converter.py', 'tests/test_account.py', 'tests/test_account_network.py',
    'tests/test_account_continuity.py',
    'tests/worker.test.mjs',
    'fixtures/catalog.yml', 'docs/runbook.md', 'docs/account-contract.md',
    'docs/category-mapping.csv', 'docs/подключение.txt',
)


def write_zip(target, entries):
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source, name in sorted(entries, key=lambda pair: pair[1]):
            if source.is_symlink() or not source.is_file():
                raise ValueError('Package input missing or symlink: ' + name)
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=pathlib.Path, default=ROOT)
    args = parser.parse_args()
    service = ROOT / 'avito-service'
    pages = [(service / 'pages' / name, name) for name in PAGES_FILES]
    write_zip(args.out / 'photostyle-avito-pages.zip', pages)
    names = [*SOURCE_FILES, *('pages/' + name for name in PAGES_FILES)]
    source = [(service / name, 'avito-service/' + name) for name in names]
    workflow = '.github/workflows/photostyle-avito.yml'
    source.append((ROOT / workflow, workflow))
    write_zip(args.out / 'photostyle-avito-source.zip', source)
    print('Built photostyle-avito-pages.zip and photostyle-avito-source.zip (allowlisted public files only)')


if __name__ == '__main__':
    main()
