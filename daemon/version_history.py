"""History from the registered public source and reversible local backups."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from daemon import fetcher, installer, module_install, precheck, safe_paths, net, privacy


def write_backup_metadata(backup, record):
    Path(str(backup) + '.json').write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')


def _entry(name, tool):
    safe_paths.check_name(name)
    if tool == 'codex':
        registry, sources, dest = module_install._codex_entry(name)
        backups = module_install.CODEX_ROOT.parent / '.skill-forge-backups'
    elif tool == 'claude':
        registry = installer.SOURCES_FILE
        sources = json.loads(registry.read_text(encoding='utf-8'))
        dest = safe_paths.resolve_within(installer.SKILL_ROOT, name)
        if name not in sources or sources[name].get('self') or not dest.is_dir() or dest.is_symlink():
            raise ValueError('技能未登记或无法更新')
        backups = installer.FORGE_ROOT / '.backup'
    else:
        raise ValueError('未知工具')
    return registry, sources, dest, backups


def _backup_names(folder, name):
    pattern = re.compile(re.escape(name) + r'-\d{8}-\d{6}(?:-\d{6})?$')
    return sorted((p for p in folder.iterdir() if pattern.fullmatch(p.name) and p.is_dir()
                   and not p.is_symlink() and not getattr(p, 'is_junction', lambda: False)()), reverse=True) if folder.is_dir() else []


def _metadata(path):
    try:
        data = json.loads(Path(str(path) + '.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _git_history(url, branch):
    """GitHub's anonymous REST API can be rate-limited; use public Git history."""
    with tempfile.TemporaryDirectory(prefix='sf-history-') as temp:
        repo = Path(temp) / 'repo'
        ok, _ = installer._git_clone(url, repo, branch, strict_branch=True)
        if not ok:
            return []
        env = privacy.git_env(net.subprocess_env())
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.run(['git', '-C', str(repo), 'fetch', '--deepen=29', 'origin', branch],
                       env=env, capture_output=True, timeout=120, creationflags=flags)
        result = subprocess.run(['git', '-C', str(repo), 'log', '-30', '--format=%H%x00%cI%x00%s'],
                                env=env, capture_output=True, text=True, errors='replace', timeout=20, creationflags=flags)
        commits = []
        for line in result.stdout.splitlines():
            fields = line.split('\0', 2)
            if len(fields) == 3 and re.fullmatch(r'[0-9a-f]{40}', fields[0]):
                commits.append({'sha':fields[0], 'date':fields[1], 'message':fields[2][:140]})
        return commits


def history(name, tool='claude'):
    try:
        _, sources, _, folder = _entry(name, tool)
        info = sources[name]
        url = module_install._url(info.get('url', ''))
        path = urlsplit(url).path.strip('/').removesuffix('.git')
        query = {'sha': info.get('branch') or 'main', 'per_page': 30}
        subpath = info.get('subpath') or ''
        if subpath:
            parts = subpath.replace('\\', '/').split('/')
            if any(p in ('', '.', '..') for p in parts) or subpath.startswith('/') or ':' in subpath:
                raise ValueError('登记的仓库子目录无效')
            # Keep the full branch history so versions before a module move remain selectable.
        response = fetcher.api_get('https://api.github.com/repos/' + path + '/commits?' + urlencode(query))
        commits = []
        for item in response.get('data', []) if response.get('ok') and isinstance(response.get('data'), list) else []:
            sha = item.get('sha', '')
            if not re.fullmatch(r'[0-9a-f]{40}', sha): continue
            commit = item.get('commit') or {}
            commits.append({'sha':sha, 'message':str(commit.get('message', '')).split('\n')[0][:140],
                            'date':(commit.get('committer') or {}).get('date', '')})
        if not response.get('ok'):
            try:
                commits = _git_history(url, query['sha'])
            except (OSError, subprocess.SubprocessError, ValueError):
                commits = []
        backups = [{'name':p.name, 'sha':_metadata(p).get('installed_sha', '')} for p in _backup_names(folder, name)]
        return {'ok':True, 'commits':commits, 'backups':backups,
                'warning':'' if response.get('ok') else ('GitHub API 受限，已使用 Git 历史' if commits else '无法获取线上版本；仍可使用本地备份')}
    except Exception as exc:
        return {'ok':False, 'error':installer._redact(str(exc))}


def rollback(name, tool, backup_name):
    try:
        with installer._update_lock:
            registry, sources, dest, folder = _entry(name, tool)
            available = {p.name:p for p in _backup_names(folder, name)}
            if backup_name not in available:
                raise ValueError('备份不存在或不可用')
            backup = available[backup_name]
            info = sources[name]
            old = _metadata(backup)
            if old and module_install._url(old.get('url', '')) != module_install._url(info.get('url', '')):
                raise ValueError('备份来源与当前技能不一致')
            with tempfile.TemporaryDirectory(prefix='.sf-rollback-', dir=dest.parent) as temp:
                stage = Path(temp) / name
                shutil.copytree(backup, stage, symlinks=True)
                if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in stage.rglob('*')):
                    raise ValueError('备份包含链接，无法恢复')
                collection = tool == 'claude' and info.get('type') == 'collection' and (stage / 'skills').is_dir()
                staged = {name:stage}
                records = {}
                restored_children = []
                if collection:
                    from verify.l1_structure import find_skill_file
                    members = {p.name:p for p in (stage / 'skills').iterdir() if p.is_dir() and find_skill_file(p)}
                    if not members: raise ValueError('集合备份中没有可恢复的子技能')
                    stamp = backup.name[len(name) + 1:]
                    for child in members:
                        child_backup = folder / (child + '-' + stamp)
                        if not child_backup.is_dir() or child_backup.is_symlink():
                            continue
                        child_info = sources.get(child)
                        if not child_info or child_info.get('installed_sha') != info.get('installed_sha') or module_install._url(child_info.get('url', '')) != module_install._url(info.get('url', '')):
                            raise ValueError('子技能已单独变更，无法一起回退：' + child)
                        child_stage = Path(temp) / ('child-' + child)
                        shutil.copytree(child_backup, child_stage, symlinks=True)
                        if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in child_stage.rglob('*')):
                            raise ValueError('子技能备份包含链接：' + child)
                        child_old = _metadata(child_backup)
                        records[child] = dict(child_info, **child_old) if child_old else dict(child_info, installed_sha='')
                        staged[child] = child_stage
                        restored_children.append(child)
                    verdict = precheck.verify_collection(stage, info.get('url', ''), members, skills_dir=dest.parent)
                else:
                    verdict = precheck.verify_repo(stage, info.get('url', ''), skills_dir=dest.parent)
                if not verdict.get('ok'):
                    return {'ok':False, 'error':'备份预检未通过：' + verdict.get('summary', ''), 'verify':verdict}
                record = dict(info, **old) if old else dict(info, installed_sha='')
                record.update(updated_at=datetime.now().isoformat(timespec='seconds'),
                              trust_level=installer._trust_level(verdict), trust_note=installer._trust_note(verdict))
                if tool == 'claude':
                    records[name] = record
                    saved = installer._commit_staged_updates(staged, records, Path(temp))[name]
                    result = installer._finish_codex_sync(name, {'ok':True, 'name':name, 'backup':saved, 'verify':verdict, 'sha':record.get('installed_sha', ''), 'updated_skills':restored_children})
                else:
                    saved = module_install._codex_backup(name)
                    dest.rename(saved)
                    try:
                        stage.rename(dest)
                        sources[name] = record
                        module_install._write_registry(registry, sources)
                    except Exception:
                        if dest.exists(): shutil.rmtree(dest)
                        saved.rename(dest)
                        raise
                    try: write_backup_metadata(saved, info)
                    except OSError: pass
                    result = {'ok':True, 'name':name, 'tool':tool, 'backup':str(saved), 'verify':verdict, 'sha':record.get('installed_sha', '')}
                result['version_unknown'] = not bool(old.get('installed_sha'))
                return result
    except Exception as exc:
        return {'ok':False, 'error':installer._redact(str(exc))}
