"""Opt-in manager upgrades from matching official GitHub release packages."""
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from daemon import app_version, credentials, edition

REPOSITORY = 'fushengsan3/skill-forge-gate'
RELEASE_API = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
STATE_PATH = 'templates/app-update.json'
OPERATION_LOCK = threading.Lock()
_state_lock = threading.RLock()
_check_lock = threading.Lock()
_install_lock = threading.Lock()
MAX_ARCHIVE = 64 * 1024 * 1024


class RecoveryError(RuntimeError):
    pass


def _constants(path):
    values = {}
    for node in ast.parse(path.read_text(encoding='utf-8-sig')).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name): values[target.id] = node.value.value
    return values


def _identity(root):
    root = Path(root)
    version_file, edition_file = root/'daemon/app_version.py', root/'daemon/edition.py'
    return ((_constants(version_file).get('VERSION') if version_file.exists() else app_version.VERSION),
            (_constants(edition_file).get('ID') if edition_file.exists() else edition.ID))


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d{4}\.\d{2}\.\d{2}(?:\.\d+)?', value):
        raise ValueError('发布版本号格式无效')
    return tuple(int(x) for x in value.lstrip('v').split('.'))


def _load(root):
    try:
        data = json.loads((Path(root)/STATE_PATH).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError): return {}


def _save(root, **updates):
    with _state_lock:
        data = _load(root)
        data.update(updates)
        path = Path(root)/STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
        try: os.replace(output.name, path)
        finally: Path(output.name).unlink(missing_ok=True)
    return data


def status(root):
    current, distribution = _identity(root)
    data = _load(root)
    release = data.get('release') or {}
    available = bool(release.get('tag') and _version(release['tag']) > _version(current))
    return {'ok': True, 'current_version': current, 'edition': distribution,
            'available': available, 'latest_version': release.get('version', ''), 'tag': release.get('tag', ''),
            'notes': release.get('notes', ''), 'release_url': release.get('url', ''),
            'checked_at': data.get('checked_at', ''), 'checking': data.get('checking', False),
            'deferred': data.get('deferred', ''), 'phase': data.get('phase', 'idle'),
            'error': data.get('error', ''), 'install_error': data.get('install_error', ''),
            'restart_required': data.get('restart_required', False)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def _fetch_release():
    headers = {'User-Agent':'skill-forge-updater', 'Accept':'application/vnd.github+json'}
    token = credentials.get_secret(credentials.GITHUB_TOKEN)
    if token: headers['Authorization'] = 'Bearer ' + token
    # The credential is only sent to this fixed official API, never to a CDN.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(urllib.request.Request(RELEASE_API, headers=headers), timeout=20) as response:
        return json.loads(response.read(2 * 1024 * 1024).decode('utf-8'))


def _release_info(data, distribution):
    if not isinstance(data, dict) or distribution not in ('full', 'coreless'):
        raise ValueError('发布信息无效')
    tag = data.get('tag_name', '')
    _version(tag)
    if data.get('draft') or data.get('prerelease'): raise ValueError('没有可用的正式发布版本')
    filename = 'skill-forge-gate-' + distribution + '.zip'
    asset = next((a for a in data.get('assets', []) if isinstance(a, dict) and a.get('name') == filename), None)
    if not asset: raise ValueError('新版尚未提供对应版本的安装包')
    expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + filename
    if asset.get('browser_download_url') != expected: raise ValueError('发布包地址不匹配')
    return {'tag':tag, 'version':tag.lstrip('v'), 'notes':str(data.get('body') or '')[:12000],
            'url':'https://github.com/' + REPOSITORY + '/releases/tag/' + tag,
            'asset_url':expected, 'digest':asset.get('digest') or '', 'size':asset.get('size', 0)}


def check(root, force=False):
    if not _check_lock.acquire(blocking=False): return status(root)
    try:
        data = _load(root)
        if not force and time.time() - data.get('checked_epoch', 0) < 900: return status(root)
        _save(root, checking=True)
        release = _release_info(_fetch_release(), _identity(root)[1])
        _save(root, release=release, checking=False, error='', checked_epoch=time.time(),
              checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    except urllib.error.HTTPError as exc:
        reason = {401:'GitHub 令牌无效，请在设置中重新保存',403:'GitHub 拒绝访问或请求限流',
                  404:'尚无正式发布版本',429:'GitHub 请求限流'}.get(exc.code,'GitHub 服务暂不可用')
        _save(root, checking=False, error=f'{reason}（HTTP {exc.code}）')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _save(root, checking=False, error=str(exc) if isinstance(exc, ValueError) else '检查失败，请检查网络后重试')
    finally: _check_lock.release()
    return status(root)


def check_async(root):
    _save(root, checking=True)
    thread = threading.Thread(target=check, args=(root, True), daemon=True)
    thread.start()
    return thread


def defer(root, tag):
    if tag != status(root)['tag']: return {'ok':False, 'error':'版本信息已变化，请重新检查'}
    _save(root, deferred=tag)
    return status(root)


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
            raise ValueError('安装包重定向地址无效')
        if parsed.username or parsed.password: raise ValueError('安装包地址无效')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(release, destination):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _ReleaseRedirect())
    request = urllib.request.Request(release['asset_url'], headers={'User-Agent':'skill-forge-updater'})
    digest, size = hashlib.sha256(), 0
    with opener.open(request, timeout=30) as response, open(destination, 'wb') as output:
        while chunk := response.read(128 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE: raise ValueError('安装包超过大小限制')
            digest.update(chunk); output.write(chunk)
    if release.get('size') and size != release['size']: raise ValueError('安装包下载不完整')
    if release.get('digest') and release['digest'] != 'sha256:' + digest.hexdigest():
        raise ValueError('安装包校验失败，请重新下载')


def _extract(package, destination):
    from daemon.safe_paths import check_name
    with zipfile.ZipFile(package) as archive:
        members = archive.infolist()
        if len(members) > 2000 or sum(x.file_size for x in members) > MAX_ARCHIVE * 2:
            raise ValueError('安装包内容超过限制')
        seen = set()
        for item in members:
            parts = PurePosixPath(item.filename).parts
            if not parts or parts[0] != 'skill-forge' or '\\' in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('安装包包含无效路径')
            for part in parts: check_name(part)
            rel = '/'.join(parts)
            if rel.casefold() in seen: raise ValueError('安装包包含重复路径')
            seen.add(rel.casefold())
            target = destination.joinpath(*parts)
            if not target.resolve().is_relative_to(destination.resolve()): raise ValueError('安装包路径越界')
            if item.is_dir(): target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('wb') as output: shutil.copyfileobj(source, output)
    return destination/'skill-forge'


def refresh_panel(root):
    from daemon.safe_embed import json_for_script
    from daemon.bridge_auth import KEY_PLACEHOLDER, get_or_create_key
    root = Path(root)
    target = root/'discover/latest.html'
    payload, key = {'categories':{}}, None
    if target.exists():
        previous = target.read_text(encoding='utf-8')
        payload = json.JSONDecoder().raw_decode(previous.split('const DISCOVER_DATA = ', 1)[1])[0]
        key = json.JSONDecoder().raw_decode(previous.split('const BRIDGE_KEY = ', 1)[1])[0]
    payload['_app_update'] = status(root)
    payload['_edition'] = {'id':_identity(root)[1], 'name':'无核版' if _identity(root)[1] == 'coreless' else '完整版',
                           'security_checks':_identity(root)[1] != 'coreless'}
    template = (root/'panel/fallback.html').read_text(encoding='utf-8')
    html = template.replace('/* __DATA_PLACEHOLDER__ */', json_for_script(payload)).replace(KEY_PLACEHOLDER, json_for_script(key or get_or_create_key(root=root)))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding='utf-8')


def apply_package(root, package, tag):
    from scripts import deploy
    root = Path(root).resolve()
    backup = []
    with tempfile.TemporaryDirectory(prefix='skill-forge-update-') as temp:
        source = _extract(package, Path(temp))
        version, distribution = _identity(source)
        if version != tag.lstrip('v') or distribution != _identity(root)[1]:
            raise ValueError('安装包版本或类型与当前选择不符')
        required = ('SKILL.md','daemon/queue_bridge.py','daemon/app_version.py','daemon/edition.py','panel/fallback.html','scripts/data-paths.txt')
        if not all((source/rel).is_file() for rel in required): raise ValueError('安装包文件不完整')
        if distribution == 'coreless' and any((source/name).exists() for name in ('verify','sandbox')):
            raise ValueError('无核版安装包包含其他版本文件')
        for file in source.rglob('*.py'): compile(file.read_text(encoding='utf-8-sig'), str(file.relative_to(source)), 'exec')
        # Both the old and new data lists are preserved during migration.
        data_file = source/'scripts/data-paths.txt'
        old_data = root/'scripts/data-paths.txt'
        if old_data.exists():
            lines = data_file.read_text(encoding='utf-8').splitlines() + old_data.read_text(encoding='utf-8').splitlines()
            data_file.write_text('\n'.join(dict.fromkeys(lines)) + '\n', encoding='utf-8')
        try:
            result = deploy.deploy(source, root, on_archive=lambda path: backup.append(Path(path)))
            if result['mismatched']: raise OSError('安装文件校验失败')
            registry = root/'sources.json'
            commit = _constants(source/'daemon/app_version.py').get('COMMIT', '')
            if registry.exists() and re.fullmatch(r'[a-f0-9]{40}', commit):
                records = json.loads(registry.read_text(encoding='utf-8'))
                for record in records.values():
                    if isinstance(record, dict) and record.get('self'):
                        record.update(installed_sha=commit, updated_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
                registry.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
            _save(root, phase='ready', restart_required=True, install_error='', deferred='')
            refresh_panel(root)
        except (Exception, SystemExit):
            if backup:
                try: deploy.restore_snapshot(backup[0]/'snapshot', root)
                except (Exception, SystemExit) as exc:
                    raise RecoveryError('升级失败且自动恢复未完成，请使用 .backup 中的升级前备份恢复') from exc
            raise
    return status(root)


def start_install(root, tag, on_ready=None):
    if not _install_lock.acquire(blocking=False): return {'ok':False,'error':'正在升级，请稍候'}
    if not OPERATION_LOCK.acquire(blocking=False):
        _install_lock.release()
        return {'ok':False,'error':'正在执行安装或管理操作，请完成后再升级'}
    current = status(root)
    if not current['available'] or tag != current['tag']:
        OPERATION_LOCK.release(); _install_lock.release()
        return {'ok':False,'error':'没有已确认的新版本，请重新检查'}
    _save(root, phase='downloading', install_error='', restart_required=False)

    def worker():
        ready = False
        try:
            # Revalidate the exact offered release; never silently install a newer offer.
            fresh = _release_info(_fetch_release(), _identity(root)[1])
            if fresh['tag'] != tag: raise ValueError('发布版本已变化，请重新检查并选择')
            with tempfile.TemporaryDirectory(prefix='skill-forge-download-') as temp:
                package = Path(temp)/'update.zip'
                _download(fresh, package)
                _save(root, phase='installing')
                apply_package(root, package, tag)
            ready = True
        except (Exception, SystemExit) as exc:
            message = str(exc) if isinstance(exc, RecoveryError) else '升级未完成，当前版本已保留。' + (
                str(exc) if isinstance(exc, ValueError) else '请检查网络或文件占用后重试。')
            _save(root, phase='failed', install_error=message)
        finally:
            _install_lock.release()
            if not ready: OPERATION_LOCK.release()
        if ready:
            if on_ready: on_ready()
            else: OPERATION_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()
    return status(root)


def mark_started(root):
    if _load(root).get('phase') == 'ready':
        _save(root, phase='idle', restart_required=False, install_error='')
