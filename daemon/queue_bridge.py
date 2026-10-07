#!/usr/bin/env python3
"""
队列桥接 — 接收面板 HTTP 请求，读写 install-queue.json + 管理外部数据源
常驻在 localhost:18970（R4-b 后由开机自启任务拉起，见 daemon/install-bridge-service.ps1）
"""
import json
import sys
import time
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
SKILLS_DIR = Path.home() / ".claude" / "skills"  # 所有 skill 的安装根目录
QUEUE_FILE = SKILL_ROOT / "templates" / "install-queue.json"
EXT_SOURCES_FILE = SKILL_ROOT / "templates" / "external_sources.json"
PORT = 18970

# 让 bridge 能独立启动（R4-b 会把它拆成常驻服务），不依赖调用方先设好 sys.path
sys.path.insert(0, str(SKILL_ROOT))
from daemon.bridge_auth import KEY_HEADER, check_request, is_local_origin
from daemon.installed import installed_snapshot
from daemon import cc_models, credentials, safe_paths, secret_prompt, translate_config


class QueueHandler(BaseHTTPRequestHandler):
    # ---- 鉴权闸门 ----

    def _authorized(self):
        """所有数据接口的入口。未通过则回 403 并返回 False。"""
        ok, reason = check_request(self.headers)
        if ok:
            return True
        self._deny(reason)
        return False

    def _deny(self, reason):
        body = json.dumps({"ok": False, "error": reason}).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 刻意不回 CORS 头：跨站页面既拿不到数据，也读不到这条错误
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # 预检请求不带自定义头（浏览器规定），所以这里只校验 Origin。
        # 真正的密钥校验发生在随后的实际请求上。
        if not is_local_origin(self.headers.get("Origin")):
            self._deny("origin not allowed")
            return
        self._cors_reply(200)

    def do_GET(self):
        if not self._authorized():
            return
        path = self.path.rstrip("/")
        if path == "/ext-sources":
            self._handle_get_ext_sources()
        elif path == "/installed":
            self._handle_get_installed()
        elif path == "/credentials":
            self._handle_get_credentials()
        elif path == "/models":
            self._handle_get_models()
        elif path == "/translate-config":
            self._handle_get_translate_config()
        else:
            self._handle_get_queue()

    def do_POST(self):
        if not self._authorized():
            return

        # Content-Length 是外部输入。`int()` 撞上非数字会抛 ValueError ——
        # 那是个未捕获异常，连接被直接掐断，客户端只看到"连接被重置"。
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length < 0:
            content_length = 0

        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}

        # 顶层必须是对象。`[]` / `"x"` / `42` 都能过 json.loads，
        # 但后面每个 handler 都在 `data.get(...)` 上取值 —— 列表没有 .get，
        # 于是又是未捕获 AttributeError，客户端拿到断连而不是错误码。
        # 压力测试（2026-10-05）实测到过：body 传 `[]` 时客户端报连接异常。
        if not isinstance(data, dict):
            self._json_reply({"ok": False, "error": "body must be a JSON object"},
                             code=400)
            return

        if self.path.rstrip("/") == "/ext-sources":
            self._handle_save_ext_sources(data)
        elif self.path.rstrip("/") == "/ext-sources/fetch":
            self._handle_fetch_ext_sources()
        elif self.path.rstrip("/") == "/install":
            self._handle_install()
        elif self.path.rstrip("/") == "/uninstall":
            self._handle_uninstall(data)
        elif self.path.rstrip("/") == "/credentials/request":
            self._handle_request_credential(data)
        elif self.path.rstrip("/") == "/credentials/delete":
            self._handle_delete_credential(data)
        elif self.path.rstrip("/") == "/translate-config":
            self._handle_save_translate_config(data)
        else:
            self._handle_save_queue(data)

    # ---- 密钥与模型（R6/R9）----

    def _handle_get_credentials(self):
        """已配置哪些密钥。

        **只回布尔值**。这个响应会送到浏览器页面，任何"顺手带上值"的改动
        都等于把密钥交给 XSS —— 面板永远拿不到密钥本身。
        """
        self._json_reply(credentials.configured())

    def _handle_delete_credential(self, data):
        name = (data or {}).get("name", "")
        if not name:
            self._json_reply({"ok": False, "error": "name is required"})
            return
        ok = credentials.delete_secret(name)
        result = {"ok": True, "deleted": ok}
        result.update(credentials.configured())
        self._json_reply(result)

    def _handle_request_credential(self, data):
        """弹出原生输入框收密钥。

        值的路径：输入框 → 本进程内存 → Windows 凭据管理器。
        **不经过 HTTP 响应，也不经过浏览器** —— 这里只回"成没成"。
        """
        name = (data or {}).get("name", "")
        if not name:
            self._json_reply({"ok": False, "error": "name is required"})
            return
        try:
            ok, status = secret_prompt.ask_and_store(name)
        except secret_prompt.DialogBusy:
            self._json_reply({"ok": False, "error": "busy",
                              "message": "已经有一个密钥输入框开着，请先处理它"})
            return
        result = {"ok": ok, "status": status}
        result.update(credentials.configured())
        self._json_reply(result)

    def _handle_get_models(self):
        """Claude Code 配置里的模型清单（白名单提取，见 daemon/cc_models.py）。"""
        self._json_reply(cc_models.list_models())

    # ---- 翻译后端配置（R6）----

    def _handle_get_translate_config(self):
        """一次把面板要的东西全给它：当前配置 + 可选提示词 + AI 是否就绪。

        合成一个响应而不是开三个接口 —— 面板打开设置时是同时需要这三样的，
        分三次请求只会让界面闪三次。
        """
        from daemon import translate_prompts
        body = {
            "ok": True,
            "config": translate_config.load(),
            "prompts": translate_prompts.list_prompts(),
            "models": cc_models.list_models(),
            # 面板需要知道"现在能不能用 AI"，好提示用户去配密钥
            "ai_ready": bool(credentials.get_secret(credentials.AI_TOKEN)),
        }
        self._json_reply(body)

    def _handle_save_translate_config(self, data):
        cfg = translate_config.save(data or {})
        self._json_reply({"ok": True, "config": cfg})

    # ---- Queue ----

    def _handle_get_queue(self):
        data = {"version": 1, "pending": [], "history": [], "last_processed": None}
        if QUEUE_FILE.exists():
            try:
                data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._json_reply(data)

    def _handle_save_queue(self, data):
        try:
            QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            # 合并写入：**只更新 pending**，history/last_processed 一律以盘上的为准。
            #
            # ## 为什么面板传来的那两个字段被丢掉（2026-10-06 用户拍板：保留丢弃）
            #
            # 面板是**渲染不受信内容的地方**，而这个进程是唯一能删本地文件的东西。
            # 让页面写 history 等于让页面改写"装过什么"的记录 ——
            # 那是一条审计线索，不是 UI 状态。面板 POST 过来的
            # `history: [] / last_processed: null` 只是它自己那份副本的默认值，
            # 采信它会把服务端的记录**清空**。
            #
            # 所以：**pending 听面板的（那是用户当下的意图），历史听磁盘的（那是记录）。**
            # 对应地，面板现在也不再发这两个字段了（发了也没人读）。
            # （问题清单 #22，冻结树复核 D5 —— 六项里**唯一**没被证伪的推荐。）
            existing = {}
            if QUEUE_FILE.exists():
                try:
                    existing = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            merged = {
                "version": data.get("version", existing.get("version", 1)),
                "pending": data.get("pending", existing.get("pending", [])),
                "history": existing.get("history", []),
                "last_processed": existing.get("last_processed"),
            }
            QUEUE_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json_reply({"ok": True})
        except Exception as e:
            self._json_reply({"ok": False, "error": str(e)})

    # ---- Installed Skills ----

    def _handle_get_installed(self):
        """读取 sources.json 返回已安装 skill 列表

        转换逻辑在 daemon/installed.py —— daemon 离线生成面板时会用**同一个函数**
        把名单嵌进 HTML（R4-a）。两边共用一份实现，在线/离线才不会给出不同形状。
        显式传入本模块的 SKILL_ROOT，测试替换这个全局变量依然生效。
        """
        skills = installed_snapshot(SKILL_ROOT)
        self._json_reply({"ok": True, "count": len(skills), "skills": skills})

    # ---- External Sources ----

    def _handle_get_ext_sources(self):
        data = {"sources": []}
        if EXT_SOURCES_FILE.exists():
            try:
                data = json.loads(EXT_SOURCES_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        self._json_reply(data)

    def _handle_save_ext_sources(self, data):
        try:
            EXT_SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
            EXT_SOURCES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json_reply({"ok": True, "count": len(data.get("sources", []))})
        except Exception as e:
            self._json_reply({"ok": False, "error": str(e)})

    def _handle_fetch_ext_sources(self):
        """从外部数据源拉取 skill 并返回结果"""
        try:
            from daemon.fetcher import fetch_external_sources
            skills = fetch_external_sources()
            self._json_reply({"ok": True, "count": len(skills), "skills": skills})
        except Exception as e:
            self._json_reply({"ok": False, "error": str(e)})

    # ---- Install ----

    def _handle_install(self):
        """触发安装队列处理"""
        try:
            from daemon.installer import process_install_queue
            result = process_install_queue()
            # 队列文件损坏时 process_install_queue 会带回 error。
            # 以前这里无条件 `ok: True`，面板于是弹出"安装完成" —— 而那条
            # 队列里的东西一个都没动。"处理了 0 条"和"队列是空的"必须分开。
            ok = not result.get("error")
            self._json_reply({"ok": ok, **result})
        except Exception as e:
            self._json_reply({"ok": False, "error": str(e)})

    def _handle_uninstall(self, data):
        """卸载 skill：删除目录 + 从 sources.json 移除"""
        name = data.get("name", "")
        if not name:
            self._json_reply({"ok": False, "error": "name is required"})
            return

        # 先验名，再拼路径 —— 顺序反了就等于没验。
        # `SKILLS_DIR / name` 对 "../../.." 和绝对路径都照单全收，
        # 而下面紧跟一句 rmtree，所以这里是一道必须存在的闸门。
        try:
            skill_dir = safe_paths.resolve_within(SKILLS_DIR, name)
        except safe_paths.UnsafeName as e:
            self._json_reply({"ok": False, "error": "unsafe-name", "message": f"拒绝卸载：{e}"})
            return

        # `skill-forge` 是**完全合法**的名字，名字校验拦不住它 ——
        # 但它指向的是**我们自己**。删掉它等于把管理器连同日志、队列、
        # 部署留档一起删掉，而执行这条删除的正是它自己。
        # 2026-10-06 之前这里没有这道判断（冻结树复核 #18 实测可达）。
        if safe_paths.is_forge_dir(skill_dir, SKILLS_DIR):
            self._json_reply({
                "ok": False, "error": "self-uninstall-refused",
                "message": "拒绝卸载 skill-forge 本体 —— 它正在运行。"
                           "要卸它请手工处理，不要在面板里点。",
            })
            return

        try:
            import shutil
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            # 从 sources.json 移除
            sources_file = SKILL_ROOT / "sources.json"
            if sources_file.exists():
                sources = json.loads(sources_file.read_text(encoding="utf-8"))
                if name in sources:
                    del sources[name]
                    sources_file.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json_reply({"ok": True, "name": name})
        except Exception as e:
            self._json_reply({"ok": False, "error": str(e)})

    # ---- Helpers ----

    def _send_cors_headers(self):
        """只回本地页面。

        原先一律回 `*`，等于告诉所有网站"随便读"。现在回显请求的 Origin，
        且仅当它来自本地页面；否则不设这个头，浏览器就会拦下读取。
        """
        origin = self.headers.get("Origin")
        if is_local_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin or "null")
            self.send_header("Vary", "Origin")

    def _json_reply(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_reply(self, code):
        # ⚠️ 原先还有个 `body` 形参，从不写进响应（响应体恒为空）—— 2026-10-07 删除。
        # 全仓库唯一的调用点传的是 `""`，没有任何地方真的用它回过内容。
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, " + KEY_HEADER)
        self.end_headers()

    def log_message(self, format, *args):
        """把所有 HTTP 响应记一行。

        ## 为什么原来是 `pass`，以及为什么那个理由站不住

        原意是"别往控制台刷屏"。但代价是：**鉴权失败也不留痕迹**。
        `_deny()` 的 403（Origin 不合法 / 没带密钥 / 密钥不对）走的正是这里，
        而这条路径的用途就是"发现有人在敲"。敲一百次和敲零次，日志里一模一样。

        更糟的是服务用 `pythonw` 启动（`install-bridge-service.ps1` 优先找它）——
        没有控制台、没有窗口，连 stderr 都是黑洞。所以"不刷屏"的实际效果是
        **整个 bridge 一条日志都没有**。

        ## 记什么、不记什么

        记：请求行 + 状态码。这是排查"为什么面板说连不上"唯一需要的东西。
        **不记**：`Origin` 的值、密钥头的值、请求体 —— 前两个是攻击者可控的输入，
        请求体里可能有用户填的密钥。日志文件不像终端，它会被长期留在磁盘上。

        ## 谁来看这个文件

        `daemon/bridge.log`。跟 `watchdog.log` 并排，不设轮转 —— 一行几十字节，
        而写入量由本机面板的点击次数决定，不是外部流量。
        """
        try:
            line = format % args if args else format
            code = ""
            for tok in line.split():
                if tok.isdigit() and len(tok) == 3:
                    code = tok
                    break
            # ⚠️ 必须走 getattr，不能直接读 self.command / self.path。
            #
            # **请求行本身非法时**（400 Bad request syntax / 505 版本不支持），
            # `parse_request()` 在给 self.command / self.path 赋值**之前**就调了
            # `send_error()`，而 send_error 正是走到这里。直接读会抛 AttributeError，
            # 再被下面那个 except 吞掉 —— 结果是**最该被记下来的那种请求，一条都没有**：
            # 一个探测 127.0.0.1:18970 的扫描器会在日志里完全隐形。
            # （2026-10-06 冻结树复核 #4：起真实服务器发畸形请求行复现，日志 0 行。）
            method = getattr(self, "command", None) or "<非法请求行>"
            raw_path = getattr(self, "path", None)
            where = raw_path.split("?")[0] if raw_path else "<未解析到路径>"
            # 只记方法 + 路径 + 状态码；查询串和请求体一概不碰
            log_file = SKILL_ROOT / "daemon" / "bridge.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"{method} {where} {code or '???'}\n")
        except Exception:
            # 记日志失败绝不能把响应带崩 —— 这是日志，不是功能。
            pass


def start_bridge(allow_reuse: bool = True):
    """启动队列桥接服务器（阻塞）。

    R4-b 之后这是常驻服务的入口。用 HKCU\\...\\Run 自启时没有计划任务那种
    "崩溃自动重启"，所以自己兜一层：任何异常都不让进程死掉，退避后重来。
    否则一次偶发错误就会让面板的安装/卸载废到下次登录为止。
    """
    if allow_reuse:
        # 进程刚退时端口可能还在 TIME_WAIT，不设这个会 bind 失败
        HTTPServer.allow_reuse_address = True

    backoff = 5
    while True:
        try:
            # ThreadingHTTPServer 而不是 HTTPServer：收密钥要弹原生框，
            # 那是个阻塞操作（最长 180 秒等用户输入）。单线程的话
            # 整个面板在这期间会完全卡死，看起来像崩了。
            server = ThreadingHTTPServer(("127.0.0.1", PORT), QueueHandler)
            backoff = 5  # 起来了就把退避重置
            server.serve_forever()
            # serve_forever 正常返回 = 被要求关闭，这时该退出而不是重来
            return
        except KeyboardInterrupt:
            return
        except OSError as e:
            # 最常见的是 10048（端口被占）—— 可能另一个实例刚起来，等一下再看
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        except Exception:
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    start_bridge()
