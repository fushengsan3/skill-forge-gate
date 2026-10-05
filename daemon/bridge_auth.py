#!/usr/bin/env python3
"""
队列桥接的鉴权。

bridge 监听 127.0.0.1:18970，原先没有任何鉴权 —— 于是用户在浏览器里打开的
**任何网站**都能 POST 到 /install，把任意 skill 塞进安装队列，
或调 /uninstall 删掉本地 skill（决策记录 §2.2）。

这里上两把锁：

1. **共享密钥**。首次需要时生成，存本地文件。daemon 生成面板 HTML 时读同一个文件，
   把密钥注入页面；面板每次请求都带上它。
2. **Origin 校验**。只接受本地页面（`file://` 页面发出的请求，Origin 头是字符串 "null"）。

---

## 密钥轮换（2026-10-05 加）

原先的注释写着"密钥刻意不轮换"，理由是"轮换后一旦 bridge 重启，已经打开的面板
就会永远失联，直到下次扫描才恢复"。**那个理由是真的，但结论下错了。**
正确做法不是"不轮换"，是**轮换 + 宽限期**：

    watchdog 每周扫描时轮换 → 新面板拿新密钥
    旧密钥继续接受 14 天（> 一个扫描周期）→ 已打开的面板不会当场失效
    14 天后旧密钥自动作废

原来的"永不轮换"意味着：密钥一旦泄露就是**永久且不可撤销**的。宽限期把这件事
从"永久"降级成"最多 14 天"，代价是保留一份过期表 —— 很划算。

## 一个必须记住的陷阱

**bridge 是独立进程，它不能缓存密钥。** 密钥文件由 watchdog（另一个进程）轮换。
如果 bridge 在内存里缓存住启动时读到的那个值，轮换之后它会拿旧密钥去比新面板
带来的新密钥，结果是**面板永远连不上，重启 bridge 才能恢复** —— 而且表现和
"密钥泄露了"一模一样，极难排查。

所以 `get_key()` **每次都读盘**。文件很小、请求很少，这点 I/O 不值得换一个
跨进程失效的坑。`reset_cache()` 保留下来只是为了不破坏既有调用方，它现在是空操作。
"""
import itertools
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"

# 当前密钥。**纯文本**，格式与历史版本一致 —— 别的地方可能直接读它。
KEY_FILE = SKILL_ROOT / "templates" / ".bridge-key"

# 上一批仍在宽限期内的密钥（轮换时才写）
PREV_FILE = SKILL_ROOT / "templates" / ".bridge-keys-prev.json"

# 面板在每个请求上带的头
KEY_HEADER = "X-Skill-Forge-Key"

# 模板里的占位符。刻意保留后面的 `null`：这样未注入的模板仍是合法 JS，
# 直接打开 panel/fallback.html 不会整段脚本语法错误。
KEY_PLACEHOLDER = "/* __BRIDGE_KEY__ */ null"

# file:// 页面发出的跨源请求，Origin 头就是字符串 "null"
LOCAL_ORIGINS = frozenset({"null", "file://"})

# 旧密钥的宽限期。必须 **大于** 一个扫描周期（7 天），否则面板会在下一次扫描前失效。
GRACE_DAYS = 14

PREV_VERSION = 1

# 「第一次创建密钥」这条路径要串行化。
# 并发首次调用会让多个线程同时认为"文件不存在"、各自造一把、互相覆盖 ——
# 结果是几把不同的密钥同时在飞，而宽限期表里一把都没记。压测实测到过。
# **必须是 RLock（可重入）**：rotate() 持锁后会调 get_key()，
# 而密钥文件不存在时 get_key() 也要拿这把锁 —— 普通 Lock 会当场死锁。
# 这个死锁实测到过：test_bridge_auth 跑到 rotate(一个全新的 root) 就再也回不来。
_CREATE_LOCK = threading.RLock()

# 临时文件名的计数器。**不能只用 pid** —— 同一进程里的多个线程会撞名，
# 一个线程的 finally 会把另一个线程刚要换名的临时文件删掉。
_TMP_SEQ = itertools.count()


class KeyUnavailable(RuntimeError):
    """密钥读不到、又不该新建时抛这个。调用方应当**拒绝请求**，不是另造一把。"""


# ---------------------------------------------------------------- 读密钥

def _read_text(path: Path, attempts: int = 6) -> str:
    """读文本。遇到"文件正被换名"的**瞬时**错误退让重试几次。

    光把写侧做成原子还不够：Windows 上 `os.replace` 的那一瞬间，
    另一侧并发的 `open()` 可能拿到共享冲突。直接把它当成"文件不存在"
    会有两个后果 ——

      - 密钥文件读成空 → 当前密钥为空 → 所有请求 403
      - 宽限期表读成损坏 → 整表丢弃 → 轮换刚作废的那把密钥立刻失效

    表现都是"轮换期间随机一批 403，过一会儿自己又好了"。压测实测到过
    （改原子写之前 10/357，改之后仍有 9/374 —— 因为漏了读侧）。
    """
    for i in range(attempts):
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
            # 文件在、但内容是空的 —— 只可能来自"正写到一半"。
            # 当成读失败重试，不要当成"没有密钥"：后者会让调用方
            # 在别人正写的时候另造一把。
            if not path.exists():
                return ""
        except FileNotFoundError:
            return ""          # 真不存在，重试没意义
        except OSError:
            pass
        time.sleep(0.002 * (i + 1))
    return ""


def _write_atomic(path: Path, text: str, attempts: int = 12) -> None:
    """先写临时文件，再 `os.replace` 换过去 —— 换名在同一分区上是原子的。

    不这么做的话，并发的读会读到"写了一半"的文件：

      - 密钥文件读到截断的值 → 那一瞬间所有请求 403
      - 宽限期表读到半个 JSON → 被当成损坏、整表丢弃 → 旧面板当场失联

    表现出来就是"轮换期间随机一批请求被拒"，而且**过一会儿自己又好了** ——
    这种 bug 没有日志、没有规律，只能靠猜。压力测试（2026-10-05）实测到过：
    357 次并发请求里 10 次 403。

    **为什么要重试**：Windows 上如果目标文件正被另一个线程/进程打开着，
    `os.replace` 会直接抛 PermissionError（CPython 打开文件时不带
    FILE_SHARE_DELETE）。这些文件都只有几十字节，读窗口是微秒级，
    退让一下重试就行。压测里正是这一条把轮换线程打死了。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{next(_TMP_SEQ)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        for i in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.002 * (i + 1))
        # 换名一直失败（文件被长期占着）——退回直接写。
        # 会短暂重现"读到半截"的窗口，但总好过轮换彻底失败。
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _restrict(path: Path) -> None:
    """尽量把权限收到仅当前用户。Windows 上 chmod 语义有限，失败无妨。"""
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass


def _key_paths(root=None):
    """密钥文件的位置。`root` 用来把整套东西挪到别处（测试、多实例）。

    这个参数不是装饰：watchdog 的扫描流程会被测试直接调用，
    如果轮换写的是**写死的绝对路径**，那么"跑一遍测试"就等于
    "轮换一次用户真实密钥"。让路径跟着调用方的 root 走，
    测试改 `watchdog.SKILL_ROOT` 时密钥就自然落到临时目录里。
    """
    base = Path(root) if root else SKILL_ROOT
    return base / "templates" / ".bridge-key", base / "templates" / ".bridge-keys-prev.json"


def get_or_create_key(root=None) -> str:
    """读取当前密钥；文件**确实不存在**才生成一个并落盘。

    读不到必须分两种情况，混在一起会出大问题：

      a) 文件不存在 → 首次运行，造一把，天经地义
      b) 文件在、但这次读失败（并发换名时的瞬时共享冲突）→ **绝不能造新的**

    早期版本没分这两种，读失败就造一把新的**并覆盖真的那把** —— 那等于在
    不记录宽限期的情况下把密钥换掉：已打开的面板全部失联，而且宽限期表里
    没有旧密钥，等于绕过整个轮换机制做了一次静默作废。

    压测实测到过：374 次并发请求里 9 次 403，抓到的现场是"5 个互不相同的
    密钥同时在飞 + 宽限期表为空"。根因就是这个。

    **不缓存** —— 见模块顶部那个陷阱。
    """
    key_file, _ = _key_paths(root)
    key = _read_text(key_file)
    if key:
        return key

    # 加锁，并**双检**：等锁的这段时间里可能有别的线程已经建好了。
    # 不加锁的话，并发首次调用会让 N 个线程各自造一把、互相覆盖 ——
    # 结果是几把不同的密钥同时在飞，谁也不知道哪把是真的。
    with _CREATE_LOCK:
        key = _read_text(key_file)
        if key:
            return key

        # 拿不准文件在不在时，一律当"在" —— 宁可这一次读不到（调用方会拒绝请求），
        # 也不要冒"把现有密钥冲掉"的风险。
        try:
            key_file.stat()
        except FileNotFoundError:
            pass
        except OSError:
            raise KeyUnavailable(f"无法确认密钥文件状态：{key_file}")
        else:
            raise KeyUnavailable(
                f"密钥文件存在但读不出来：{key_file}。"
                "不新建 —— 新建会静默作废现有密钥（且不进宽限期表）。"
            )

        key = secrets.token_urlsafe(32)
        try:
            _write_atomic(key_file, key)
            _restrict(key_file)
        except OSError:
            # 落盘失败也不能让 bridge 起不来：退化成"本次运行有效"的密钥，
            # 面板会连不上，但至少不会把接口敞着。
            pass
        # 跨进程竞争：bridge 和 watchdog 是两个进程，进程内的锁拦不住它们。
        # 真撞上的话以**文件里**的为准 —— 让双方收敛到同一个值，
        # 而不是各自拿着一把自己以为是真的密钥。
        return _read_text(key_file) or key


def get_key(root=None) -> str:
    """本次请求该用的密钥（= 当前密钥）。每次读盘，理由见模块顶部。"""
    return get_or_create_key(root)


def reset_cache():
    """空操作，仅为兼容既有调用方保留。

    以前这里有进程内缓存，那正是"轮换之后 bridge 拒绝新密钥"的成因。
    现在每次读盘，没有缓存可清。
    """


# ---------------------------------------------------------------- 宽限期

def _load_previous(now: datetime = None, root=None) -> list:
    """读上一批密钥，顺手丢掉已过期的。返回 [{"key":..., "until":...}]。"""
    now = now or datetime.now()
    _, prev_file = _key_paths(root)
    raw = _read_text(prev_file)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 文件坏了就当没有 —— 宽限期失效只会要求用户重开面板，
        # 而"把坏文件当有效"才是真的危险。
        return []

    entries = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []

    alive = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        k, until = e.get("key"), e.get("until")
        if not isinstance(k, str) or not k:
            continue
        try:
            exp = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            continue
        if exp > now:
            alive.append({"key": k, "until": exp.isoformat(timespec="seconds")})
    return alive


def _save_previous(entries: list, root=None) -> None:
    _, prev_file = _key_paths(root)
    try:
        _write_atomic(prev_file, json.dumps(
            {"version": PREV_VERSION, "keys": entries}, ensure_ascii=False, indent=2))
        _restrict(prev_file)
    except OSError:
        pass


def valid_keys(now: datetime = None, root=None) -> list:
    """当前还认的密钥：当前密钥 + 宽限期内未过期的旧密钥。"""
    keys = [get_key(root)]
    keys += [e["key"] for e in _load_previous(now, root)]
    # 去重但保持顺序（当前密钥永远排第一）
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def rotate(now: datetime = None, root=None) -> str:
    """换一把新密钥，旧密钥进宽限期。返回新密钥。

    watchdog 在生成面板**之前**调用它 —— 顺序反了的话，新面板里注入的
    还是刚被作废的那把。

    `root` 必须传调用方自己的 SKILL_ROOT（见 `_key_paths` 的说明）：
    不传的话，测试跑一遍扫描流程就会轮换掉用户真实的密钥。
    """
    now = now or datetime.now()
    key_file, _ = _key_paths(root)
    # 与"首次创建"互斥：否则可能一边在造新密钥、一边在轮换，
    # 结果宽限期表里记的和文件里的对不上。
    with _CREATE_LOCK:
        old = get_key(root)

        entries = _load_previous(now, root)
        if old and all(e["key"] != old for e in entries):
            entries.append({
                "key": old,
                "until": (now + timedelta(days=GRACE_DAYS)).isoformat(timespec="seconds"),
            })
        # 同一把密钥可能在表里出现多次（连续轮换），保留最晚的过期时间
        merged = {}
        for e in entries:
            merged[e["key"]] = max(merged.get(e["key"], ""), e["until"])
        _save_previous([{"key": k, "until": v} for k, v in sorted(merged.items())], root)

        new_key = secrets.token_urlsafe(32)
        _write_atomic(key_file, new_key)
        _restrict(key_file)
    return new_key


def status(now: datetime = None, root=None) -> dict:
    """给人和给排查用的：现在认几把、旧的什么时候到期。**不回密钥本身。**"""
    now = now or datetime.now()
    prev = _load_previous(now, root)
    return {
        "current_prefix": get_key(root)[:6],
        "previous_count": len(prev),
        "previous_expiry": [e["until"] for e in prev],
        "grace_days": GRACE_DAYS,
    }


# ---------------------------------------------------------------- 校验

def is_local_origin(origin) -> bool:
    """Origin 是否可能来自本地页面。

    缺头（非浏览器客户端，比如 curl 或面板之外的本地脚本）也放行 ——
    真正的那道门是密钥，这一层只是提前挡掉明显的跨站请求。
    """
    if origin is None or origin == "":
        return True
    return origin in LOCAL_ORIGINS


def check_request(headers) -> tuple:
    """校验一个请求，返回 (是否放行, 失败原因)。"""
    if not is_local_origin(headers.get("Origin")):
        return False, "origin not allowed"

    supplied = headers.get(KEY_HEADER) or ""
    try:
        keys = valid_keys()
    except KeyUnavailable:
        # 密钥文件此刻读不出来 —— 拒绝，而不是另造一把。
        # 造一把会把现有密钥静默作废（而且不进宽限期表），
        # 那比"这一次请求被拒"严重得多。
        return False, "key currently unreadable"
    # 逐把比。不能用 `supplied in valid_keys()` —— 那样会把比较变成
    # 短路字符串相等，泄露"前面几个字符对上了"的时序信息。
    # compare_digest 要求两边都是纯 ASCII；非法字符一律视为不匹配。
    matched = False
    for k in keys:
        try:
            if secrets.compare_digest(supplied, k):
                matched = True
        except TypeError:
            continue
    if not matched:
        return False, "invalid or missing key"
    return True, ""


if __name__ == "__main__":
    if "--rotate" in sys.argv:
        print(rotate())
    elif "--status" in sys.argv:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        # 手工排查用：打印当前密钥
        print(get_key(), file=sys.stdout)
