"""Fetch public skill candidates and rank them without sending personal input."""
import difflib
import json
import re

from daemon import cc_models, privacy, translate_ai, translate_config

MAX_ITEMS = 500
ALIASES = {
    '论文': 'paper research academic literature', '文献': 'literature research paper academic',
    '图片': 'image picture photo graphic', '图像': 'image picture photo',
    '表格': 'spreadsheet excel csv table', '演示': 'presentation slides powerpoint ppt',
    '笔记': 'notes markdown obsidian knowledge', '搜索': 'search find query research',
    '代码': 'code programming developer software', '翻译': 'translate translation language',
}


def _local(query, items):
    q = query.lower().strip()
    expanded = q + ' ' + ' '.join(words for key, words in ALIASES.items() if key in q)
    tokens = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', expanded)
    ranked = []
    for index, item in enumerate(items):
        text = (item['name'] + ' ' + item['description']).lower()
        score = sum(3 if token in text else 0 for token in tokens)
        score += difflib.SequenceMatcher(None, q, item['name'].lower()).ratio()
        if q in text: score += 5
        if score >= 0.55: ranked.append((score, index))
    return [index for _, index in sorted(ranked, key=lambda pair: (-pair[0], pair[1]))[:30]]


def search(query, candidates):
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        return {'ok': False, 'error': '请输入不超过 300 字的需求'}
    if not isinstance(candidates, list) or len(candidates) > 5000:
        return {'ok': False, 'error': '搜索列表格式不正确'}
    items = []
    for candidate in candidates[:MAX_ITEMS]:
        if not isinstance(candidate, dict):
            return {'ok': False, 'error': '搜索列表格式不正确'}
        items.append({'name':str(candidate.get('name', ''))[:100],
                      'description':str(candidate.get('description', ''))[:220]})
    truncated = len(candidates) > MAX_ITEMS
    try:
        config = translate_config.load()
        if config.get('backend') in ('google', 'local'):
            raise translate_ai.AIBackendError('当前使用本地模式')
        model = config.get('model') or 'deepseek-flash'
        if not model:
            raise translate_ai.AIBackendError('未选择 AI 模型')
        request_tags = privacy.capability_tags(query)
        if not request_tags:
            raise translate_ai.AIBackendError('没有可安全发送的通用功能标签')
        payload = json.dumps({'request_tags':request_tags, 'candidates':[
            {'id':i, 'tags':privacy.capability_tags(x['name'] + ' ' + x['description'])}
            for i,x in enumerate(items)]}, ensure_ascii=False)
        response = translate_ai.complete(payload, model, '', purpose='capabilities')
        ids = json.loads(response)
        if not isinstance(ids, list): raise ValueError('模型未返回列表')
        result = []
        for value in ids:
            if type(value) is int and 0 <= value < len(items) and value not in result:
                result.append(value)
            if len(result) >= 30: break
        return {'ok':True, 'mode':'ai', 'indices':result,
                'warning':'仅搜索了前 500 条发现结果' if truncated else ''}
    except (translate_ai.AIBackendError, ValueError, TypeError, KeyError):
        warning = 'AI 排序不可用，已对候选使用本地模糊匹配；请在设置中检查 AI 密钥和模型'
        if truncated: warning += '；仅搜索了前 500 条发现结果'
        return {'ok':True, 'mode':'local', 'indices':_local(query, items), 'warning':warning}


def search_online(query):
    """Search GitHub and configured public sources, then rank newly fetched items."""
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        return {'ok':False, 'error':'请输入不超过 300 字的需求'}
    tags = privacy.capability_tags(query)
    if not tags:
        return {'ok':False, 'error':'未识别出通用功能类型，请加入如 PDF、图片、代码或翻译等关键词'}
    from concurrent.futures import ThreadPoolExecutor
    from daemon import fetcher
    reports = []
    with ThreadPoolExecutor(max_workers=min(len(tags), 3) + 1) as pool:
        futures = [pool.submit(fetcher.search_online, tag) for tag in tags[:3]]
        external = pool.submit(fetcher.fetch_external_sources, with_report=True)
        for future in futures:
            try:
                reports.append(future.result())
            except Exception:
                reports.append({'ok':False, 'skills':[], 'error':'GitHub 连接失败'})
        try:
            ext = external.result()
        except Exception:
            ext = {'skills':[], 'errors':[{'label':'外部源','error':'连接失败'}]}
    if not any(report.get('skills') for report in reports) and not ext.get('skills'):
        try:
            reports.append(fetcher.search_claudskills(tags[:3]))
        except Exception:
            reports.append({'ok':False, 'skills':[], 'error':'公开目录连接失败'})
    candidates, seen = [], set()
    for report in reports + [ext]:
        for item in report.get('skills', []):
            if not isinstance(item, dict): continue
            identity = item.get('url') or item.get('full_name') or item.get('name')
            if not identity or identity in seen: continue
            seen.add(identity)
            candidates.append(item)
    failures = [r.get('error', 'GitHub 连接失败') for r in reports if not r.get('ok')]
    failures.extend((e.get('label') or '外部源') + '：' + (e.get('error') or '连接失败') for e in ext.get('errors', []))
    if not candidates and not any(r.get('ok') for r in reports):
        return {'ok':False, 'error':'在线搜索失败：' + ('；'.join(dict.fromkeys(failures)) or '数据源不可用')}
    if not candidates:
        return {'ok':True, 'scope':'online', 'mode':'local', 'skills':[], 'warning':'；'.join(dict.fromkeys(failures))}
    ranking = search(query, candidates)
    if not ranking.get('ok'):
        return ranking
    selected = [candidates[i] for i in ranking['indices']]
    warnings = list(dict.fromkeys(failures))
    if ranking.get('warning'): warnings.append(ranking['warning'])
    if len(tags) > 3: warnings.append('在线检索使用了前三个通用功能类型')
    return {'ok':True, 'scope':'online', 'mode':ranking['mode'], 'skills':selected,
            'warning':'；'.join(warnings)}
