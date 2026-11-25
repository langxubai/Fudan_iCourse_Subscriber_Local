#!/usr/bin/env python3
"""Run the iCourse library on this computer, or perform one local sync."""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

from src.runtime.local_providers import LocalProviders
from src.runtime.local_store import ROOT, LocalStore, lecture_markdown, read_environment
from src.runtime.local_sync import SyncBusy, current_plan, start_sync, status, worker


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "iCourse"
    sys_version = ""

    @property
    def store(self) -> LocalStore:
        return self.server.store

    def reply(self, code, body, mime="application/json; charset=utf-8", *, filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.end_headers()
        self.wfile.write(body)

    def handle_local(self, *, post=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update(("127.0.0.1", "localhost"))
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin and origin != "http://" + host):
            self.reply(403, {"error": "请从本机课程库访问。"})
            return
        path = urlsplit(self.path).path
        try:
            if post:
                self.post_route(path)
            else:
                self.get_route(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except SyncBusy as exc:
            self.reply(409, {"error": str(exc)})
        except (ValueError, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.reply(500, {"error": "读取或保存失败，请检查本机终端中的运行记录。"})

    def get_route(self, path):
        if path == "/api/status":
            self.reply(200, status(self.store))
        elif path == "/api/plan":
            self.reply(200, current_plan(self.store))
        elif path == "/api/providers":
            self.reply(200, LocalProviders(self.store).list(read_environment(strict=False)))
        elif path == "/api/subscriptions":
            self.reply(200, {"course_ids": self.store.subscriptions(read_environment(strict=False))})
        elif path == "/api/library":
            self.reply(200, self.store.library())
        elif match := re.fullmatch(r"/api/lectures/([^/]+)(/markdown)?", path):
            lecture = self.store.lecture(unquote(match[1]))
            if lecture is None:
                self.reply(404, {"error": "找不到该课次。"})
            elif match[2]:
                title = f"{lecture['course_title'] or lecture['course_id']}_{lecture['sub_title'] or lecture['sub_id']}"
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title)[:100] + ".md"
                self.reply(200, lecture_markdown(lecture).encode("utf-8"), "text/markdown; charset=utf-8", filename=name)
            else:
                self.reply(200, lecture)
        else:
            asset = (ROOT / "local_ui" / (unquote(path).lstrip("/") or "index.html")).resolve()
            if not asset.is_relative_to(ROOT / "local_ui") or not asset.is_file():
                self.reply(404, {"error": "页面不存在。"})
                return
            mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            self.reply(200, asset.read_bytes(), mime)

    def post_route(self, path):
        if path not in ("/api/subscriptions", "/api/sync", "/api/discover", "/api/process",
                        "/api/providers", "/api/providers/select", "/api/providers/delete"):
            self.reply(404, {"error": "操作不存在。"})
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "需要 JSON 请求。"})
            return
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 1024 * 1024:
            self.reply(400, {"error": "请求大小无效。"})
            return
        body = json.loads(self.rfile.read(size))
        if not isinstance(body, dict):
            raise ValueError("请求应为 JSON 对象。")
        if path == "/api/subscriptions":
            self.reply(200, self.store.save_subscriptions(body.get("course_ids")))
        elif path.startswith("/api/providers"):
            providers = LocalProviders(self.store)
            env = read_environment(strict=False)
            if path == "/api/providers":
                result = providers.save(body, env)
            elif path == "/api/providers/select":
                result = providers.select(body.get("id"), env)
            else:
                result = providers.delete(body.get("id"), env)
            self.reply(200, result)
        else:
            process, state = start_sync(self.store, background=True,
                                        selection=body if path == "/api/process" else None)
            if process:
                threading.Thread(target=process.wait, daemon=True).start()
            self.reply(202 if process else 200, state)

    def do_GET(self):
        self.handle_local()

    def do_POST(self):
        self.handle_local(post=True)

    def log_message(self, fmt, *args):
        # The viewer polls frequently. Keep routine HTTP traffic out of the terminal.
        if len(args) > 1 and str(args[1]).startswith("5"):
            super().log_message(fmt, *args)


def serve(store: LocalStore, port: int | None, browser: bool) -> int:
    from src.data.database import Database
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765 if port is None else port), LocalHandler)
    except OSError as exc:
        if port is not None or exc.errno != errno.EADDRINUSE:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
    with server:
        Database(str(store.db)).conn.close()  # Create or migrate the existing local DB once.
        server.store = store
        url = f"http://127.0.0.1:{server.server_port}/"
        print("本地课程库 " + url, flush=True)
        if browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main() -> int:
    os.umask(0o077)
    if sys.argv[1:] == ["_worker"]:
        return worker()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["serve", "sync"], default="serve")
    parser.add_argument("--no-browser", action="store_true", help="仅启动服务")
    parser.add_argument("--port", type=int, help="固定本机端口（默认优先使用 8765）")
    args = parser.parse_args()
    store = LocalStore(read_environment(strict=False))
    if args.command == "sync":
        process, state = start_sync(store, background=False)
        print(state["message"], flush=True)
        if not process:
            return 0
        print("运行记录：" + str(store.data / "sync.log"), flush=True)
        code = process.wait()
        print(status(store)["message"], flush=True)
        return code
    return serve(store, args.port, not args.no_browser)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, SyncBusy) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
