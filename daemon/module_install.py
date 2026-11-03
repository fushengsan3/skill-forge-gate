"""Preview and install explicitly selected repository skills into their own tools."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from daemon import installer, precheck, safe_paths, privacy, translate_ai, translate_config
from daemon.skill_entry import FRONTMATTER_RE, find_skill_file

CODEX_ROOT = Path.home() / '.agents' / 'skills'
EXCLUDED = {'.git', '.github', 'node_modules', '__pycache__', '.venv', 'tests', 'test', 'examples'}


def _url(url):
    parsed = urlsplit(url or '')
    parts = parsed.path.strip('/').split('/')
    if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password or len(parts) < 2:
        raise ValueError('仅支持 GitHub HTTPS 仓库地址')
    owner, repo = parts[:2]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
        raise ValueError('仓库地址无效')
    return f'https://github.com/{owner}/{repo.removesuffix(".git")}.git'


def inspect_repo(repo, name):
    repo = Path(repo).resolve()
    safe_paths.check_name(name)
    parents = set()
    for file in installer._find_skill_files(repo):
        rel = file.relative_to(repo)
        if any(part.lower() in EXCLUDED for part in rel.parts):
            continue
        parent = file.parent.resolve()
        if parent.is_relative_to(repo):
            parents.add(parent)
    modules = []
    for parent in sorted(parents, key=lambda p: p.relative_to(repo).as_posix()):
        entry = find_skill_file(parent)
        if not entry or entry.is_symlink() or entry.resolve().parent != parent:
            continue
        rel = parent.relative_to(repo).as_posix()
        relative_parts = parent.relative_to(repo).parts
        parts = {part.lower() for part in relative_parts[:-1]}
        if len(relative_parts) >= 2 and relative_parts[-2].lower() in ('configs', 'platforms', 'runtimes'):
            parts.add(relative_parts[-1].lower())
        if any('codex' in p for p in parts):
            platform = 'codex'
        elif any('claude' in p for p in parts):
            platform = 'claude'
        elif any(tool in p for p in parts for tool in ('antigravity', 'copilot', 'cursor', 'gemini', 'windsurf', 'kimi', 'opencode')):
            platform = 'other'
        else:
            platform = 'generic'
        name_for_install = name if parent == repo else parent.name
        try:
            safe_paths.check_name(name_for_install)
        except safe_paths.UnsafeName:
            continue
        content = entry.read_text(encoding='utf-8', errors='replace')[:12000]
        fm = FRONTMATTER_RE.match(content)
        match = re.search(r'^description\s*:\s*(.+)$', fm.group(1), re.MULTILINE) if fm else None
        description = match.group(1).strip().strip('"\'')[:240] if match else ''
        if match and re.fullmatch(r'[>|][+-]?', description):
            continuation = []
            for line in fm.group(1)[match.end():].splitlines():
                if not line.strip():
                    continue
                if not line[:1].isspace():
                    break
                continuation.append(line.strip())
            description = ' '.join(continuation)[:240]
        modules.append({'path': rel, 'name': name_for_install, 'platform': platform,
                        'description': description, 'codex_supported': entry.name == 'SKILL.md'})
    return modules


def translate_public_modules(modules):
    """Translate only public repository descriptions after removing private indicators."""
    modules = [dict(module) for module in modules]
    config = translate_config.load()
    if config.get('backend') in ('google', 'local'):
        return modules, '当前使用本地模式，模块简介显示原文'
    pending = []
    for index, module in enumerate(modules):
        description = module.get('description', '')
        if not description:
            continue
        if re.search(r'[\u4e00-\u9fff]', description):
            module['description_zh'] = description
        else:
            pending.append({'id': index, 'text': privacy.clean_public_description(description)})
    model = config.get('model') or 'deepseek-flash'
    for start in range(0, len(pending), 8):
        batch = pending[start:start + 8]
        try:
            response = translate_ai.complete(json.dumps({'items': batch}, ensure_ascii=False), model,
                                             purpose='public_descriptions')
            parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', response.strip()))
            if not isinstance(parsed, list):
                raise ValueError('invalid translation')
            allowed = {item['id'] for item in batch}
            for item in parsed:
                if (isinstance(item, dict) and type(item.get('id')) is int
                        and item['id'] in allowed and isinstance(item.get('text'), str)):
                    modules[item['id']]['description_zh'] = item['text'][:500]
        except (translate_ai.AIBackendError, ValueError, TypeError, KeyError):
            return modules, '部分模块翻译不可用，显示原文；请检查 DeepSeek 密钥和模型'
    return modules, ''


def preview(name, url):
    safe_paths.check_name(name)
    url = _url(url)
    with tempfile.TemporaryDirectory(prefix='sf-preview-') as temp:
        repo = Path(temp) / 'repo'
        ok, detail = installer._git_clone(url, repo)
        if not ok:
            return {'ok': False, 'error': '无法读取仓库：' + installer._redact(detail)}
        sha = installer._get_latest_sha(repo)
        if not sha:
            return {'ok': False, 'error': '无法读取仓库版本，未开始安装'}
        try:
            proc = subprocess.run(['git', '-C', str(repo), 'branch', '--show-current'],
                                  capture_output=True, text=True, errors='replace', timeout=10,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            branch = proc.stdout.strip() if proc.returncode == 0 else ''
        except (OSError, subprocess.SubprocessError):
            branch = ''
        if not branch:
            return {'ok': False, 'error': '无法确认仓库分支，未开始安装'}
        modules, translation_warning = translate_public_modules(inspect_repo(repo, name))
        for module in modules:
            claude_path = installer.SKILL_ROOT / module['name']
            codex_path = CODEX_ROOT / module['name']
            codex_link = codex_path.is_symlink() or getattr(codex_path, 'is_junction', lambda: False)()
            owned = codex_link and codex_path.resolve() == claude_path.resolve()
            module['targets'] = {
                'claude': 'exists' if os.path.lexists(claude_path) else 'available',
                'codex': 'sync-link' if owned else ('exists' if os.path.lexists(codex_path) else 'available'),
            }
        return {'ok': bool(modules), 'error': '' if modules else '仓库中没有可安装的技能',
                'sha': sha, 'branch': branch, 'modules': modules, 'translation_warning': translation_warning}


def _write_registry(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.sf-sources-', suffix='.tmp', delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _copy_module(source, destination):
    """Copy one selected skill, excluding nested skills selected separately."""
    nested = {entry.parent for entry in installer._find_skill_files(source) if entry.parent != source}
    def ignore(directory, names):
        return {name for name in names if name in ('.git', '.github', 'node_modules')
                or Path(directory) / name in nested}
    shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in destination.rglob('*')):
        raise ValueError('模块包含链接，无法安全安装')


def install_selected(name, url, branch, expected_sha, selected):
    with installer._update_lock:
        return _install_selected(name, url, branch, expected_sha, selected)


def _install_selected(name, url, branch, expected_sha, selected):
    try:
        safe_paths.check_name(name)
        url = _url(url)
        if not isinstance(selected, list) or not selected:
            raise ValueError('请选择至少一个模块')
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', expected_sha or ''):
            raise ValueError('预览版本无效，请重新预览')
        if not isinstance(branch, str) or not branch or branch.startswith('-'):
            raise ValueError('预览分支无效，请重新预览')
        with tempfile.TemporaryDirectory(prefix='sf-selected-') as temp:
            repo = Path(temp) / 'repo'
            ok, detail = installer._git_clone(url, repo, branch, strict_branch=True)
            if not ok:
                raise ValueError('Clone 失败：' + installer._redact(detail))
            sha = installer._get_latest_sha(repo)
            if sha.lower() != expected_sha.lower():
                return {'ok':False, 'error':'仓库版本已变化，请重新预览后选择模块',
                        'requires_preview':True, 'modules':[], 'failed_modules':selected}
            catalog = {x['path']: x for x in inspect_repo(repo, name)}
            plan, destinations = [], set()
            for sel in selected:
                if not isinstance(sel, dict) or sel.get('path') not in catalog or sel.get('target') not in ('claude', 'codex'):
                    raise ValueError('选择包含无效的模块或工具，请重新预览')
                module = catalog[sel['path']]
                target = sel['target']
                if module['platform'] not in ('generic', target):
                    raise ValueError('所选工具与模块版本不匹配：' + module['path'])
                if target == 'codex' and not module['codex_supported']:
                    raise ValueError('Codex 技能需要 SKILL.md：' + module['path'])
                identity = (target, module['name'].lower())
                if identity in destinations:
                    raise ValueError('同一工具不能安装两个同名版本：' + module['name'])
                destinations.add(identity)
                plan.append((module, target, sel))
            results, failed = [], []
            for module, target, sel in plan:
                root = installer.SKILL_ROOT if target == 'claude' else CODEX_ROOT
                registry = installer.SOURCES_FILE if target == 'claude' else installer.FORGE_ROOT / 'templates' / 'codex-sources.json'
                item = {'name': module['name'], 'path': module['path'], 'target': target}
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    sources = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {}
                    link = root.resolve() / module['name']
                    is_link = link.is_symlink() or getattr(link, 'is_junction', lambda: False)()
                    owned_sync = (target == 'codex' and is_link and
                                  link.resolve() == (installer.SKILL_ROOT / module['name']).resolve())
                    dest = link if owned_sync else safe_paths.resolve_within(root, module['name'])
                    if (os.path.lexists(link) and not owned_sync) or module['name'] in sources:
                        raise ValueError('该工具已存在同名技能，未覆盖')
                    source = (repo / module['path']).resolve()
                    with tempfile.TemporaryDirectory(prefix='.sf-install-', dir=root) as stage:
                        staged = Path(stage) / module['name']
                        _copy_module(source, staged)
                        verdict = precheck.verify_repo(staged, url, skills_dir=root)
                        item['verify'] = verdict
                        if not verdict.get('ok'):
                            raise ValueError(verdict.get('summary', '安全预检未通过'))
                        record = {'type':'remote','url':url,'branch':branch,
                                  'subpath':'' if module['path'] == '.' else module['path'],
                                  'installed_sha':sha,'installed_at':datetime.now().isoformat(timespec='seconds'),
                                  'source':'skill-forge','selection_mode':True,'trust_level':installer._trust_level(verdict),
                                  'trust_note':installer._trust_note(verdict)}
                        backup = _codex_backup(module['name']) if owned_sync else None
                        if backup:
                            link.rename(backup)
                        installed = False
                        try:
                            staged.rename(dest)
                            installed = True
                            sources[module['name']] = record
                            _write_registry(registry, sources)
                        except Exception:
                            if installed: shutil.rmtree(dest)
                            if backup and not os.path.lexists(dest): backup.rename(dest)
                            raise
                        if backup: item['backup'] = str(backup)
                    item['ok'] = True
                except Exception as exc:
                    item.update(ok=False, error=installer._redact(str(exc)))
                    failed.append(sel)
                results.append(item)
            # Explicit Codex choices are independent; only sync unpaired Claude skills.
            codex_names = {m['name'].lower() for m, t, _ in plan if t == 'codex'}
            sync_names = [r['name'] for r in results if r['ok'] and r['target'] == 'claude' and r['name'].lower() not in codex_names]
            from daemon.codex_sync import sync_skills
            sync = sync_skills(installer.SKILL_ROOT, installer.FORGE_ROOT, sync_names) if sync_names else None
            return {'ok': not failed, 'error': '' if not failed else '部分模块未安装',
                    'modules': results, 'failed_modules': failed, 'sha': sha, 'codex_sync': sync}
    except Exception as exc:
        return {'ok': False, 'error': installer._redact(str(exc)), 'modules': [], 'failed_modules': selected}


def _codex_entry(name):
    safe_paths.check_name(name)
    registry = installer.FORGE_ROOT / 'templates' / 'codex-sources.json'
    sources = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else {}
    if name not in sources:
        raise ValueError('未找到由 Skill Forge 独立管理的 Codex 版本')
    link = CODEX_ROOT / name
    if link.is_symlink() or getattr(link, 'is_junction', lambda: False)():
        raise ValueError('Codex 目录是链接，拒绝替换')
    dest = safe_paths.resolve_within(CODEX_ROOT, name)
    if not dest.is_dir():
        raise ValueError('Codex 技能目录不存在')
    return registry, sources, dest


def _codex_backup(name):
    backups = CODEX_ROOT.parent / '.skill-forge-backups'
    backups.mkdir(parents=True, exist_ok=True)
    return backups / (name + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))


def update_codex(name, revision=''):
    try:
        with installer._update_lock:
            registry, sources, dest = _codex_entry(name)
            info = sources[name]
            url = _url(info.get('url', ''))
            branch = info.get('branch') or 'main'
            with tempfile.TemporaryDirectory(prefix='sf-codex-update-') as temp:
                repo = Path(temp) / 'repo'
                ok, reason = installer._git_clone(url, repo, branch, strict_branch=True)
                if not ok: raise ValueError('Clone 失败：' + installer._redact(reason))
                if revision: installer._checkout_revision(repo, revision)
                sha = installer._get_latest_sha(repo)
                subpath = info.get('subpath') or ''
                source = (repo / (subpath or '.')).resolve()
                if source.is_relative_to(repo.resolve()) and not (source / 'SKILL.md').is_file() and revision and installer._same_skill_root(dest, repo):
                    source, subpath = repo.resolve(), ''
                if not source.is_relative_to(repo.resolve()) or not (source / 'SKILL.md').is_file():
                    raise ValueError('登记的 Codex 技能目录不存在')
                if not sha: raise ValueError('无法确认新版本')
                with tempfile.TemporaryDirectory(prefix='.sf-update-', dir=CODEX_ROOT) as stage:
                    staged = Path(stage) / name
                    _copy_module(source, staged)
                    verdict = precheck.verify_repo(staged, url, skills_dir=CODEX_ROOT)
                    if not verdict.get('ok'):
                        return {'ok':False, 'error':verdict.get('summary', '预检未通过'), 'verify':verdict}
                    backup = _codex_backup(name)
                    dest.rename(backup)
                    installed = False
                    try:
                        staged.rename(dest)
                        installed = True
                        sources[name] = dict(info, installed_sha=sha, subpath=subpath,
                            updated_at=datetime.now().isoformat(timespec='seconds'),
                            trust_level=installer._trust_level(verdict), trust_note=installer._trust_note(verdict))
                        _write_registry(registry, sources)
                    except Exception:
                        if installed: shutil.rmtree(dest)
                        if not os.path.lexists(dest): backup.rename(dest)
                        raise
                    try:
                        from daemon.version_history import write_backup_metadata
                        write_backup_metadata(backup, info)
                    except OSError:
                        pass
                return {'ok':True, 'name':name, 'tool':'codex', 'sha':sha,
                        'backup':str(backup), 'verify':verdict}
    except Exception as exc:
        return {'ok':False, 'error':installer._redact(str(exc))}


def uninstall_codex(name):
    try:
        with installer._update_lock:
            registry, sources, dest = _codex_entry(name)
            backup = _codex_backup(name)
            dest.rename(backup)
            try:
                del sources[name]
                _write_registry(registry, sources)
            except Exception:
                backup.rename(dest)
                raise
            return {'ok':True, 'name':name, 'tool':'codex', 'backup':str(backup)}
    except Exception as exc:
        return {'ok':False, 'error':installer._redact(str(exc))}
