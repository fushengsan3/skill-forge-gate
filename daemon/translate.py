#!/usr/bin/env python3
"""
翻译公开来源的简介并复用本地缓存；来源不明的文本不外发。
"""
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# 代理不再硬编码 —— 唯一真源是 daemon/net.py
SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
CACHE_FILE = SKILL_ROOT / "daemon" / "translation_cache.json"
LOG_FILE = SKILL_ROOT / "daemon" / "watchdog.log"


def log(msg: str):
    """记一行到与 installer / watchdog 同一个日志文件。

    **这个模块以前一处日志都没有**，而它有三种结果长得一模一样：
    AI 成功、AI 失败后回退 Google、两个后端都失败后留空。
    用户选了 AI、实际每次都在走 Google —— 在扫描报告里完全看不出来。
    """
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] [translate] {msg}\n")
    except OSError:
        # 日志写不进去，不该让翻译（连带发现新 skill 的主流程）失败。
        pass


def _api_translate(text: str) -> str:
    """调用 Google Translate API 翻译单条文本"""
    from daemon import privacy
    if not privacy.allow_unverified_translation():
        return ""
    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text, safe="")
    from daemon import net
    opener = net.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "skill-forge/1.0"})

    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            # API 返回格式: [[["译文","原文",...],...],null,"en"]
            data = json.loads(raw)
            sentences = []
            for segment in data[0]:
                if segment[0]:
                    sentences.append(segment[0])
            return "".join(sentences)
    except Exception as e:
        # 只记异常的**类型**，不记 e 全文 —— 理由同 translate_ai.py:107：
        # 有些库会把请求头（含密钥）印进错误里。
        # 以前这里是完全无声的：`return ""` 与"这条描述本来就是空的"无法区分。
        log(f"Google 后端失败：{type(e).__name__}")
        return ""


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


def _translate_one(text: str, cfg: dict):
    """按配置选后端翻译一条。

    返回 (译文, 实际用的后端)。译文为空表示两个后端都没成，调用方留空即可。

    **AI 失败必须回退到 Google**，这是刻意的：AI 后端会因为密钥过期、
    余额不足、模型下线、网络不通等原因失败，而这些都不该让整个扫描
    （连带发现新 skill 的主流程）跟着失败。
    """
    from daemon import privacy
    if not privacy.allow_unverified_translation():
        return "", "local"
    if cfg.get("backend") == "ai":
        try:
            from daemon import translate_ai
            return translate_ai.translate(text, cfg.get("model", ""), cfg.get("prompt", "")), "ai"
        except Exception as e:
            # 回退不往上抛 —— 见上面的理由。但**要留一行痕迹**：
            # "选了 AI、其实走了 Google"以前在任何输出里都看不出来。
            log(f"AI 后端失败，本轮回退 Google：{type(e).__name__}")
        return _api_translate(text), "google"

    return _api_translate(text), "google"


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
