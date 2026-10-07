#!/usr/bin/env python3
"""
翻译模块 — 通过 Google Translate API 将英文简介批量翻译为中文
使用可乐云代理访问 translate.googleapis.com
"""
import json
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

PROXY = "http://127.0.0.1:7897"
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
    url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text, safe="")
    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
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
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
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


def translate_skills(skills: list) -> list:
    """
    为 skill 列表逐条翻译 description → description_zh
    使用缓存避免重复翻译（daemon 每周跑一次，大部分命中缓存）

    后端由 templates/translate-config.json 决定（面板通过 bridge 写），
    默认 google。见 daemon/translate_config.py。
    """
    import time
    from daemon import translate_config

    cfg = translate_config.load()
    cache = _load_cache()

    # 收集需要翻译的描述
    to_translate = []  # [(index, text), ...]
    for i, s in enumerate(skills):
        desc = (s.get("description") or "").strip()
        if not desc:
            skills[i]["description_zh"] = ""
            continue
        # 纯中文的直接跳过
        if _is_chinese(desc):
            skills[i]["description_zh"] = desc
            continue
        # 查缓存
        if desc in cache:
            skills[i]["description_zh"] = cache[desc]
            continue
        to_translate.append((i, desc))

    if not to_translate:
        return skills

    # 逐条翻译（避免批量分隔符被翻译接口破坏）
    for idx, original in to_translate:
        translated, used = _translate_one(original, cfg)
        if translated:
            skills[idx]["description_zh"] = translated
            cache[original] = translated
        else:
            skills[idx]["description_zh"] = ""
        # 只有 Google 需要这个间隔（它有免费额度的限流）。
        # AI 后端是计费的，没必要拖慢。
        if used == "google":
            time.sleep(0.3)

    # 留空也是一种结果，不能只有"没报错"这一个信号
    blank = sum(1 for i, _ in to_translate if not (skills[i].get("description_zh") or "").strip())
    if blank:
        log(f"有 {blank}/{len(to_translate)} 条两个后端都没译出来，description_zh 已留空")

    _save_cache(cache)
    return skills


def _is_chinese(text: str) -> bool:
    """判断文本是否主要是中文"""
    chinese_chars = sum(1 for c in text if '一' <= c <= '鿿')
    return chinese_chars > len(text) * 0.3 if text else False
