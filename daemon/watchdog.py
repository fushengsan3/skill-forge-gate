#!/usr/bin/env python3
"""
Skill Forge 守护进程 — 每周扫描新 skill 并通知用户
通过 Windows 任务计划程序开机自启，pythonw.exe 静默运行
"""
import time
import json
import sys
import os
import socket
import ctypes
import threading
from pathlib import Path
from datetime import datetime, timedelta

# 配置
SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
BRIDGE_PORT = 18970
BRIDGE_KEEPALIVE = 300  # 仅在桥接由本进程临时拉起时生效，见 main()
SCAN_INTERVAL = 7 * 24 * 3600  # 7 天
MAX_RETRIES = 3
RETRY_INTERVAL = 30 * 60  # 30 分钟
SNOOZE_INTERVAL = 3600  # 1 小时

# 可乐云代理
PROXY = "http://127.0.0.1:7897"
os.environ["https_proxy"] = PROXY
os.environ["http_proxy"] = PROXY

# 添加模块路径
sys.path.insert(0, str(SKILL_ROOT))

# Windows MessageBox 常量
MB_ABORTRETRYIGNORE = 0x00000002
MB_ICONWARNING = 0x00000030
IDABORT = 3   # 跳过本周
IDRETRY = 4   # 重试
IDIGNORE = 5  # 1小时后提醒


def log(msg: str):
    """写入日志"""
    log_file = SKILL_ROOT / "daemon" / "watchdog.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")


def show_error_dialog(reason: str) -> int:
    """弹出 Windows 原生错误对话框，返回用户选择"""
    return ctypes.windll.user32.MessageBoxW(
        0,
        f"已尝试 {MAX_RETRIES} 次自动重试，均失败。\n\n"
        f"失败原因：{reason}\n\n"
        f"请检查网络连接后选择：\n\n"
        f"[重试] 立即重新扫描\n"
        f"[1小时后提醒] 暂时跳过，稍后再试\n"
        f"[跳过本周] 安静退出，下周再试",
        "Skill Forge — 守护进程启动失败",
        MB_ABORTRETRYIGNORE | MB_ICONWARNING
    )


def check_connectivity() -> bool:
    """检查网络连通性（使用显式代理，不依赖环境变量）
    403/429 视为网络可达（仅限流），2xx 视为正常，其他异常视为不可达"""
    import urllib.request
    import urllib.error
    try:
        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request("https://api.github.com", method="HEAD")
        opener.open(req, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        # 403/429 = GitHub 可达但限流，网络正常
        if e.code in (403, 429):
            return True
        return False
    except Exception:
        return False


def run_weekly_scan():
    """执行周度扫描主流程"""
    log("开始周度扫描...")

    # 导入扫描模块
    from daemon.fetcher import fetch_all_sources
    from daemon.classifier import classify_and_sort
    from daemon.notifier import send_notification

    # 1. 拉取数据
    skills = fetch_all_sources()

    # 2. 过滤已安装（排除无更新的，保留有更新的）
    installed = load_installed_skills()
    skills = filter_skills(skills, installed)

    # 3. 分类排序
    categorized = classify_and_sort(skills)

    # 3.5 翻译简介（所有分类的 skill description → description_zh）
    from daemon.translate import translate_skills
    for cat_name, skill_list in categorized.get("categories", {}).items():
        translate_skills(skill_list)

    # 4. 保存结果
    discover_dir = SKILL_ROOT / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output_file = discover_dir / f"weekly-{date_str}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(categorized, f, ensure_ascii=False, indent=2)

    # 4.5 轮换 bridge 密钥。
    #
    # **必须在生成面板之前** —— 顺序反了的话，新面板里注入的还是刚被作废的那把，
    # 于是用户下次打开面板就 403。
    #
    # 旧密钥会进宽限期（GRACE_DAYS=14，大于一个扫描周期），
    # 所以此刻已经开着的面板不会当场失效。
    try:
        from daemon.bridge_auth import rotate as rotate_bridge_key
        # 必须把本进程的 SKILL_ROOT 传进去。不传的话 rotate 会用 bridge_auth
        # 自己写死的绝对路径 —— 而测试会直接调用 run_weekly_scan，
        # 于是"跑一遍测试"就等于"轮换一次用户真实的密钥"。
        rotate_bridge_key(root=SKILL_ROOT)
        log("bridge 密钥已轮换，旧密钥进入宽限期")
    except Exception as e:
        # 轮换失败不该拖垮整轮扫描：旧密钥仍然有效，最坏是这次没换。
        # 但要在日志里留痕，否则"以为在轮换其实一直没换"会静默存在。
        log(f"bridge 密钥轮换失败（旧密钥继续有效）: {e}")

    # 5. 生成面板 HTML
    generate_panel_html(categorized, discover_dir / "latest.html")

    # 6. 发送通知
    total = categorized.get("total", 0)
    new_count = categorized.get("new_count", 0)
    update_count = categorized.get("update_count", 0)

    msg_parts = []
    if new_count > 0:
        msg_parts.append(f"{new_count} 个新 skill")
    if update_count > 0:
        msg_parts.append(f"{update_count} 个可更新")
    message = "，".join(msg_parts) if msg_parts else "无新发现"

    send_notification(
        title="🔧 Skill Forge — 周度扫描完成",
        message=f"本周发现：{message}",
        panel_path=str(discover_dir / "latest.html")
    )

    # 记录时间戳
    timestamp_file = SKILL_ROOT / "daemon" / "last_scan.txt"
    timestamp_file.write_text(datetime.now().isoformat())

    log(f"扫描完成：发现 {total} 个 skill")


def load_installed_skills() -> dict:
    """从 sources.json 加载已安装 skill 列表"""
    sources_file = SKILL_ROOT / "sources.json"
    if not sources_file.exists():
        return {}
    try:
        with open(sources_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def filter_skills(skills: list, installed: dict) -> list:
    """过滤已安装且无更新的 skill"""
    filtered = []
    for skill in skills:
        name = skill.get("name", "")
        if name in installed:
            # 已安装，检查是否有更新
            installed_sha = installed[name].get("installed_sha", "")
            upstream = skill.get("latest_sha", "")
            if upstream and installed_sha and upstream != installed_sha:
                skill["status"] = "update_available"
                skill["installed_sha"] = installed_sha
                filtered.append(skill)
            # SHA 相同 → 排除
        else:
            skill["status"] = "new"
            filtered.append(skill)
    return filtered


def generate_panel_html(categorized: dict, output_path: Path):
    """生成轻量 HTML 面板（fallback 用）"""
    # 注：必须用 json_for_script 而不是 json.dumps —— 数据要落进 <script> 块，
    # 上游 description 里的 </script> 会提前闭合脚本块造成 XSS。详见 daemon/safe_embed.py
    from daemon.safe_embed import json_for_script
    from daemon.bridge_auth import KEY_PLACEHOLDER, get_or_create_key
    from daemon.installed import installed_snapshot
    from daemon.periods import load_periods

    # R4-a：把已安装名单一并嵌进页面。
    # 面板原本只在 bridge 在线时才拿得到这份名单，bridge 一停就只剩一句
    # "Bridge 未运行，无法加载安装列表"。嵌进来之后，名单的最差新鲜度 = 上次扫描时间，
    # 而不是"bridge 是否恰好活着"。
    #
    # 用副本而不是直接往 categorized 上挂：上层还会拿这个 dict 做别的事，
    # 不该让"给面板看的字段"漏进通用数据里。
    payload = dict(categorized)
    payload["_installed"] = installed_snapshot(SKILL_ROOT)

    # R2/R3：历史各期数据。
    # 本期的存档在上一步（写 weekly-<date>.json）已经落盘，所以这里读得到它，
    # 读出来的最后一期就是"本期"。
    # 拆成 catalog + periods 两层是为了避免体积每周线性增长，详见 daemon/periods.py
    snapshot = load_periods(SKILL_ROOT / "discover")
    payload["_catalog"] = snapshot["catalog"]
    payload["_periods"] = snapshot["periods"]
    payload["_first_seen"] = snapshot["first_seen"]
    # 存档总期数可能大于嵌进去的期数（见 periods.MAX_PERIODS）——
    # 面板要如实告知，不能让用户以为更早的期不存在
    payload["_periods_total"] = snapshot["total_periods"]

    template = (SKILL_ROOT / "panel" / "fallback.html")
    # 简单注入 JSON 数据到 HTML 模板
    if template.exists():
        html = template.read_text(encoding="utf-8")
        html = html.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(payload))
        # 注入 bridge 密钥（R7）：面板每次请求都带上它，bridge 才认。
        # 同一个密钥两处注入，都必须走 json_for_script —— 它也是要落进 <script> 的。
        html = html.replace(KEY_PLACEHOLDER, json_for_script(get_or_create_key()))
        output_path.write_text(html, encoding="utf-8")


def try_start() -> bool:
    """尝试启动扫描，返回是否成功"""
    try:
        if not check_connectivity():
            raise Exception("无法连接到 api.github.com，请检查网络或代理")
        run_weekly_scan()
        return True
    except Exception as e:
        log(f"扫描失败: {e}")
        return False


def linger_if_bridge_owned(bridge_owned: bool):
    """只有桥接是本进程临时拉起的时候，才需要多活一会儿替它续命。

    常驻服务（R4-b）自己在跑，守护进程扫描完就该退出 —— 没必要为了一个
    别人提供的服务白占一个进程 5 分钟。
    """
    if not bridge_owned:
        return
    log(f"桥接由本进程临时提供，保持 {BRIDGE_KEEPALIVE}s 供面板交互...")
    time.sleep(BRIDGE_KEEPALIVE)


def bridge_already_running(port: int = BRIDGE_PORT, host: str = "127.0.0.1") -> bool:
    """端口上是否已经有东西在监听（即常驻桥接服务已在跑）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def main():
    log("守护进程启动")

    # 队列桥接（HTTP 服务，面板通过它读已装列表、执行安装/卸载）
    #
    # R4-b 之后桥接有两条来源：
    #   1. 常驻服务（daemon/install-bridge-service.ps1 注册的开机自启任务）—— 首选
    #   2. 本进程临时拉起 —— 没注册服务时的兜底，否则安装/卸载将永远不可用
    #
    # 端口只有一个，重复绑定会抛 OSError，所以先探一次再决定。
    bridge_owned = False
    try:
        if bridge_already_running():
            log(f"常驻桥接已在 127.0.0.1:{BRIDGE_PORT} 运行，本进程不再另起")
        else:
            from daemon.queue_bridge import start_bridge
            threading.Thread(target=start_bridge, daemon=True).start()
            bridge_owned = True
            log(f"队列桥接已在进程内临时启动 (127.0.0.1:{BRIDGE_PORT})")
    except Exception as e:
        log(f"队列桥接启动失败: {e}")

    # 检查是否需要扫描
    timestamp_file = SKILL_ROOT / "daemon" / "last_scan.txt"
    if timestamp_file.exists():
        last_scan = datetime.fromisoformat(timestamp_file.read_text().strip())
        if datetime.now() - last_scan < timedelta(seconds=SCAN_INTERVAL):
            log(f"距上次扫描不足 7 天（上次: {last_scan}），跳过扫描")
            linger_if_bridge_owned(bridge_owned)
            return

    # 尝试启动
    retries = 0
    while retries < MAX_RETRIES:
        if try_start():
            log("扫描成功")
            linger_if_bridge_owned(bridge_owned)
            return  # 成功

        retries += 1
        log(f"第 {retries} 次重试失败")

        if retries < MAX_RETRIES:
            log(f"等待 {RETRY_INTERVAL} 秒后重试...")
            time.sleep(RETRY_INTERVAL)

    # 3 次全部失败 → 弹窗
    log("3 次重试全部失败，弹出对话框")
    reason = "无法连接到 api.github.com" if not check_connectivity() else "扫描过程发生未知错误"

    while True:
        choice = show_error_dialog(reason)
        if choice == IDRETRY:  # 重试
            if try_start():
                return
            retries = 0  # 重置重试计数
        elif choice == IDIGNORE:  # 1小时后提醒
            time.sleep(SNOOZE_INTERVAL)
            if try_start():
                return
        else:  # 跳过本周
            log("用户选择跳过本周")
            timestamp_file.write_text(datetime.now().isoformat())  # 标记已跳过
            return


if __name__ == "__main__":
    main()
