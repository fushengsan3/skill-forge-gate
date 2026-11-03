#!/usr/bin/env python3
"""Build separate full/coreless ZIP distributions from tracked public source files."""
import argparse
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {'README.md', 'SKILL.md', 'LICENSE', '安装部署说明.md', 'editions/coreless/README.md'}
CODE_DIRS = {'daemon', 'scripts', 'panel', 'templates', 'verify', 'sandbox', 'docs'}


def build_package(source, output_dir, edition):
    if edition not in ('full', 'coreless'):
        raise ValueError('Unknown edition')
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    tracked = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode('utf-8').split('\0')
    data_paths = [line.split('|', 1)[0].rstrip('/') for line in
                  (source/'scripts/data-paths.txt').read_text(encoding='utf-8').splitlines()
                  if line.strip() and not line.lstrip().startswith('#')]
    content = {}
    for rel in tracked:
        if not rel:
            continue
        path = Path(rel)
        if rel not in TOP_FILES and path.parts[0] not in CODE_DIRS:
            continue
        if rel == 'scripts/build_release.py':
            continue  # The builder needs the source Git checkout and edition templates.
        if any(rel == data or rel.startswith(data + '/') for data in data_paths):
            continue
        if path.name.startswith('.') or any(part in ('__pycache__', 'node_modules', '.backup', '.tmp') for part in path.parts):
            continue
        if edition == 'coreless' and (path.parts[0] in ('verify', 'sandbox', 'docs', 'editions') or rel == '安装部署说明.md'):
            continue
        disk_path = (source/path).resolve()
        if not disk_path.is_relative_to(source) or disk_path != source/path or disk_path.is_symlink():
            raise ValueError('Package source contains a link: ' + rel)
        content[rel] = disk_path.read_bytes()
    metadata = content['daemon/edition.py'].decode('utf-8')
    if metadata.count('ID = "full"') != 1:
        raise ValueError('Source must be the full development edition')
    content['daemon/edition.py'] = metadata.replace('ID = "full"', f'ID = "{edition}"').encode('utf-8')
    if edition == 'coreless':
        for target, template in {'daemon/precheck.py':'precheck.py', 'scripts/self-update.sh':'self-update.sh',
                                 'README.md':'README.md', 'SKILL.md':'SKILL.md'}.items():
            content[target] = (source/'editions/coreless'/template).read_bytes()
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    content['sources.json'] = json.dumps({'skill-forge':{'self':True,'type':'remote',
        'url':'https://github.com/fushengsan3/skill-forge-gate', 'branch':'main',
        'installed_sha':sha}},ensure_ascii=False,indent=2).encode('utf-8')
    content['templates/install-queue.json'] = b'{"pending": [], "history": []}\n'
    panel = content['panel/fallback.html'].decode('utf-8')
    panel = panel.replace('data-edition="full"', f'data-edition="{edition}"')
    content['panel/fallback.html'] = panel.encode('utf-8')
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir/f'skill-forge-gate-{edition}.zip'
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, payload in sorted(content.items()):
            archive.writestr('skill-forge/' + rel, payload)
    return archive_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--edition', choices=('full','coreless','all'), default='all')
    parser.add_argument('--output-dir', type=Path, default=ROOT/'dist')
    args = parser.parse_args()
    for edition in (('full','coreless') if args.edition == 'all' else (args.edition,)):
        print(build_package(ROOT, args.output_dir, edition))


if __name__ == '__main__':
    main()
