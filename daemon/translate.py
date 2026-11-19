#!/usr/bin/env python3
"""
翻译公开来源的简介并复用本地缓存；来源不明的文本不外发。
"""
import json
import re
from datetime import datetime
from pathlib import Path

# 代理不再硬编码 —— 唯一真源是 daemon/net.py
SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
CACHE_FILE = SKILL_ROOT / "daemon" / "translation_cache.json"
LOG_FILE = SKILL_ROOT / "daemon" / "watchdog.log"


def log(msg: str):
    """记录公开简介翻译失败的原因，不记录密钥或原始内容。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [translate] {msg}\n")
    except OSError:
        # 日志写不进去，不该让翻译（连带发现新 skill 的主流程）失败。
        pass


def _load_cache() -> dict:
    """加载翻译缓存"""
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    """保存翻译缓存"""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _has_translation(text):
    return isinstance(text, str) and bool(re.search(r'[\u4e00-\u9fff]', text))


def translate_public_descriptions(rows, config=None, *, cache=None):
    """Caller must establish public provenance before using this shared translator."""
    from daemon import privacy, translate_ai, translate_config
    config = translate_config.load() if config is None else config
    rows = [dict(row) for row in rows]
    pending = []
    for index, row in enumerate(rows):
        original = str(row.get('description') or '').strip()
        row['description_zh'] = ''
        if not original:
            continue
        if _is_chinese(original):
            row['description_zh'] = original
        elif cache is not None and _has_translation(cache.get(original)):
            row['description_zh'] = cache[original]
        else:
            pending.append({'id':index, 'text':privacy.clean_public_description(original)})
    if not pending:
        return rows, ''
    if config.get('backend') in ('google', 'local'):
        return rows, '当前使用本地模式，未缓存的简介显示原文；可在设置中启用 DeepSeek 翻译'
    failures = []
    for start in range(0, len(pending), 4):
        batch = pending[start:start + 4]
        try:
            response = translate_ai.complete(json.dumps({'items':batch}, ensure_ascii=False),
                                             config.get('model') or 'deepseek-flash', purpose='public_descriptions')
            parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$', '', response.strip()))
            if not isinstance(parsed, list):
                raise ValueError('invalid translation')
            allowed = {item['id'] for item in batch}
            completed = set()
            for item in parsed:
                if (isinstance(item, dict) and type(item.get('id')) is int and item['id'] in allowed
                        and _has_translation(item.get('text'))):
                    index = item['id']
                    rows[index]['description_zh'] = item['text'].strip()[:2400]
                    completed.add(index)
                    if cache is not None:
                        cache[str(rows[index]['description']).strip()] = rows[index]['description_zh']
            if completed != allowed:
                failures.append('模型返回的译文不完整')
        except translate_ai.AIBackendError as exc:
            failures.append(str(exc))
            break  # A bad key/model or outage should not repeat for every batch.
        except (ValueError, TypeError, KeyError):
            failures.append('模型返回格式不正确')
    warning = '部分简介翻译不可用，显示原文：' + '；'.join(dict.fromkeys(failures)) if failures else ''
    return rows, warning


def translate_skills(skills: list, report=None) -> list:
    """Translate only descriptions marked public by a trusted fetch boundary."""
    cache = _load_cache()
    public = [row for row in skills if row.get('_description_public') is True]
    translated, warning = translate_public_descriptions(public, cache=cache)
    for row, result in zip(public, translated):
        row['description_zh'] = result['description_zh']
    for row in skills:
        if row.get('_description_public') is True:
            continue
        original = str(row.get('description') or '').strip()
        row['description_zh'] = original if _is_chinese(original) else (
            cache.get(original, '') if _has_translation(cache.get(original)) else '')
    if warning:
        log(warning)
    if report is not None:
        report['warning'] = warning
    if public:
        _save_cache(cache)
    return skills


def _is_chinese(text: str) -> bool:
    """判断文本是否主要是中文"""
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    return chinese_chars > len(text) * 0.3 if text else False
