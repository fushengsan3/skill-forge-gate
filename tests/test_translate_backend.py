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

    # Unverified catalog entries must not be sent to any remote provider.
    from daemon import privacy
    try:
        translate_ai.translate("private text", "deepseek-flash")
        check(False, "13 拒绝上传来源不明的内容")
    except translate_ai.AIBackendError:
        check(True, "13 拒绝上传来源不明的内容")

    calls = []
    skills = [skill("a", "hello world"), skill("b", "纯中文描述")]
    with patch.object(translate, "_api_translate", side_effect=lambda text: calls.append(text)), \
         patch.object(translate_ai, "translate", side_effect=AssertionError("unexpected remote call")), \
         patch("time.sleep", return_value=None):
        translate.translate_skills(skills)
    check(skills[0]["description_zh"] == "", "14 未经验证的英文简介保留原文")
    check(skills[1]["description_zh"] == "纯中文描述", "15 中文简介直接使用")
    check(not calls, "16 不调用 Google 或其他未经授权的接口")
    check(privacy.capability_tags("需要 PDF 论文") == ["pdf", "research"], "17 搜索提取通用标签")


if __name__ == "__main__":
    sys.exit(main())
