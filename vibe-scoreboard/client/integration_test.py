#!/usr/bin/env python3
"""Run a built client against a temporary real Python server and database."""
import argparse
import json
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AppServer, ScoreStore, technical_stats
from server import EntryServer, load_access_config


def request(address, path, body=None, token=None):
    headers = {}
    if token:
        headers["X-Client-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(address + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method="GET" if body is None else "POST")
    try:
        response = urllib.request.urlopen(req, timeout=40)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, type=Path)
    args = parser.parse_args()
    binary = args.client.resolve()
    clients, servers, threads = [], [], []
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        store = ScoreStore(root / "data" / "scoreboard.sqlite3")
        store.create_player({"name": "蓝白碗"})
        season = store.create_competition({"name": "Season 14"})
        config = load_access_config(root / "data")
        entry = EntryServer(("127.0.0.1", 0), store, config["access_code"], config["server_id"])
        admin = AppServer(("127.0.0.1", 0), store)
        upstream = "http://127.0.0.1:" + str(entry.server_port)
        drop_reply = [True]

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
                req = urllib.request.Request(upstream + self.path, data=body, method=self.command,
                                             headers={"Authorization": self.headers.get("Authorization", ""),
                                                      "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    status, raw = response.status, response.read()
                if self.command == "POST" and drop_reply[0]:
                    drop_reply[0] = False
                    self.close_connection = True
                    return  # Backend committed, but its success response never reaches the client.
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = forward
            do_POST = forward

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        proxy_url = "http://127.0.0.1:" + str(proxy.server_port)
        servers.extend([entry, admin, proxy])
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)

        def launch(name, target):
            process = subprocess.Popen([str(binary), "--no-browser", "--listen", "127.0.0.1:0",
                                        "--data-dir", str(root / name)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first_line = process.stdout.readline().strip()
            assert first_line.startswith("Blue3 Score Entry: "), first_line
            address = first_line.split(": ", 1)[1]
            status, bootstrap = request(address, "/api/config")
            assert status == 200
            token = bootstrap["token"]
            clients.append((process, address, token))
            status, catalog = request(address, "/api/connect", {"server_url": target, "access_code": config["access_code"]}, token)
            assert status == 200, catalog
            assert catalog["server_id"] == config["server_id"]
            return process, address, token, bootstrap

        try:
            first, client1, token1, _ = launch("client-one", proxy_url)
            payload = {"server_id": config["server_id"], "competition_id": season, "played_at": "2026-11-13T20:00",
                       "rate": "1", "request_key": "full-integration-001", "note": "客户端联调",
                       "entries": [{"player_id": pid, "base": str(base), "battle_count": "8"}
                                   for pid, base in [(1, 100), (2, -50), (3, -30), (4, -20)]]}
            status, result = request(client1, "/api/submit", payload, token1)
            assert status == 502 and result["pending"] is True, result
            assert len(store.state()["matches"]) == 1
            request(client1, "/api/quit", {}, token1)
            assert first.wait(timeout=10) == 0

            restarted, client1, token1, bootstrap = launch("client-one", proxy_url)
            assert bootstrap["pending"]["payload"]["request_key"] == payload["request_key"]
            status, result = request(client1, "/api/submit", payload, token1)
            assert status == 201, result
            assert len(store.state()["matches"]) == 1
            assert not (root / "client-one" / "pending.json").exists()

            second, client2, token2, _ = launch("client-two", upstream)
            payload2 = {**payload, "request_key": "full-integration-002"}
            status, result = request(client2, "/api/submit", payload2, token2)
            assert status == 201, result
            admin_url = "http://127.0.0.1:" + str(admin.server_port)
            status, state = request(admin_url, "/api/state")
            assert status == 200 and len(state["matches"]) == 2
            stats = technical_stats(state, state["matches"])
            player = next(p for p in stats["players"] if p["id"] == 1)
            assert player["matches"] == 16 and player["total_units"] == 2000000, player
            assert all(m["competition_id"] == season and m["scoring_mode"] == "multi" for m in state["matches"])
            assert len(ScoreStore(store.path).state()["matches"]) == 2
            with urllib.request.urlopen(client2 + "/form.js") as response:
                assert response.status == 200 and b"validate" in response.read()
            for kind in ("players", "competitions"):
                status, result = request(client2, "/api/catalog/"+kind,
                    {"server_id": config["server_id"], "name": "Client creation disabled"}, token2)
                assert status == 404, result
            status, result = request(client2, "/api/submit", {**payload, "rate": "10", "request_key": "fixed-rate-001"}, token2)
            assert status == 201, result
            saved=next(m for m in store.state()["matches"] if m["id"]==result["id"])
            assert saved["rate"] == "1"
            assert len(store.state()["matches"]) == 3
            with urllib.request.urlopen(client2 + "/") as response:
                html=response.read().decode("utf-8")
                assert 'id="rate"' not in html and 'create-player-form' not in html and '1.3.0' in html
            print(json.dumps({"ok": True, "fixed_rate": "1", "catalog_creation_unavailable": True,
                "clients": 2, "saved_matches": 3, "lost_response_retry": "same record", "client_restart_recovery": True,
                "admin_visible": True},ensure_ascii=False))
        finally:
            for process, address, token in clients:
                if process.poll() is None:
                    try:
                        request(address, "/api/quit", {}, token)
                        process.wait(timeout=10)
                    except Exception:
                        process.terminate()
                        process.wait(timeout=5)
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)


if __name__ == "__main__":
    main()
