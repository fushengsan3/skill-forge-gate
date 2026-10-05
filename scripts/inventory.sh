#!/bin/bash
# 列出所有已安装 skill，检查更新状态
# 用法: inventory.sh [--check-remote]
set -euo pipefail

export https_proxy="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"

SKILLS_ROOT="${SKILLS_ROOT:-$HOME/.claude/skills}"
SKILL_FORGE="$SKILLS_ROOT/skill-forge"
SOURCES_FILE="$SKILL_FORGE/sources.json"
CHECK_REMOTE=false
[[ "${1:-}" == "--check-remote" ]] && CHECK_REMOTE=true

if [[ ! -f "$SOURCES_FILE" ]]; then
    echo '{"error": "sources.json 不存在，请先安装至少一个 skill"}'
    exit 3
fi

# 将 CHECK_REMOTE 传入 Python（0=false, 1=true）
CHECK_FLAG="0"
[[ "$CHECK_REMOTE" == true ]] && CHECK_FLAG="1"

python3 -c "
import json, subprocess, sys
from pathlib import Path
from subprocess import TimeoutExpired

data = json.loads(open('$SOURCES_FILE').read())
results = []
check_remote = bool(int('$CHECK_FLAG'))

for name, info in data.items():
    entry = {
        'name': name,
        'type': info.get('type', 'unknown'),
        'self': info.get('self', False),
        'installed_sha': info.get('installed_sha', 'unknown'),
        'installed_at': info.get('installed_at', ''),
    }

    skill_path = Path('$SKILLS_ROOT') / name

    if skill_path.exists():
        entry['path'] = str(skill_path)
        entry['installed'] = True

        # 检查本地是否有未提交修改
        if (skill_path / '.git').exists():
            try:
                status = subprocess.run(
                    ['git', '-C', str(skill_path), 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=10
                )
                entry['local_changes'] = bool(status.stdout.strip())
            except (TimeoutExpired, Exception):
                entry['local_changes'] = False
    else:
        entry['installed'] = False

    # 远程检查（仅在指定 --check-remote 时执行）
    if check_remote and info.get('type') == 'remote':
        url = info.get('url', '')
        branch = info.get('branch', 'main')

        if url:
            repo_url = url.split('/tree/')[0] if '/tree/' in url else url
            repo_url = repo_url.rstrip('/').replace('https://github.com/', '')
            if '/' not in repo_url:
                repo_url = f'{repo_url}/{repo_url}'

            owner, repo = repo_url.split('/')[:2]
            remote_url = f'https://github.com/{owner}/{repo}.git'

            try:
                result = subprocess.run(
                    ['git', 'ls-remote', remote_url, f'refs/heads/{branch}'],
                    capture_output=True, text=True, timeout=15
                )

                if result.returncode == 0 and result.stdout.strip():
                    latest_sha = result.stdout.split()[0]
                    entry['latest_sha'] = latest_sha

                    if info.get('installed_sha') == latest_sha:
                        entry['status'] = 'up_to_date'
                    else:
                        entry['status'] = 'update_available'
                else:
                    entry['status'] = 'unknown'
            except TimeoutExpired:
                entry['status'] = 'unknown'
                entry['remote_error'] = '网络超时'
            except Exception as e:
                entry['status'] = 'unknown'
                entry['remote_error'] = str(e)[:100]

    results.append(entry)

print(json.dumps({'total': len(results), 'skills': results}, ensure_ascii=False, indent=2))
"
