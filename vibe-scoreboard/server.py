#!/usr/bin/env python3
"""Shared score-entry service. The full management UI stays on localhost."""
from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app import AppError, AppServer, ROOT, ScoreStore, positive_id


def load_access_config(directory: Path):
    """Keep the entry code stable across server restarts."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "entry-access.json"
    if not path.exists():
        value = {"access_code": secrets.token_urlsafe(24), "server_id": str(uuid.uuid4())}
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(value, output, indent=2)
                output.write("\n")
            path.chmod(0o600)
        except FileExistsError:
            pass
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", value.get("access_code", "")):
        raise ValueError("entry-access.json 中的录分码无效, 请恢复此配置文件.")
    uuid.UUID(value["server_id"])
    return value


class EntryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, access_code, server_id):
        self.store = store
        self.access_code = access_code
        self.server_id = server_id
        super().__init__(address, EntryHandler)

    def catalog(self):
        with self.store.connection() as db:
            players = [dict(row) for row in db.execute("SELECT id,name FROM players WHERE active=1 ORDER BY id")]
            competitions = [dict(row) for row in db.execute("SELECT id,name,note FROM competitions ORDER BY id")]
        return {"ok": True, "api_version": 1, "server_id": self.server_id,
                "server_name": "Blue3 Scoreboard", "rate": "1",
                "server_time": datetime.now().isoformat(timespec="minutes"),
                "players": players, "competitions": competitions,
                "capabilities": {"create_players": True, "create_competitions": True}}

    def create_catalog_item(self, payload, kind):
        if not isinstance(payload, dict):
            raise AppError("请求须为 JSON 对象.")
        if payload.get("server_id") != self.server_id:
            raise AppError("服务器已变更, 请重新连接后再新增.", 409)
        if kind == "players":
            item_id = self.store.create_player({"name": payload.get("name")})
        else:
            item_id = self.store.create_competition({"name": payload.get("name"),
                                                     "note": payload.get("note", "")})
        return {"ok": True, "id": item_id, "catalog": self.catalog()}

    def create_entry(self, payload):
        if not isinstance(payload, dict):
            raise AppError("请求须为 JSON 对象.")
        if payload.get("server_id") != self.server_id:
            raise AppError("服务器已变更, 请重新连接后选择赛季和选手.", 409)
        entries = payload.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
            raise AppError("请选择 1 到 200 位选手, 一起填写本场成绩.")
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise AppError("选手记录格式不正确.")
            normalized.append({"player_id": positive_id(entry.get("player_id")),
                               "base": entry.get("base"), "battle_count": entry.get("battle_count", 1)})
        record = {"competition_id": positive_id(payload.get("competition_id"), "赛季"),
                  "played_at": payload.get("played_at"), "note": payload.get("note", ""),
                  "request_key": payload.get("request_key"), "rate": payload.get("rate", "1"),
                  "scoring_mode": "multi", "record_kind": "score", "entries": normalized}
        # The existing transaction validates zero-sum scores, counts, active players,
        # season membership and idempotency before committing the entire round.
        match_id = self.store.save_match(record)
        return {"ok": True, "id": match_id, "competition_id": record["competition_id"],
                "participants": len(entries), "message": "成绩已写入服务器数据库."}


class EntryHandler(BaseHTTPRequestHandler):
    server: EntryServer

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, message, *args):
        logging.debug(message, *args)

    def reply(self, body, status=200):
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authenticate(self):
        supplied = self.headers.get("Authorization", "")
        if not supplied.isascii() or not secrets.compare_digest(supplied, "Bearer " + self.server.access_code):
            raise AppError("录分码不正确, 请向服务器管理员确认.", 401)

    def handle_request(self, method):
        try:
            path = urlparse(self.path).path
            if method == "GET" and path == "/health":
                self.reply({"ok": True, "service": "blue3-score-entry", "api_version": 1})
                return
            self.authenticate()
            if method == "GET" and path == "/api/entry/options":
                self.reply(self.server.catalog())
                return
            creators = {
                "/api/entry/matches": self.server.create_entry,
                "/api/entry/players": lambda data: self.server.create_catalog_item(data, "players"),
                "/api/entry/competitions": lambda data: self.server.create_catalog_item(data, "competitions"),
            }
            if method != "POST" or path not in creators:
                raise AppError("此端口只提供录分及赛季、选手的新增与查询.", 404)
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise AppError("请使用 JSON 提交录分.", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError("请求长度不正确.") from None
            if not 0 < length <= 128 * 1024:
                raise AppError("录分内容过大或为空.", 413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise AppError("录分内容未接收完整, 请重试.")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AppError("录分内容格式不正确.") from None
            result = creators[path](payload)
            self.reply(result, 201)
        except AppError as error:
            self.reply({"error": str(error)}, error.status)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            logging.exception("Shared entry database request failed")
            self.reply({"error": "数据库暂时忙碌, 请重试本次提交."}, 503)
        except Exception:
            logging.exception("Shared entry request failed")
            self.reply({"error": "提交暂未完成, 请重试本次提交."}, 500)

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_PUT(self):
        self.handle_request("PUT")

    def do_PATCH(self):
        self.handle_request("PATCH")

    def do_DELETE(self):
        self.handle_request("DELETE")


def main():
    parser = argparse.ArgumentParser(description="Blue3 Scoreboard 共享录分服务器")
    parser.add_argument("--host", default="0.0.0.0", help="录分服务监听地址")
    parser.add_argument("--entry-port", type=int, default=8766)
    parser.add_argument("--admin-port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()
    if not all(1 <= port <= 65535 for port in (args.entry_port, args.admin_port)) or args.entry_port == args.admin_port:
        parser.error("两个端口须在 1 到 65535 之间, 且不能相同.")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("HTTPS 需要同时指定 --tls-cert 和 --tls-key.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    admin = entry = None
    try:
        store = ScoreStore(args.data_dir / "scoreboard.sqlite3")
        config = load_access_config(args.data_dir)
        admin = AppServer(("127.0.0.1", args.admin_port), store)
        entry = EntryServer((args.host, args.entry_port), store, config["access_code"], config["server_id"])
        scheme = "http"
        if args.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            entry.socket = context.wrap_socket(entry.socket, server_side=True)
            scheme = "https"
    except (OSError, ValueError, KeyError) as error:
        if admin:
            admin.server_close()
        if entry:
            entry.server_close()
        print(f"无法启动服务器: {error}\n若端口被占用, 请先关闭原来的 start.bat 窗口.", flush=True)
        return 1
    admin_address = f"http://127.0.0.1:{args.admin_port}"
    addresses = {args.host} if args.host != "0.0.0.0" else set()
    if not addresses:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                         if not item[4][0].startswith("127.")}
        except OSError:
            pass
    print("\nBlue3 Scoreboard 共享录分服务器", flush=True)
    print(f"数据库: {store.path}\n本机管理界面: {admin_address}", flush=True)
    for address in sorted(addresses):
        print(f"客户端服务器地址: {scheme}://{address}:{args.entry_port}", flush=True)
    if not addresses:
        print(f"客户端地址: {scheme}://本机局域网IPv4地址:{args.entry_port} (使用 ipconfig 查看)", flush=True)
    print(f"录分码: {config['access_code']}\n请将服务器地址和录分码发给参与者. 保持本窗口运行, Ctrl+C 停止.\n", flush=True)
    thread = threading.Thread(target=admin.serve_forever, daemon=True)
    thread.start()
    if not args.no_browser:
        webbrowser.open(admin_address)
    try:
        entry.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止, 数据已保留.")
    finally:
        entry.server_close()
        admin.shutdown()
        admin.server_close()
        thread.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
