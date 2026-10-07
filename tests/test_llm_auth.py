#!/usr/bin/env python3
"""
`verify/llm_auth.py` 测试 —— L4 深度分析和 L5 沙箱共用的那份凭据/端点/取值判定。

## 为什么单独给这个模块写一套

它是 2026-10-05 那轮审计里**最隐蔽的一条**的修复处：`ANTHROPIC_AUTH_TOKEN`
这类变量只存在于 Claude Code 的 shell 进程里（User / Machine 两级环境变量都查不到
任何一个 `ANTHROPIC*`），而面板一键安装跑在 bridge 这个常驻进程里 ——
于是那条路上 `credentials()` 永远返回空，**L5 一次都没跑过，而且看起来一切正常**
（"前置缺失，跳过"，不是失败）。

这套测试钉的就是"谁是赢家"这件事。判定顺序如果被谁顺手改了，
这里必须红 —— 因为它决定的是"L5 到底跑不跑"，而跑不跑是看不出来的。

用法：
    python tests/test_llm_auth.py
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from verify import llm_auth

results = []

# 一个**一眼能认出来**的假密钥。断言"它没有出现在某处"时，普通字符串
# 容易被别的输出碰巧命中，用这个就不会有假阴性。
#
# ⚠️ **故意不加 `sk-ant-` 前缀。** 第一版写成了 `sk-ant-SENTINEL-...`，
# 结果 `tests/test_no_local_leakage.py` 把它当成真 Anthropic key 报了出来 ——
# 那个守卫是对的（它按前缀扫描，而这个文件是被跟踪、会被推送的）。
# 别为了"更像真的"把前缀加回来：那会让仓库自带的推送前守卫永远红着，
# 而一个永远红着的守卫等于没有守卫。
SENTINEL = "SENTINEL-DO-NOT-LEAK-9f3a2b"


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class env:
    """临时替换环境变量和两个打桩点，退出时原样还原。

    用 `with` 而不是在每个用例里手写 save/restore：漏还原一次，
    后面的用例就在一个脏环境里跑，而那种红看起来像是"功能坏了"。
    """

    def __init__(self, cm=None, cm_note="", env_vars=None, cc=None):
        self.cm = cm
        self.cm_note = cm_note
        self.env_vars = env_vars if env_vars is not None else {}
        self.cc = cc
        self._saved = {}

    def __enter__(self):
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
            self._saved[name] = os.environ.get(name)
            os.environ.pop(name, None)
        for k, v in self.env_vars.items():
            if v is not None:
                os.environ[k] = v

        self._saved["_cm"] = llm_auth._credential_manager_lookup
        self._saved["_cc"] = llm_auth._cc_models

        def fake_cm():
            # 真函数的第二个值是**故障描述**，正常时是空串（来源名不在这里）。
            if self.cm:
                return self.cm, ""
            return "", self.cm_note
        llm_auth._credential_manager_lookup = fake_cm
        llm_auth._cc_models = lambda: self.cc
        return self

    def __exit__(self, *exc):
        llm_auth._credential_manager_lookup = self._saved["_cm"]
        llm_auth._cc_models = self._saved["_cc"]
        for name, old in self._saved.items():
            if name.startswith("_"):
                continue
            if old is not None:
                os.environ[name] = old
            else:
                os.environ.pop(name, None)
        return False


class FakeCC:
    """假的 `daemon.cc_models`。"""

    def __init__(self, base="", resolved=None, raises=False):
        self._base = base
        self._resolved = resolved
        self._raises = raises

    def base_url(self):
        if self._raises:
            raise RuntimeError("配置读不动")
        return self._base

    def resolve(self, alias):
        if self._raises:
            raise RuntimeError("配置读不动")
        # 真 `resolve()` 认不出来时把输入**原样返回** —— 这里也照抄，
        # 否则那段"原样返回就不算数"的守卫测不到。
        return self._resolved if self._resolved is not None else alias


def run_checks():
    print("--- 1. 凭据优先级：凭据管理器 > API_KEY > AUTH_TOKEN ---")
    # 凭据管理器排第一不是审美问题：它是**唯一跨进程可用**的来源。
    # 面板那条路上的 bridge 是常驻进程，继承的是登录环境，看不到注入的环境变量。

    with env(cm=SENTINEL) as _:
        token, style, source = llm_auth.credentials()
        check(token == SENTINEL, "只有凭据管理器 → 用它", source)
        check(style == "x-api-key",
              "★ 凭据管理器那条走 x-api-key（跟 translate_ai.py 同一个口令槽，同一种发法）",
              style)
        check("凭据管理器" in source, "来源名说得出是凭据管理器", source)

    with env(cm=SENTINEL, env_vars={"ANTHROPIC_API_KEY": "env-api-key",
                                    "ANTHROPIC_AUTH_TOKEN": "env-auth-token"}):
        token, style, source = llm_auth.credentials()
        check(token == SENTINEL,
              "★ 三者都在时凭据管理器赢 —— 必须是**确定**的赢家，不能靠碰巧读到哪个",
              source)

    with env(env_vars={"ANTHROPIC_API_KEY": "env-api-key",
                       "ANTHROPIC_AUTH_TOKEN": "env-auth-token"}):
        token, style, source = llm_auth.credentials()
        check(token == "env-api-key" and style == "x-api-key",
              "API_KEY 胜过 AUTH_TOKEN（语义无歧义的那个优先）", f"{source}/{style}")

    with env(env_vars={"ANTHROPIC_AUTH_TOKEN": "env-auth-token"}):
        token, style, source = llm_auth.credentials()
        check(token == "env-auth-token" and style == "bearer",
              "★ 只有 AUTH_TOKEN → bearer（这一条以前完全没被认，功能在却一次不触发）",
              f"{source}/{style}")

    with env():
        check(llm_auth.credentials() == ("", "", ""),
              "★ 一个都没有 → 三元组全空（调用方按「前置缺失」跳过，不报错）",
              str(llm_auth.credentials()))

    print("--- 2. 空串 / 空白不算凭据 ---")
    with env(env_vars={"ANTHROPIC_API_KEY": "   "}):
        check(llm_auth.credentials()[0] == "",
              "★ 全是空白的 API_KEY 不算数（否则会拿一个空密钥去撞 401）")
    with env(cm="", cm_note="absent"):
        check(llm_auth.credentials()[0] == "", "凭据管理器没配 → 继续往下找")

    print("--- 3. 端点：环境变量 > Claude Code 配置 > 内置默认 ---")
    # 端点和凭据**反过来**（环境变量优先）是有意的：端点不是秘密，
    # 也不存在"哪条路只有哪个来源"的问题，此时"显式设置胜过隐式配置"才对。
    with env():
        check(llm_auth.base_url() == llm_auth.DEFAULT_BASE_URL,
              "都没有 → 内置默认", llm_auth.base_url())
    with env(cc=FakeCC(base="https://relay.example.com/")):
        check(llm_auth.base_url() == "https://relay.example.com",
              "★ 读 Claude Code 配置（配了中转就不会去戳 api.anthropic.com）",
              llm_auth.base_url())
    with env(cc=FakeCC(base="https://relay.example.com"),
             env_vars={"ANTHROPIC_BASE_URL": "https://explicit.example.com/"}):
        check(llm_auth.base_url() == "https://explicit.example.com",
              "环境变量压过配置文件", llm_auth.base_url())
    with env(cc=None, env_vars={"ANTHROPIC_BASE_URL": "https://x.example.com/"}):
        check(llm_auth.base_url() == "https://x.example.com",
              "daemon/ 不在旁边时环境变量照常work", llm_auth.base_url())
    with env(cc=FakeCC(raises=True)):
        check(llm_auth.base_url() == llm_auth.DEFAULT_BASE_URL,
              "★ 配置文件读崩了 → 退回默认（不该把整个 L5 拖崩）", llm_auth.base_url())

    print("--- 4. 模型：取快档，绝不继承「当前档」 ---")
    # L5 每次安装都要跑。取"当前档"意味着无人值守地反复用最贵的那档调模型，
    # 账单会在你注意到之前涨起来。
    with env():
        check(llm_auth.model() == llm_auth.DEFAULT_MODEL,
              "都没有 → 内置默认（Haiku 档）", llm_auth.model())
    with env(cc=FakeCC(resolved="deepseek-v4-flash")):
        check(llm_auth.model() == "deepseek-v4-flash",
              "★ 解析出快档 → 用它", llm_auth.model())
    with env(cc=FakeCC(resolved=None)):
        # ⚠️ 这条是整套测试里最值钱的一条。`cc_models.resolve()` 认不出别名时
        # 会把输入**原样返回**（"haiku"）。原样返回的 "haiku" 不是模型 ID，
        # 发出去就是 404 —— 而 404 看起来像"网络问题"。
        check(llm_auth.model() == llm_auth.DEFAULT_MODEL,
              "★ resolve() 原样退回别名 → 不算数，回落默认（否则发出去是个 404）",
              llm_auth.model())
    with env(cc=FakeCC(resolved="deepseek-v4-flash"),
             env_vars={"ANTHROPIC_MODEL": "my-model"}):
        check(llm_auth.model() == "my-model",
              "环境变量压过配置文件", llm_auth.model())
    with env(cc=FakeCC(raises=True)):
        check(llm_auth.model() == llm_auth.DEFAULT_MODEL,
              "配置文件读崩了 → 退回默认", llm_auth.model())
    check(llm_auth.CHEAP_MODEL_ALIAS == "haiku",
          "快档别名是 haiku（换别名时这里要跟着改）", llm_auth.CHEAP_MODEL_ALIAS)

    print("--- 5. ★ describe() 绝不回密钥本身 ---")
    # 它的输出会进日志、进排查命令、可能进 HTTP 响应。带回密钥就等于把它
    # 写到一个渲染不受信数据的地方。
    with env(cm=SENTINEL, env_vars={"ANTHROPIC_BASE_URL": "https://x.example.com"}):
        d = llm_auth.describe()
        blob = json.dumps(d, ensure_ascii=False)
        check(SENTINEL not in blob, "★ 描述里没有密钥", blob[:100])
        check(d["has_token"] is True, "has_token 是布尔值", str(d["has_token"]))
        check(d["auth_style"] == "x-api-key", "auth_style 也报出来", d["auth_style"])
        check(set(d) >= {"key_source", "auth_style", "has_token", "key_note",
                         "base_url", "model"},
              "字段齐全（precheck 的跳过理由靠 has_token / key_note）", str(sorted(d)))

    with env():
        d = llm_auth.describe()
        check(d["has_token"] is False, "没有凭据时 has_token=False")
        check(SENTINEL not in json.dumps(d, ensure_ascii=False), "空的时候也没有密钥")

    print("--- 6. key_note：把「没配」和「读不出来」分开 ---")
    # 两者在 credentials() 里都退化成空串、都让 L5 跳过，但排查方向**相反**：
    # 前者去设置页填一次，后者要去凭据管理器看那条记录是不是坏了。
    # 混成一句话，用户会在设置页反复保存，而问题一次都不动。
    with env(cm_note=""):
        check(llm_auth.describe()["key_note"] == "", "没配过 → 没有备注（正常状态）")
    with env(cm_note="unreadable: 凭据库坏了"):
        d = llm_auth.describe()
        check("unreadable" in d["key_note"],
              "★ 配了但读不出来 → 备注里说明白", d["key_note"])
        check(d["has_token"] is False, "读不出来时确实没有可用凭据")
    with env(cm_note="unavailable"):
        d = llm_auth.describe()
        check(d["key_note"] == "unavailable",
              "凭据管理器整个用不了 → 也报出来（没装 pywin32 是另一回事）", d["key_note"])

    print("--- 7. auth_headers：两种风格互斥 ---")
    h = llm_auth.auth_headers("tok", "x-api-key")
    check(h.get("x-api-key") == "tok", "x-api-key 风格")
    check("authorization" not in h,
          "★ 不能同时发 authorization —— 原生端点对多余的那个头会直接报错")
    h2 = llm_auth.auth_headers("tok", "bearer")
    check(h2.get("authorization") == "Bearer tok", "bearer 风格")
    check("x-api-key" not in h2, "★ 同理，bearer 也不带 x-api-key")
    for hh in (h, h2):
        check(hh.get("anthropic-version") == "2023-06-01", "带上了 anthropic-version")
        check(hh.get("content-type") == "application/json", "带上了 content-type")

    print("--- 8. first_text：跳过 thinking 块 ---")
    # 开了思考的模型（DeepSeek 等中转默认开）会把 thinking 放在最前面，
    # 它**没有 text 字段**。写 response["content"][0]["text"] 会永远取到空 ——
    # 而那样看起来像"模型没答"，不像代码有 bug。
    resp = {"content": [{"type": "thinking", "thinking": "让我想想……"},
                        {"type": "text", "text": "真正的回答"}]}
    check(llm_auth.first_text(resp) == "真正的回答",
          "★ thinking 在前也取到 text", llm_auth.first_text(resp))
    check(llm_auth.first_text({"content": [{"type": "thinking", "thinking": "…"}]},
                              "兜底") == "兜底",
          "全是 thinking → 返回兜底值，不是崩")
    check(llm_auth.first_text({}, "兜底") == "兜底", "没有 content → 兜底")
    check(llm_auth.first_text({"content": "不是列表"}, "兜底") == "兜底",
          "content 不是列表 → 兜底（不抛 AttributeError）")
    check(llm_auth.first_text({"content": "x" * 10}, "") == "",
          "兜底值默认为空串")
    check(llm_auth.first_text({"content": [{"type": "text", "text": "一"},
                                           {"type": "text", "text": "二"}]}) == "一",
          "多个 text 块取第一个（顺序有意义，别改成拼接）")


def main():
    print("=" * 60)
    print("llm_auth 测试")
    print("=" * 60)
    run_checks()
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
