#!/usr/bin/env python3
"""
翻译后端测试（R6）。

重点是**回退**：AI 后端会因为密钥过期、余额不足、模型下线、网络不通而失败，
这些都不该让整个扫描（连带"发现新 skill"的主流程）跟着失败。
所以 AI 挂了必须静默退回 Google，而不是抛上去。

全程 mock，不发任何真实请求。

用法：
    python tests/test_translate_backend.py
"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import translate, translate_config, translate_ai, translate_prompts

errors = []
passed = []


def check(ok, label, detail=""):
    (passed if ok else errors).append(label + (f" — {detail}" if detail else ""))


def skill(name, desc):
    return {"name": name, "description": desc}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sf-tr-"))
    cache_file = tmp / "cache.json"
    cfg_file = tmp / "translate-config.json"

    # 提示词目录也要指向**仓库**而不是运行时：
    # daemon 里写死的 SKILL_ROOT 是运行时路径，而测试跑的是仓库里的代码。
    # 不重定向的话，这个测试会变成"测部署状态"，而不是测代码。
    prompt_dir = ROOT / "templates" / "translate_prompts"
    if not prompt_dir.exists():
        print(f"FAIL: 仓库里找不到提示词目录 {prompt_dir}")
        return 1

    with patch.object(translate, "CACHE_FILE", cache_file), \
         patch.object(translate_config, "CONFIG_FILE", cfg_file), \
         patch.object(translate_prompts, "PROMPT_DIR", prompt_dir), \
         patch.object(translate_prompts, "MANIFEST", prompt_dir / "manifest.json"):
        run_checks(cache_file, cfg_file)

    print("=" * 60)
    print("翻译后端测试（R6）")
    print("=" * 60)
    for p in passed:
        print("  [PASS] " + p)
    for e in errors:
        print("  [FAIL] " + e)
    print("")
    print(f"总计 {len(passed) + len(errors)} 项，失败 {len(errors)} 项")
    print("RESULT: " + ("PASS" if not errors else "FAIL"))
    return 0 if not errors else 1


def run_checks(cache_file, cfg_file):
    # ---- 1. 配置：默认值 / 校验 ----
    cfg = translate_config.load()
    check(cfg["backend"] == "google", "1 配置缺失时默认 google", str(cfg))

    translate_config.save({"backend": "ai", "model": "m1"})
    check(translate_config.load()["backend"] == "ai", "2 能保存并读回 backend")

    translate_config.save({"backend": "不存在的后端"})
    check(translate_config.load()["backend"] == "google",
          "3 非法后端名被退回默认（配置文件可被手改，不能让它静默失效）")

    # 上一行刚把 model 设成 'm1'；这里传一个非字符串的 model，
    # 正确行为是**忽略它、保留原值**，而不是把它写进去、也不是清空
    translate_config.save({"backend": "ai", "恶意字段": "x", "model": 123})
    after = translate_config.load()
    check("恶意字段" not in after and after["model"] == "m1",
          "4 只接受已知字段；非字符串的 model 被忽略且不破坏原值", str(after))

    # ---- 2. 提示词：正文非空 + 回退 ----
    body = translate_prompts.get_prompt("skill-forge-tech")
    check(len(body) > 200, "5 内置提示词正文能读到", f"{len(body)} 字符")
    check("只输出译文" in body, "6 正文含【只输出译文】这条铁律")

    fallback = translate_prompts.get_prompt("不存在的id")
    check(fallback == body, "7 未知 id 回退到默认提示词")
    check(len(translate_prompts.get_prompt("")) > 0, "8 空 id 也能拿到可用正文")

    listed = translate_prompts.list_prompts()
    check(len(listed["prompts"]) >= 2, "9 清单里至少两个提示词", str(len(listed["prompts"])))
    check(all("file" not in p for p in listed["prompts"]),
          "10 清单不暴露内部文件路径")
    check(all(p["license"] for p in listed["prompts"]),
          "11 每个提示词都有许可证标注（引入第三方提示词的前置条件）")
    check(all(p["available"] for p in listed["prompts"]),
          "12 清单里的提示词文件都真实存在")

    # ---- 3. 输出校验：模型加壳必须被拒 ----
    for bad in ["以下是翻译：你好", "译文如下：你好", "```\n你好\n```",
                "Here is the translation: hi", "你好。希望对你有帮助"]:
        check(translate_ai._looks_wrapped(bad), f"13 拒绝加壳输出: {bad[:14]}")
    for good in ["你好世界", "这是一个用于代码审查的 skill"]:
        check(not translate_ai._looks_wrapped(good), f"14 正常译文不被误拒: {good}")

    # ---- 4. AI 不可用时的行为 ----
    with patch.object(translate_ai.credentials, "get_secret", return_value=None):
        try:
            translate_ai.translate("hello", "m1")
            check(False, "15 无密钥时应抛 AIBackendError")
        except translate_ai.AIBackendError as e:
            check("密钥" in str(e), "15 无密钥时抛 AIBackendError", str(e))

    # ---- 5. 分发：google 后端 ----
    calls = []

    def fake_google(text):
        calls.append(("google", text))
        return "中文:" + text

    translate_config.save({"backend": "google"})
    skills = [skill("a", "hello world"), skill("b", "纯中文描述")]
    with patch.object(translate, "_api_translate", side_effect=fake_google), \
         patch("time.sleep", return_value=None):
        translate.translate_skills(skills)
    check(skills[0]["description_zh"] == "中文:hello world", "16 google 后端正常翻译")
    check(skills[1]["description_zh"] == "纯中文描述", "17 纯中文条目直接复用，不调接口")
    check(len(calls) == 1, "18 只对需要翻译的条目调接口", f"{len(calls)} 次")

    # ---- 6. 分发：AI 后端成功路径 ----
    cache_file.unlink(missing_ok=True)
    translate_config.save({"backend": "ai", "model": "m1", "prompt": "skill-forge-tech"})
    seen = {}

    def fake_ai(text, model, prompt_id=""):
        seen["model"] = model
        seen["prompt"] = prompt_id
        return "AI译:" + text

    with patch.object(translate_ai, "translate", side_effect=fake_ai), \
         patch.dict(sys.modules, {"daemon.translate_ai": translate_ai}), \
         patch("time.sleep", return_value=None):
        # 让 _translate_one 内部 import 到的就是被 patch 过的那个模块
        s2 = [skill("c", "ai backend test")]
        translate.translate_skills(s2)
    check(s2[0]["description_zh"] == "AI译:ai backend test",
          "19 AI 后端成功时用 AI 译文", s2[0]["description_zh"])
    check(seen.get("model") == "m1", "20 AI 后端收到配置里的模型")
    check(seen.get("prompt") == "skill-forge-tech", "21 AI 后端收到配置里的提示词")

    # ---- 7. 关键：AI 失败必须回退 google，而不是让扫描失败 ----
    cache_file.unlink(missing_ok=True)
    translate_config.save({"backend": "ai", "model": "m1"})
    google_calls = []

    def fake_google2(text):
        google_calls.append(text)
        return "兜底:" + text

    def boom(*a, **k):
        raise translate_ai.AIBackendError("余额不足")

    with patch.object(translate_ai, "translate", side_effect=boom), \
         patch.object(translate, "_api_translate", side_effect=fake_google2), \
         patch("time.sleep", return_value=None):
        s3 = [skill("d", "fallback test")]
        translate.translate_skills(s3)
    check(s3[0]["description_zh"] == "兜底:fallback test",
          "22 AI 失败时静默回退 google，扫描不受影响", s3[0]["description_zh"])
    check(len(google_calls) == 1, "23 回退确实调用了 google", f"{len(google_calls)} 次")

    # ---- 8. 缓存 ----
    cache_file.unlink(missing_ok=True)
    translate_config.save({"backend": "google"})
    n = []

    def counting(text):
        n.append(text)
        return "计:" + text

    with patch.object(translate, "_api_translate", side_effect=counting), \
         patch("time.sleep", return_value=None):
        translate.translate_skills([skill("e", "cache me")])
        translate.translate_skills([skill("e", "cache me")])
    check(len(n) == 1, "24 第二条命中缓存，不重复调用", f"调了 {len(n)} 次")

    # ---- 9. 缓存不会被脏译文污染 ----
    cache_file.unlink(missing_ok=True)
    translate_config.save({"backend": "ai", "model": "m1"})

    def wrapped(*a, **k):
        raise translate_ai.AIBackendError("模型输出带解释")

    with patch.object(translate_ai, "translate", side_effect=wrapped), \
         patch.object(translate, "_api_translate", return_value=""), \
         patch("time.sleep", return_value=None):
        s4 = [skill("f", "dirty")]
        translate.translate_skills(s4)
    import json
    cached = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    check(s4[0]["description_zh"] == "" and not cached,
          "25 两个后端都失败时留空，且不写缓存", f"缓存={cached}")


if __name__ == "__main__":
    sys.exit(main())
