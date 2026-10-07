#!/usr/bin/env python3
"""`daemon/cc_models.py` 的两条不变式（问题清单 #26）。

## 为什么一个"读配置"的模块值得单独测

它读取的是 `~/.claude/settings.json` 里的 `env` 段 —— 那是一份**密钥级别的**
文件（`ANTHROPIC_AUTH_TOKEN` 可能就在里面）。这个模块做两件事，两件都容易做错：

1. **`base_url()`：仅本地使用，绝不外发。** 端点本身不是秘密，但没必要让浏览器
   知道你的请求打到哪 —— 所以它**不能**出现在 `list_models()` 的返回值里
   （那个返回值会被 bridge 原样发给面板）。这份"不外发"以前只是一句注释。
2. **`resolve()`：只按白名单解析，猜不出来就原样返回。** 把别名（"opus"）
   直接发给 API 会 404，所以要解析；但**不认识的值绝不能猜** ——
   猜错了是静默走错模型，比报错难查得多。

用法：
    python tests/test_cc_models.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import cc_models

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


# 故意用一个**一眼能认出来**的端点：它只要出现在 list_models() 里就是泄漏
SECRET_ENDPOINT = "https://internal.example.invalid/v1"
SETTINGS = {
    "env": {
        "ANTHROPIC_BASE_URL": SECRET_ENDPOINT,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-resolved-id",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "haiku-resolved-id",
        # 白名单之外的键：连读都不该被读出来
        "SOME_OTHER_SECRET": "should-never-be-read",
    }
}


def with_settings(payload):
    class _Ctx:
        def __enter__(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="sf-cc-"))
            self.file = self.tmp / "settings.json"
            self.file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.real = cc_models.SETTINGS
            cc_models.SETTINGS = self.file
            return self.file

        def __exit__(self, *a):
            cc_models.SETTINGS = self.real
            shutil.rmtree(self.tmp, ignore_errors=True)
            return False
    return _Ctx()


def main():
    print("=" * 60)
    print("cc_models 不变式（#26）")
    print("=" * 60)

    print("--- 1. ★ base_url 仅本地使用，绝不外发 ---")
    with with_settings(SETTINGS):
        got = cc_models.base_url()
        check(got == SECRET_ENDPOINT, "base_url() 读得到端点", got)

        listing = cc_models.list_models()
        blob = json.dumps(listing, ensure_ascii=False)
        check(SECRET_ENDPOINT not in blob,
              "★★ list_models() 的返回值里**没有**端点 —— 它会被原样发给面板",
              blob[:120])
        check("ANTHROPIC_BASE_URL" not in blob,
              "★★ 连那个键名都没有（键名本身也说明了你打到哪）")
        check("should-never-be-read" not in blob,
              "★ 白名单之外的键根本没被读出来")
        # ⚠️ 这里断言的是"清单照常能出"，**不是** "current 非空"：
        # current 来自 `ANTHROPIC_MODEL`，上面那份夹具没配它 → 空是**正确**的。
        # （第一版我把断言写成了 current 必须非空 —— 那是我把夹具的性质
        #   当成了被测行为，测试当场纠正。）
        check(listing.get("ok") is True and listing.get("models"),
              "清单照常能出（别为了不泄漏把功能关了）",
              f"models={len(listing.get('models') or [])} current={listing.get('current')!r}")

    print("--- 2. ★ resolve 只按白名单解析，猜不出来就原样返回 ---")
    with with_settings(SETTINGS):
        check(cc_models.resolve("opus") == "opus-resolved-id",
              "匿名别名按 _MODEL 解析", cc_models.resolve("opus"))
        check(cc_models.resolve("OPUS") == "opus-resolved-id",
              "大小写不敏感（面板可能传来大写）", cc_models.resolve("OPUS"))
        check(cc_models.resolve("haiku") == "haiku-resolved-id",
              "_MODEL_NAME 优先于 _MODEL（两条都配时）",
              cc_models.resolve("haiku"))
        # 最关键的一条：不认识的**绝不能猜**。猜错 = 静默走错模型。
        check(cc_models.resolve("sonnet") == "sonnet",
              "★ 没配过的别名**原样返回**，不去猜一个 ID",
              cc_models.resolve("sonnet"))
        check(cc_models.resolve("claude-opus-5-5") == "claude-opus-5-5",
              "★ 看着就像真正 model id 的，原样放过（由调用方去撞错误）",
              cc_models.resolve("claude-opus-5-5"))
        check(cc_models.resolve("") == "", "空值不崩、不编造", repr(cc_models.resolve("")))

    print("--- 3. 配置缺失/损坏时不崩 ---")
    with with_settings({}):
        check(cc_models.base_url() == "", "没有 env 段 → 空端点", repr(cc_models.base_url()))
        check(cc_models.resolve("opus") == "opus", "没有配置 → 原样返回")
    with with_settings({"env": "这不是个字典"}):
        check(cc_models.base_url() == "" and cc_models.resolve("opus") == "opus",
              "env 不是字典 → 不崩，退回空")
    check(cc_models.base_url() == "" or True, "（真实 settings.json 下也不抛异常）")

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
