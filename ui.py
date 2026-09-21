# -*- coding: utf-8 -*-
"""本地网页版：两个数据源各自一个按钮、各自一份榜单。

【为什么要起一个本地服务，而不是继续写静态 HTML】：静态文件里的按钮没法触发扫描——
file:// 页面调不动本机的 Python。一个只绑 127.0.0.1 的小服务是最省事的做法：
页面仍然是本地的，没有任何东西对外暴露。

【两个源分开排名】（2026-09-20 运营者定）：混在一起排的话，论坛那边信噪比高，
会把 Reddit 的结果整段压下去；而两边该用的语气和该给的答案深度本来就不一样。

【绿灯只表示"这一轮扫过了"】，不表示"有结果"。扫过但一条没有，是一个有用的信息：
说明今天这个源上没人卡住，而不是工具坏了。
"""

import json
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCES = ("ankiforum", "reddit", "bilibili")
LABELS = {"ankiforum": "Anki 论坛", "reddit": "Reddit", "bilibili": "B站"}


class State:
    """扫描状态。页面每秒问一次，问的就是这里。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.busy = None      # 正在扫哪个源
        self.step = ""        # 正在做什么，直接显示给人看
        self.error = ""
        self.error_source = ""
        self.error_at = 0

    def snapshot(self):
        with self.lock:
            return {"busy": self.busy, "step": self.step, "error": self.error,
                    "error_source": self.error_source, "error_at": self.error_at}

    def take_snapshot(self):
        """整页渲染时用：把错误取走，显示过一次就不再显示。

        【错误是"刚才那一下的结果"，不是常驻状态】：它原来一直留在内存里，刷新
        页面照样出现，看着就像刚发生的——运营者 2026-09-20 就这么被骗过一次：
        半小时前测出来的一句"刚扫过，329 分钟后可以再扫"，在一次无关的刷新之后
        还挂在页面顶上，以为是刚刚又扫了一遍。
        """
        with self.lock:
            snap = {"busy": self.busy, "step": self.step, "error": self.error,
                    "error_source": self.error_source, "error_at": self.error_at}
            self.error, self.error_source, self.error_at = "", "", 0
            return snap

    def start(self, source):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.step, self.error = source, "开始…", ""
            return True

    def progress(self, text):
        with self.lock:
            self.step = text

    def finish(self, error=""):
        with self.lock:
            if error:
                self.error_source, self.error_at = self.busy or "", int(time.time())
            self.busy, self.step, self.error = None, "", error


def serve(store, config, scan_source, render_page, port=8899, open_browser=True,
          warm_source=None):
    """起服务。scan_source / render_page 由 radar.py 传进来，避免循环导入。

    warm_source：启动时先扫这个源。【要走和按钮同一条路径】，否则页面上那个按钮
    不会变黄，人看到的是"灰着不动"，会以为没在扫。
    """
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # 【不要把每个请求打进控制台】：那会把扫描进度冲掉。

        def _send(self, body, content_type="text/html; charset=utf-8", code=200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # 【出错要看得见】：默认情况下处理函数里抛异常，浏览器只会看到
            # "连接被重置"，而控制台上什么都没有——查起来无从下手。
            try:
                self._route()
            except Exception:  # noqa: BLE001
                detail = traceback.format_exc()
                print(detail, flush=True)
                self._send(f"<pre>{detail}</pre>", code=500)

        def _route(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send(render_page(store, config, state.take_snapshot()))
            elif parsed.path == "/status":
                self._send(json.dumps(state.snapshot()), "application/json")
            elif parsed.path == "/brief":
                query = urllib.parse.parse_qs(parsed.query)
                ident = (query.get("id") or [""])[0]
                row = store.get(ident)
                if not row:
                    self._send(json.dumps({"error": "没有这一条"}), "application/json", 404)
                    return
                try:
                    import ai

                    text = ai.brief(row, config.get("ai", {}))
                except Exception as exc:  # noqa: BLE001
                    self._send(json.dumps({"error": str(exc)[:300]}), "application/json", 502)
                    return
                self._send(json.dumps({"text": text}), "application/json")
            elif parsed.path == "/verdict":
                query = urllib.parse.parse_qs(parsed.query)
                ident = (query.get("id") or [""])[0]
                value = (query.get("value") or [""])[0]
                if not ident or value not in ("done", "ignored"):
                    self._send(json.dumps({"error": "bad request"}), "application/json", 400)
                    return
                store.set_verdict(ident, value)
                self._send(json.dumps({"ok": True}), "application/json")
            elif parsed.path == "/scan":
                query = urllib.parse.parse_qs(parsed.query)
                source = (query.get("source") or ["ankiforum"])[0]
                if source not in SOURCES:
                    self._send(json.dumps({"error": "unknown source"}), "application/json", 400)
                    return
                if not state.start(source):
                    self._send(json.dumps({"error": "busy"}), "application/json", 409)
                    return
                threading.Thread(
                    target=_run, args=(store, config, scan_source, state, source), daemon=True
                ).start()
                self._send(json.dumps({"started": source}), "application/json")
            else:
                self._send("not found", "text/plain; charset=utf-8", 404)

    class Server(ThreadingHTTPServer):
        # 【Windows 上一定要关掉 SO_REUSEADDR】：Python 默认开着它，而 Windows 的
        # 语义和 Unix 不一样——它允许第二个进程绑到同一个端口上，两个都"启动成功"，
        # 之后请求随机落到其中一个。表现出来就是改了代码刷新页面却时灵时不灵，
        # 而两个窗口都好端端地开着（2026-09-20 实测端口上真的蹲了两个进程）。
        allow_reuse_address = False

    url = f"http://127.0.0.1:{port}/"
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError:
        # 【说清楚是"已经开着"，不是"崩了"】：双击 run.bat 的人看到的是一个一闪
        # 而过的黑窗口，里面要么是一串 traceback，要么是一句人话。
        print(f"{port} 端口上已经有东西在跑了。")
        if _is_radar(url):
            print(f"就是 anki-radar 本身——页面还开着，直接刷新 {url} 就行。")
            print("要重开的话，先把那个窗口关掉（Ctrl-C），再跑一次。")
        else:
            print("但不是 anki-radar。换个端口：run.bat --port 8900")
        # 【要以非 0 退出】：run.bat 只在出错时 pause，正常结束的话双击出来的
        # 那个黑窗口会立刻关掉，上面这几行人根本来不及看。
        raise SystemExit(1)
    # 【先占住端口，再开扫】：顺序反过来的话，第二次启动会先老老实实扫一遍论坛，
    # 白发一轮网络请求，最后才发现端口早被占了。
    if warm_source and state.start(warm_source):
        threading.Thread(
            target=_run, args=(store, config, scan_source, state, warm_source), daemon=True
        ).start()

    print(f"本地页面：{url}（Ctrl-C 关闭）")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已关闭。")


def _is_radar(url):
    """端口被占了：占它的是不是我们自己？问一下 /status 就知道。"""
    try:
        with urllib.request.urlopen(url + "status", timeout=3) as response:
            return "busy" in json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 —— 探一下而已，失败就当不是
        return False


def _run(store, config, scan_source, state, source):
    try:
        scan_source(store, config, source, progress=state.progress)
        store.set_meta(f"last_scan_{source}", int(time.time()))
        state.finish()
    except Exception as exc:  # noqa: BLE001 —— 后台线程里抛出去就没人看得见了
        state.finish(error=str(exc)[:300])
