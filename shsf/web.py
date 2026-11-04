"""Loopback-only seed selection application."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

from .seeds import SeedStore, RevisionConflict
from .recommendations import Recommendations
from .zotero import ZoteroClient, ZoteroError
from .agent_service import AgentService
from .agent_config import AgentError
from .analyses import Analyses

STATIC = Path(__file__).with_name("static")


class Application:
    def __init__(self, database, agent=None):
        self.agent = agent or AgentService()
        self.store = SeedStore(database)
        self.recommendations = Recommendations(database)
        self.analyses = Analyses(database, self.agent)
        self.token = secrets.token_urlsafe(32)
        self.zotero = None
        self.connection_revision = 0
        self.lock = threading.RLock()

    def client(self):
        with self.lock:
            if self.zotero is None:
                raise ZoteroError("请先连接 Zotero。")
            return self.zotero

    def dispatch(self, method, path, query, body):
        def number(name, default):
            value = int(query.get(name, [default])[0])
            if not 0 <= value <= 100000:
                raise ValueError("分页参数无效。")
            return value
        if method == "GET":
            if path == "/api/analysis/records":
                favorite=query.get("favorites",["false"])[0]
                if favorite not in ('true','false'):raise AgentError('收藏筛选参数无效。')
                return self.analyses.records(query.get("q",[""])[0],query.get("status",["all"])[0],favorite=='true',number("page",1))
            if path == "/api/analysis/paper": return self.analyses.paper(query.get("arxiv_id",[""])[0])
            if path == "/api/analysis/job": return self.analyses.get(query.get("job_id",[""])[0])
            if path == "/api/agent":
                return self.agent.status()
            if path == "/api/bootstrap":
                with self.lock:
                    status = self.zotero.status() if self.zotero else {"connected": False}
                return {"state": self.store.state(), "zotero": status}
            if path == "/api/recommendations":
                return self.recommendations.get()
            if path == "/api/papers":
                return self.store.search(query.get("q", [""])[0], query.get("mode", ["all"])[0], query.get("sort", ["relevance"])[0], number("page", 1))
            if path == "/api/zotero/collections":
                return self.client().collections(number("start", 0))
            if path == "/api/zotero/items":
                client = self.client()
                items, total = client.items(query.get("collection", [""])[0], number("start", 0))
                return {"items": self.store.preview(items, client.user_id), "total": total, "start": number("start", 0)}
        if method == "POST":
            revision = body.get("expected_revision")
            if path == "/api/analysis/favorite": return self.analyses.favorite(body.get("arxiv_id"),body.get("favorite"))
            if path == "/api/analysis/start": return self.analyses.start(body.get("arxiv_id"),body.get("kind"),body.get("batch_id"),body.get("retry",False))
            if path == "/api/agent/reload": return self.agent.reload()
            if path == "/api/agent/settings": return self.agent.save(body)
            if path == "/api/agent/test": return self.agent.test(body.get("task","explain"))
            if path == "/api/agent/chatgpt/start": return self.agent.auth.start(body.get("profile_id"))
            if path == "/api/agent/chatgpt/complete": return self.agent.auth.complete_remote(body.get("callback_url"))
            if path == "/api/agent/chatgpt/select": return self.agent.auth.select(body.get("profile_id"))
            if path == "/api/agent/chatgpt/models": return self.agent.models()
            if path == "/api/agent/chatgpt/disconnect": return self.agent.auth.disconnect(body.get("profile_id"))
            if path == "/api/recommendations/generate":
                return self.recommendations.generate()
            if path == "/api/recommendations/feedback":
                return self.recommendations.feedback(body.get("arxiv_id"),body.get("interest"),body.get("is_read"))
            if path == "/api/seeds/add":
                return {"state": self.store.add_local(body.get("arxiv_id"), revision)}
            if path == "/api/seeds/remove":
                return {"state": self.store.remove(body.get("seed_id"), revision)}
            if path == "/api/seeds/confirm":
                return {"state": self.store.confirm(revision)}
            if path == "/api/zotero/connect":
                client = ZoteroClient(body.get("api_key"))
                with self.lock:
                    self.connection_revision += 1
                    attempt = self.connection_revision
                status = client.connect(body.get("user_id"))
                with self.lock:
                    if attempt != self.connection_revision:
                        raise ZoteroError("连接请求已取消，请重新连接 Zotero。")
                    self.zotero = client
                return status
            if path == "/api/zotero/disconnect":
                with self.lock:
                    self.connection_revision += 1
                    self.zotero = None
                return {"connected": False}
            if path == "/api/zotero/import":
                client = self.client()
                items = client.selected(body.get("item_keys"))
                with self.lock:
                    if self.zotero is not client:
                        raise ZoteroError("Zotero 连接已变更，请重新预览后导入。")
                    return self.store.import_zotero(items, client.user_id, revision)
        raise LookupError("接口不存在。")


def make_server(database, port=8766, agent=None, host="127.0.0.1"):
    app = Application(database, agent)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, data, mime="application/json; charset=utf-8"):
            payload = json.dumps(data, ensure_ascii=False).encode() if mime.startswith("application/json") else data
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def handle_request(self):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if host == "0.0.0.0":
                request_host = self.headers.get("Host", "")
                parsed_host = urlsplit("//" + request_host)
                if parsed_host.hostname and not parsed_host.username and not parsed_host.password and parsed_host.netloc == request_host:
                    hosts.add(request_host)
            elif host != "127.0.0.1":
                hosts.add(f"{host}:{port}")
            if self.headers.get("Host") not in hosts or (self.headers.get("Origin") and self.headers["Origin"] not in {"http://" + h for h in hosts}):
                return self.send(403, {"error": "仅允许本机同源访问。"})
            url = urlsplit(self.path)
            body = {}
            if self.command == "GET" and url.path == "/auth/callback":
                try:
                    app.agent.auth.callback(parse_qs(url.query))
                    self.send_response(303)
                    self.send_header("Location", "/#agent")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                except AgentError as exc:
                    import html
                    page = ('<!doctype html><meta charset="utf-8"><title>SHSF 登录</title><h1>ChatGPT 登录未完成</h1><p>' + html.escape(str(exc)) + '</p><a href="/#agent">返回 Agent 设置</a>').encode()
                    return self.send(400, page, "text/html; charset=utf-8")
                except Exception:
                    return self.send(500, {"error": "登录未完成，请返回 Agent 设置重新连接。"})
            if self.command == "POST":
                if not hmac.compare_digest(self.headers.get("X-SHSF-Token", ""), app.token):
                    return self.send(403, {"error": "页面令牌已失效，请刷新页面。"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get_content_type() != "application/json" or not 0 < length <= 100000:
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, json.JSONDecodeError):
                    return self.send(400, {"error": "请求格式无效。"})
            if url.path.startswith("/api/"):
                try:
                    return self.send(200, app.dispatch(self.command, url.path, parse_qs(url.query), body))
                except RevisionConflict as exc:
                    return self.send(409, {"error": str(exc), "state": app.store.state()})
                except ValueError as exc:
                    return self.send(400, {"error": str(exc)})
                except LookupError:
                    return self.send(404, {"error": "接口不存在。"})
                except Exception:
                    return self.send(500, {"error": "本地服务暂时出错，请稍后重试或检查数据库状态。"})
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
            if self.command != "GET" or url.path not in assets:
                return self.send(404, {"error": "页面不存在。"})
            name, mime = assets[url.path]
            data = (STATIC / name).read_bytes().replace(b"__SHSF_TOKEN__", app.token.encode())
            self.send(200, data, mime)

        do_GET = handle_request
        do_POST = handle_request

    class LocalServer(ThreadingHTTPServer):
        def server_close(self):
            app.analyses.close()
            super().server_close()

    server = LocalServer((host, port), Handler)
    app.agent.auth.callback_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
    server.app = app
    return server


def serve(database, port=8766, agent_directory=None, profile_path=None, host="127.0.0.1"):
    server = make_server(database, port, AgentService(directory=agent_directory,profile_path=profile_path), host=host)
    server.app.analyses.recover()
    print(f"shsf: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
