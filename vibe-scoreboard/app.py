#!/usr/bin/env python3
"""Vibe Scoreboard: a local, dependency-free competition scorekeeper.

Run ``python app.py`` and open http://127.0.0.1:8765.
Raw scores use integer hundredths. With rate, final scores use exact 1/10000 units.
SQLite is the source of truth.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import sqlite3
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import tables

ROOT = Path(__file__).resolve().parent
MAX_SCORE = 1_000_000
PALETTE = ["#5878f4", "#a480dc", "#ec9c4b", "#51a998", "#d27393", "#7296b6"]
SCORE_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d{1,2})?|\.\d{1,2})$")
SCORE_SCALE = 10_000  # Exact final scores: two-decimal raw score * two-decimal rate.


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def score_cents(value, label="分数"):
    """Parse a score exactly. Never sum binary floating point input."""
    if isinstance(value, bool) or value is None:
        raise AppError(f"{label}必须是数字.")
    text = str(value).strip()
    if len(text) > 32 or not SCORE_PATTERN.fullmatch(text):
        raise AppError(f"{label}请填写数字, 最多保留两位小数.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise AppError(f"{label}格式不正确.") from None
    if not number.is_finite() or abs(number) > MAX_SCORE:
        raise AppError(f"{label}须在 -{MAX_SCORE:,} 到 {MAX_SCORE:,} 之间.")
    return int(number * 100)


def score_text(cents):
    return format(Decimal(cents) / 100, ".2f").rstrip("0").rstrip(".") or "0"


def clean_text(value, limit, label):
    if not isinstance(value, str):
        raise AppError(f"{label}格式不正确.")
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise AppError(f"{label}最多 {limit} 个字符, 不可含控制字符.")
    return value


def positive_id(value, label="选手"):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9]\d{0,9}", str(value)):
        raise AppError(f"{label}编号不正确.")
    return int(value)


def integer_input(value, label):
    if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d{1,7}", str(value).strip()):
        raise AppError(f"{label}必须是整数.")
    number = int(str(value).strip())
    if abs(number) > MAX_SCORE:
        raise AppError(f"{label}须在 -{MAX_SCORE:,} 到 {MAX_SCORE:,} 之间.")
    return number


def battle_count(value=1):
    count = integer_input(value, "对战数量")
    if count < 0:
        raise AppError("对战数量须为 0 到 1,000,000 的整数. 默认 1, 历史成绩已汇总数量时填 0.")
    return count


def single_result(match, entry):
    """Whether this score record contains one known individual game."""
    return match.get("record_kind", "score") == "score" and all(e.get("battle_count", 1) == 1 for e in match["entries"])


def rate_value(value):
    cents = score_cents(value, "rate")
    if not 0 < cents <= 1_000_000:
        raise AppError("rate 必须大于 0 且不超过 10,000, 最多两位小数.")
    return score_text(cents)


def point_scores(entries, rank_order=None, knockouts=None):
    """Return raw base cents plus canonical ordering/awards; never check point sum."""
    ids = [positive_id(entry.get("player_id")) for entry in entries]
    if len(ids) != 3 or len(set(ids)) != 3:
        raise AppError("请选择三位不同的选手.")
    points = {pid: integer_input(entry.get("points"), "点数") for pid, entry in zip(ids, entries)}
    chips = {pid: integer_input(entry.get("chip"), "chip") for pid, entry in zip(ids, entries)}
    if sum(chips.values()):
        raise AppError("三人的 chip 之和必须为 0.")
    if rank_order is None:
        order = sorted(ids, key=lambda pid: -points[pid])
        top_tie = points[order[0]] == points[order[1]] > points[order[2]]
        if len(set(points.values())) != 3 and not top_tie:
            raise AppError("点数相同, 请指定第二、三名或三人同分时的计分先后.")
    else:
        if not isinstance(rank_order, list) or len(rank_order) != 3:
            raise AppError("名次顺序必须包含本局三位选手.")
        order = [positive_id(pid) for pid in rank_order]
        if set(order) != set(ids) or len(set(order)) != 3:
            raise AppError("名次顺序必须包含本局三位不同选手.")
        if any(points[order[i]] < points[order[i+1]] for i in (0, 1)):
            raise AppError("名次须按点数从高到低排列, 只可调整同分者的先后.")
    first, second, third = order
    bases = {third: (points[third] - 500) * 10}
    if points[first] == points[second] > points[third]:
        # Third's cents are a multiple of ten, so the split is exact.
        bases[first] = bases[second] = -bases[third] // 2
    else:
        bases[second] = (points[second] - (500 if points[second] < 400 else 400)) * 10
        bases[first] = -bases[second] - bases[third]
    zeros = {str(pid) for pid in ids if points[pid] == 0}
    if knockouts is None:
        knockouts = {}
    if not isinstance(knockouts, dict) or set(knockouts) != zeros:
        raise AppError("请为每位 0 点选手选择击飞赏归属.")
    awards = {}
    for loser, recipient in knockouts.items():
        winner = positive_id(recipient)
        if winner not in ids or winner == int(loser):
            raise AppError("击飞赏须归属于本局另一位选手.")
        awards[loser] = winner
        bases[int(loser)] -= 1000
        bases[winner] += 1000
    for pid in ids:
        bases[pid] += chips[pid] * 300
    return bases, order, awards


class ScoreStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            was_existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='players'").fetchone()
            db.executescript("""
                CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    played_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    request_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    seat INTEGER NOT NULL CHECK(seat >= 0),
                    base_cents INTEGER NOT NULL,
                    extra_cents INTEGER NOT NULL,
                    PRIMARY KEY(match_id, player_id),
                    UNIQUE(match_id, seat)
                );
                CREATE INDEX IF NOT EXISTS idx_entries_player ON entries(player_id);
                CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(played_at);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS competitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    note TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    public_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS player_aliases (
                    external_key TEXT PRIMARY KEY,
                    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS competition_aliases (
                    external_key TEXT PRIMARY KEY,
                    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE
                );
            """)
            # Existing scores remain manual at rate=1; do not rewrite history.
            for table, additions in {
                "matches": {"scoring_mode": "TEXT NOT NULL DEFAULT 'manual'",
                            "rate": "TEXT NOT NULL DEFAULT '1'",
                            "rank_order": "TEXT NOT NULL DEFAULT '[]'",
                            "knockouts": "TEXT NOT NULL DEFAULT '{}'",
                            "competition_id": "INTEGER REFERENCES competitions(id)",
                            "public_key": "TEXT",
                            "record_kind": "TEXT NOT NULL DEFAULT 'score'"},
                "players": {"public_key": "TEXT"},
                "entries": {"points": "INTEGER", "chip": "INTEGER",
                            "battle_count": "INTEGER NOT NULL DEFAULT 1 CHECK(battle_count BETWEEN 0 AND 1000000)"}
            }.items():
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES ('rate','1')")
            if not was_existing:
                db.executemany("INSERT INTO players(name) VALUES (?)",
                               [("选手 A",), ("选手 B",), ("选手 C",)])
            # Migrate once. Later removal of all competitions/players stays intentional.
            if not db.execute("SELECT 1 FROM metadata WHERE key='competitions_initialized'").fetchone():
                name = "历史比赛" if db.execute("SELECT 1 FROM matches").fetchone() else "默认比赛"
                cid = db.execute("INSERT INTO competitions(name,public_key,created_at) VALUES (?,?,?)",
                                 (name, str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(timespec="seconds"))).lastrowid
                db.execute("UPDATE matches SET competition_id=? WHERE competition_id IS NULL", (cid,))
                db.execute("INSERT INTO metadata(key,value) VALUES ('competitions_initialized','1')")
            for table in ("players", "matches"):
                for row in db.execute(f"SELECT id FROM {table} WHERE public_key IS NULL OR public_key=''").fetchall():
                    db.execute(f"UPDATE {table} SET public_key=? WHERE id=?", (str(uuid.uuid4()), row[0]))
                db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_public_key ON {table}(public_key)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_matches_competition ON matches(competition_id)")
            self._upgrade_entry_capacity(db)
            self._repair_top_ties(db)

    @staticmethod
    def _upgrade_entry_capacity(db):
        """Remove the old three-seat constraint without changing saved entries."""
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name='entries'").fetchone()[0]
        if not re.search(r"seat\s+BETWEEN\s+0\s+AND\s+2", definition, re.I):
            return
        db.execute("""CREATE TABLE entries_multi (
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(id),
            seat INTEGER NOT NULL CHECK(seat >= 0),
            base_cents INTEGER NOT NULL, extra_cents INTEGER NOT NULL,
            points INTEGER, chip INTEGER,
            battle_count INTEGER NOT NULL DEFAULT 1 CHECK(battle_count BETWEEN 0 AND 1000000),
            PRIMARY KEY(match_id,player_id), UNIQUE(match_id,seat)
        )""")
        db.execute("""INSERT INTO entries_multi(match_id,player_id,seat,base_cents,extra_cents,points,chip,battle_count)
            SELECT match_id,player_id,seat,base_cents,extra_cents,points,chip,battle_count FROM entries""")
        db.execute("DROP TABLE entries")
        db.execute("ALTER TABLE entries_multi RENAME TO entries")
        db.execute("CREATE INDEX idx_entries_player ON entries(player_id)")

    @staticmethod
    def _repair_top_ties(db):
        """Upgrade previously saved top ties once, keeping their raw inputs."""
        if db.execute("SELECT 1 FROM metadata WHERE key='top_tie_scoring_v2'").fetchone():
            return
        candidates = db.execute("""
            SELECT m.id, m.rank_order, m.knockouts FROM matches m
            JOIN entries e ON e.match_id=m.id WHERE m.scoring_mode='points'
            GROUP BY m.id HAVING COUNT(*)=3 AND COUNT(DISTINCT e.points)=2
        """).fetchall()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for match in candidates:
            entries = [dict(row) for row in db.execute(
                "SELECT * FROM entries WHERE match_id=? ORDER BY seat", (match["id"],))]
            points = sorted((entry["points"] for entry in entries), reverse=True)
            if points[0] != points[1] or points[1] <= points[2]:
                continue
            bases, _, _ = point_scores(entries, json.loads(match["rank_order"]), json.loads(match["knockouts"]))
            if all(entry["base_cents"] == bases[entry["player_id"]] for entry in entries):
                continue
            db.executemany("UPDATE entries SET base_cents=? WHERE match_id=? AND player_id=?",
                           [(bases[e["player_id"]], match["id"], e["player_id"]) for e in entries])
            db.execute("UPDATE matches SET revision=revision+1,updated_at=? WHERE id=?", (now, match["id"]))
        db.execute("INSERT INTO metadata(key,value) VALUES ('top_tie_scoring_v2','1')")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _unique_name(db, name, exclude=None):
        if not name or "\n" in name or "\t" in name:
            raise AppError("请填写选手姓名.")
        for row in db.execute("SELECT id, name FROM players"):
            if row["id"] != exclude and row["name"].casefold() == name.casefold():
                raise AppError("已有同名选手, 请使用不同姓名.", 409)

    def create_player(self, data):
        name = clean_text(data.get("name"), 24, "姓名")
        with self.connection(write=True) as db:
            self._unique_name(db, name)
            if db.execute("SELECT COUNT(*) FROM players").fetchone()[0] >= 200:
                raise AppError("最多登记 200 名选手.")
            return db.execute("INSERT INTO players(name,active,public_key) VALUES (?,1,?)", (name, str(uuid.uuid4()))).lastrowid

    @staticmethod
    def _competition_name(db, value, exclude=None):
        name = clean_text(value, 60, "比赛名称")
        if not name or "\n" in name or "\t" in name:
            raise AppError("请填写比赛名称.")
        if any(row["id"] != exclude and row["name"].casefold() == name.casefold()
               for row in db.execute("SELECT id,name FROM competitions")):
            raise AppError("已有同名比赛, 请使用不同名称.", 409)
        return name

    def create_competition(self, data):
        with self.connection(write=True) as db:
            name = self._competition_name(db, data.get("name"))
            note = clean_text(data.get("note", ""), 200, "比赛备注")
            return db.execute("INSERT INTO competitions(name,note,public_key,created_at) VALUES (?,?,?,?)",
                              (name, note, str(uuid.uuid4()), datetime.now(timezone.utc).isoformat(timespec="seconds"))).lastrowid

    def update_competition(self, competition_id, data):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM competitions WHERE id=?", (competition_id,)).fetchone()
            if not row:
                raise AppError("比赛不存在.", 404)
            if data.get("revision") != row["revision"]:
                raise AppError("比赛已被修改, 请重新打开后编辑.", 409)
            name = self._competition_name(db, data.get("name", row["name"]), competition_id)
            note = clean_text(data.get("note", row["note"]), 200, "比赛备注")
            db.execute("UPDATE competitions SET name=?,note=?,revision=revision+1 WHERE id=?", (name, note, competition_id))

    def delete_competition(self, competition_id, data):
        expected = self._expected_matches(data.get("matches"), allow_empty=True)
        with self.connection(write=True) as db:
            row = db.execute("SELECT revision FROM competitions WHERE id=?", (competition_id,)).fetchone()
            if not row or row[0] != data.get("revision"):
                raise AppError("比赛已被修改或删除, 请刷新后重新确认.", 409)
            actual = {r[0]: r[1] for r in db.execute("SELECT id,revision FROM matches WHERE competition_id=?", (competition_id,))}
            if actual != expected:
                raise AppError("比赛内的对局已变化, 请刷新后重新确认. 本次未删除记录.", 409)
            db.execute("DELETE FROM matches WHERE competition_id=?", (competition_id,))
            db.execute("DELETE FROM competitions WHERE id=?", (competition_id,))
        return len(expected)

    def move_matches(self, data):
        expected = self._expected_matches(data.get("matches"))
        cid = positive_id(data.get("competition_id"), "比赛")
        with self.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM competitions WHERE id=?", (cid,)).fetchone():
                raise AppError("目标比赛不存在.", 404)
            for mid, revision in expected.items():
                row = db.execute("SELECT revision FROM matches WHERE id=?", (mid,)).fetchone()
                if not row or row[0] != revision:
                    raise AppError("所选对局已变化, 请刷新后重选. 本次未移动记录.", 409)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.executemany("UPDATE matches SET competition_id=?,revision=revision+1,updated_at=? WHERE id=? AND competition_id!=?",
                           [(cid, now, mid, cid) for mid in expected])
        return len(expected)

    def update_player(self, player_id, data):
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
            if not row:
                raise AppError("该选手不存在.", 404)
            name = clean_text(data.get("name", row["name"]), 24, "姓名")
            active = data.get("active", bool(row["active"]))
            if not isinstance(active, bool):
                raise AppError("选手状态不正确.")
            self._unique_name(db, name, player_id)
            db.execute("UPDATE players SET name=?, active=? WHERE id=?",
                       (name, int(active), player_id))

    def update_settings(self, data):
        rate = rate_value(data.get("rate"))
        with self.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('rate',?)", (rate,))
        return {"rate": rate}

    @staticmethod
    def _validate_match(db, data, old_player_ids=(), allow_archived=False):
        played_at = data.get("played_at")
        if not isinstance(played_at, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", played_at):
            raise AppError("请选择有效的比赛时间.")
        try:
            datetime.fromisoformat(played_at)
        except ValueError:
            raise AppError("比赛时间不正确.") from None
        note = clean_text(data.get("note", ""), 200, "备注")
        mode = data.get("scoring_mode", "manual")
        if mode not in ("points", "manual", "multi"):
            raise AppError("计分方式不正确.")
        kind = data.get("record_kind", "score")
        if kind not in ("score", "counts") or (kind == "counts" and mode != "multi"):
            raise AppError("数量汇总记录须使用多人模式.")
        entries = data.get("entries")
        if mode == "multi" and (not isinstance(entries, list) or not 1 <= len(entries) <= 200):
            raise AppError("多人录分须包含 1 到 200 位选手.")
        if mode != "multi" and (not isinstance(entries, list) or len(entries) != 3):
            raise AppError("每场比赛必须恰好有三位选手.")
        default_rate = (db.execute("SELECT value FROM metadata WHERE key='rate'").fetchone()[0]
                        if mode in ("points", "multi") else "1")
        rate = rate_value(data.get("rate", default_rate))
        parsed = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise AppError("选手记录格式不正确.")
            player_id = positive_id(entry.get("player_id"))
            row = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
            if not row or (not allow_archived and not row["active"] and player_id not in old_player_ids):
                raise AppError(f"第 {i+1} 位选手不存在或已停用.")
            parsed.append((player_id, entry))
        if len({e[0] for e in parsed}) != len(parsed):
            raise AppError("同一对局的选手不能重复.")
        order, awards = [], {}
        if mode == "points":
            bases, order, awards = point_scores(entries, data.get("rank_order"), data.get("knockouts"))
            parsed = [(pid, bases[pid], score_cents(e.get("extra", "0"), "额外分"),
                       integer_input(e.get("points"), "点数"), integer_input(e.get("chip"), "chip"))
                      for pid, e in parsed]
        elif mode == "multi":
            parsed = [(pid, score_cents(e.get("base"), "基础分"), 0, None, None) for pid, e in parsed]
        else:
            parsed = [(pid, score_cents(e.get("base"), "基础分"),
                       score_cents(e.get("extra", "0"), "额外分"), None, None) for pid, e in parsed]
        balance = sum(e[1] for e in parsed)
        if mode == "multi" and balance != 0:
            raise AppError(f"多人对局的基础分合计必须为 0, 当前为 {score_text(balance)}.")
        if mode != "multi" and balance != 0:
            raise AppError(f"三人的基础分之和必须为 0, 当前为 {score_text(balance)}. 额外分不参与此校验.")
        multiplier = score_cents(rate)
        if any(max(abs(base * multiplier), abs(extra * multiplier), abs((base + extra) * multiplier))
               > MAX_SCORE * SCORE_SCALE for _, base, extra, _, _ in parsed):
            raise AppError("乘以 rate 后的分数超过范围, 每项及当场总分不得超过 1,000,000.")
        parsed = [(*values, battle_count(entry.get("battle_count", 1))) for values, entry in zip(parsed, entries)]
        if kind == "counts" and any(base or extra for _, base, extra, _, _, _ in parsed):
            raise AppError("对战数量汇总只记录场数, 基础分和额外分必须为 0.")
        return played_at, note, mode, rate, json.dumps(order), json.dumps(awards, sort_keys=True), kind, parsed

    def save_match(self, data, match_id=None):
        with self.connection(write=True) as db:
            return self._save_match(db, data, match_id)

    def _save_match(self, db, data, match_id=None, allow_archived=False, public_key=None):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        old_players, row = (), None
        if match_id is not None:
            row = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            if not row:
                raise AppError("这局已不存在, 请刷新列表.", 404)
            if data.get("revision") != row["revision"]:
                raise AppError("这局已被其他页面修改, 请重新打开后编辑.", 409)
            old_players = [e[0] for e in db.execute("SELECT player_id FROM entries WHERE match_id=?", (match_id,))]
        cid = data.get("competition_id", row["competition_id"] if row else None)
        if cid is None:  # Backward-compatible API calls use the first competition.
            competition = db.execute("SELECT id FROM competitions ORDER BY id LIMIT 1").fetchone()
            if not competition:
                raise AppError("请先建立一场比赛, 再添加对局.")
            cid = competition[0]
        cid = positive_id(cid, "比赛")
        if not db.execute("SELECT 1 FROM competitions WHERE id=?", (cid,)).fetchone():
            raise AppError("所属比赛不存在, 请重新选择.")
        if data.get("scoring_mode") == "multi":
            data = self._resolve_multi_players(db, data)
        played_at, note, mode, rate, order, awards, kind, entries = self._validate_match(db, data, old_players, allow_archived)
        if match_id is None:
            key = data.get("request_key")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
                raise AppError("提交标识不正确, 请刷新后重试.")
            old = db.execute("SELECT * FROM matches WHERE request_key=?", (key,)).fetchone()
            if old:
                prior = [(e["player_id"], e["base_cents"], e["extra_cents"], e["points"], e["chip"], e["battle_count"])
                         for e in db.execute("SELECT * FROM entries WHERE match_id=? ORDER BY seat", (old["id"],))]
                if (old["competition_id"], old["played_at"], old["note"], old["scoring_mode"], old["rate"],
                    old["rank_order"], old["knockouts"], old["record_kind"], prior) != (cid, played_at, note, mode, rate, order, awards, kind, entries):
                    raise AppError("该提交标识已用于另一局, 请重新打开录分窗口.", 409)
                return old["id"]
            match_id = db.execute(
                "INSERT INTO matches(competition_id,public_key,played_at,note,request_key,created_at,updated_at,scoring_mode,rate,rank_order,knockouts,record_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, public_key or str(uuid.uuid4()), played_at, note, key, now, now, mode, rate, order, awards, kind)).lastrowid
        else:
            db.execute("UPDATE matches SET competition_id=?,played_at=?,note=?,scoring_mode=?,rate=?,rank_order=?,knockouts=?,record_kind=?,revision=revision+1,updated_at=? WHERE id=?",
                       (cid, played_at, note, mode, rate, order, awards, kind, now, match_id))
            db.execute("DELETE FROM entries WHERE match_id=?", (match_id,))
        db.executemany("INSERT INTO entries(match_id,player_id,seat,base_cents,extra_cents,points,chip,battle_count) VALUES (?,?,?,?,?,?,?,?)",
                       [(match_id, player_id, seat, base, extra, points, chip, count)
                        for seat, (player_id, base, extra, points, chip, count) in enumerate(entries)])
        return match_id

    @staticmethod
    def _resolve_multi_players(db, data):
        entries = data.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
            raise AppError("多人录分须包含 1 到 200 位选手.")
        resolved = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise AppError("选手记录格式不正确.")
            entry = dict(entry)
            if entry.get("player_id") is None:
                name = clean_text(entry.get("player_name"), 24, "选手姓名")
                existing = next((r for r in db.execute("SELECT id,name FROM players") if r["name"].casefold() == name.casefold()), None)
                if existing:
                    entry["player_id"] = existing["id"]
                else:
                    ScoreStore._unique_name(db, name)
                    if db.execute("SELECT COUNT(*) FROM players").fetchone()[0] >= 200:
                        raise AppError("最多登记 200 名选手.")
                    entry["player_id"] = db.execute("INSERT INTO players(name,active,public_key) VALUES (?,1,?)",
                        (name, str(uuid.uuid4()))).lastrowid
            resolved.append(entry)
        return {**data, "entries": resolved}

    def preview_multi_scores(self, data):
        """Read a simple score table into a draft; never create players or rounds."""
        pasted = data.get("text")
        try:
            if pasted is not None:
                if not isinstance(pasted, str) or len(pasted) > 100_000:
                    raise AppError("粘贴的数据为空或过大.")
                rows = tables.read_score_table(pasted.encode("utf-8"), "paste.csv", header_optional=True)
            else:
                filename = clean_text(data.get("filename", ""), 200, "文件名")
                content = data.get("content")
                if not isinstance(content, str) or len(content) > 7_000_000:
                    raise AppError("请选择不超过 5 MB 的表格.")
                rows = tables.read_score_table(base64.b64decode(content, validate=True), filename)
        except (ValueError, tables.TableError) as error:
            raise AppError(str(error) if isinstance(error, tables.TableError) else "上传的文件数据不正确.") from None
        with self.connection() as db:
            players = [dict(row) for row in db.execute("SELECT id,name,active FROM players")]
        by_name = {p["name"].casefold(): p for p in players}
        entries, seen, new_names = [], set(), []
        for row in rows:
            name = clean_text(row["name"], 24, "选手姓名")
            if not name or any(c in name for c in "\n\t"):
                raise AppError(f'第 {row["_line"]} 行需要有效的选手姓名.')
            if name.casefold() in seen:
                raise AppError(f'选手 "{name}" 重复出现, 每位选手在本局只能录入一次.')
            seen.add(name.casefold())
            base = score_text(score_cents(row["base"], "基础分"))
            player = by_name.get(name.casefold())
            entries.append({"player_id": player["id"] if player else None,
                            "player_name": player["name"] if player else name, "base": base,
                            "battle_count": battle_count(row.get("battle_count", 1))})
            if not player:
                new_names.append(name)
        if len(players)+len(new_names) > 200:
            raise AppError("导入后超过 200 名选手, 请核对名单.")
        balance = sum(score_cents(entry["base"]) for entry in entries)
        if balance:
            raise AppError(f"多人对局的基础分合计必须为 0, 当前为 {score_text(balance)}.")
        return {"entries": entries, "new_players": new_names}

    def delete_match(self, match_id, revision):
        with self.connection(write=True) as db:
            row = db.execute("SELECT revision FROM matches WHERE id=?", (match_id,)).fetchone()
            if not row:
                raise AppError("这场比赛已不存在.", 404)
            if revision != row["revision"]:
                raise AppError("这场比赛已被修改, 请刷新后重新操作.", 409)
            db.execute("DELETE FROM matches WHERE id=?", (match_id,))

    @staticmethod
    def _expected_matches(records, allow_empty=False):
        if not isinstance(records, list) or len(records) > 10_000 or (not records and not allow_empty):
            raise AppError("请选择要删除的比赛.")
        expected = {}
        for record in records:
            if not isinstance(record, dict):
                raise AppError("比赛选择格式不正确.")
            match_id = positive_id(record.get("id"), "比赛")
            revision = positive_id(record.get("revision"), "比赛版本")
            if match_id in expected:
                raise AppError("比赛选择不可重复.")
            expected[match_id] = revision
        return expected

    def delete_matches(self, data):
        expected = self._expected_matches(data.get("matches"))
        with self.connection(write=True) as db:
            for match_id, revision in expected.items():
                row = db.execute("SELECT revision FROM matches WHERE id=?", (match_id,)).fetchone()
                if not row or row[0] != revision:
                    raise AppError("所选比赛已被修改或删除, 请刷新后重新选择. 本次未删除任何记录.", 409)
            db.executemany("DELETE FROM matches WHERE id=?", [(mid,) for mid in expected])
        return len(expected)

    def delete_players(self, data):
        values = data.get("ids")
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise AppError("请选择要删除的选手.")
        ids = [positive_id(value) for value in values]
        if len(set(ids)) != len(ids):
            raise AppError("选手选择不可重复.")
        expected = self._expected_matches(data.get("matches"), allow_empty=True)
        slots = ",".join("?" for _ in ids)
        with self.connection(write=True) as db:
            known = {row[0] for row in db.execute(f"SELECT id FROM players WHERE id IN ({slots})", ids)}
            if known != set(ids):
                raise AppError("所选选手已被删除, 请刷新后重新选择.", 409)
            affected = {row[0]: row[1] for row in db.execute(
                f"SELECT DISTINCT m.id,m.revision FROM matches m JOIN entries e ON m.id=e.match_id WHERE e.player_id IN ({slots})", ids)}
            if affected != expected:
                raise AppError("关联比赛已变化, 请刷新后重新确认删除范围. 本次未删除任何记录.", 409)
            db.executemany("DELETE FROM matches WHERE id=?", [(mid,) for mid in affected])
            db.executemany("DELETE FROM players WHERE id=?", [(pid,) for pid in ids])
        return {"players": len(ids), "matches": len(affected)}

    def state(self):
        """Read one consistent snapshot, including complete match history."""
        with self.connection() as db:
            db.execute("BEGIN")
            players = [dict(row) for row in db.execute("SELECT * FROM players ORDER BY id")]
            competitions = [dict(row) for row in db.execute("SELECT * FROM competitions ORDER BY id DESC")]
            matches = [dict(row) for row in db.execute("SELECT * FROM matches ORDER BY played_at DESC,id DESC")]
            entries = [dict(row) for row in db.execute("""
                SELECT e.*,p.name FROM entries e JOIN players p ON p.id=e.player_id
                ORDER BY e.match_id,e.seat
            """)]
            settings = {"rate": db.execute("SELECT value FROM metadata WHERE key='rate'").fetchone()[0]}
        by_match = {m["id"]: m for m in matches}
        by_competition = {c["id"]: c for c in competitions}
        for match in matches:
            match.pop("request_key", None)
            match["rank_order"] = json.loads(match["rank_order"])
            match["knockouts"] = json.loads(match["knockouts"])
            match["entries"] = []
            match["competition_name"] = by_competition[match["competition_id"]]["name"]
        for entry in entries:
            multiplier = score_cents(by_match[entry["match_id"]]["rate"])
            entry["base_units"] = entry["base_cents"] * multiplier
            entry["extra_units"] = entry["extra_cents"] * multiplier
            entry["total_units"] = entry["base_units"] + entry["extra_units"]
            entry["total_cents"] = entry["total_units"] / 100  # Legacy API compatibility.
            by_match[entry["match_id"]]["entries"].append(entry)
        for match in matches:
            ranks = finishing_ranks(match)
            for entry in match["entries"]:
                entry["finish_rank"] = ranks[entry["player_id"]]
        for player in players:
            player["active"] = bool(player["active"])
            player["color"] = PALETTE[(player["id"] - 1) % len(PALETTE)]
        return {"players": players, "competitions": competitions, "matches": matches, "settings": settings}

    def import_rounds(self, data, commit=False):
        filename = clean_text(data.get("filename", ""), 200, "文件名")
        content = data.get("content")
        if not isinstance(content, str) or len(content) > 7_000_000:
            raise AppError("请选择不超过 5 MB 的表格.")
        try:
            raw = base64.b64decode(content, validate=True)
            rounds = parse_import_rounds(tables.read_import_table(raw, filename))
        except (ValueError, tables.TableError) as error:
            raise AppError(str(error) if isinstance(error, tables.TableError) else "上传的文件数据不正确.") from None
        mode = data.get("mode", "skip")
        if mode not in ("skip", "overwrite", "copy"):
            raise AppError("重复对局处理方式不正确.")
        target = data.get("competition_id") or None
        target = positive_id(target, "比赛") if target is not None else None
        with self.connection(write=commit) as db:
            if not commit:
                db.execute("BEGIN")
            if target and not db.execute("SELECT 1 FROM competitions WHERE id=?", (target,)).fetchone():
                raise AppError("导入目标比赛已不存在.", 409)
            snapshot = {table: [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                        for table in ("players", "competitions", "matches", "entries", "player_aliases", "competition_aliases")}
            fingerprint = hashlib.sha256(raw + json.dumps([mode, target, snapshot], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if commit and data.get("fingerprint") != fingerprint:
                raise AppError("数据或导入选项已变化, 请重新预览后导入. 本次未写入记录.", 409)
            plan = self._import_plan(db, rounds, mode, target)
            preview = {"fingerprint": fingerprint, "total": len(rounds), "created": plan["created"],
                       "updated": plan["updated"], "skipped": plan["skipped"],
                       "new_players": [v["name"] for v in plan["players"].values() if v["id"] is None],
                       "new_competitions": [v["name"] for v in plan["competitions"].values() if v["id"] is None],
                       "rounds": []}
            for record in plan["rounds"][:50]:
                r = record["round"]
                preview["rounds"].append({"competition": plan["competitions"][record["competition"]]["name"],
                                          "played_at": r["played_at"], "action": record["action"],
                                          "players": [{"name": plan["players"][pk]["name"], "total_units": r["totals"][i]}
                                                      for i, pk in enumerate(record["players"])]})
            if not commit:
                return preview
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for value in plan["players"].values():
                if value["id"] is None:
                    value["id"] = db.execute("INSERT INTO players(name,active,public_key) VALUES (?,1,?)",
                                             (value["name"], value["public_key"] or str(uuid.uuid4()))).lastrowid
                for key in value["aliases"]:
                    db.execute("INSERT OR IGNORE INTO player_aliases(external_key,player_id) VALUES (?,?)", (key, value["id"]))
            for value in plan["competitions"].values():
                if value["id"] is None:
                    value["id"] = db.execute("INSERT INTO competitions(name,note,public_key,created_at) VALUES (?,?,?,?)",
                                             (value["name"], value["note"], value["public_key"] or str(uuid.uuid4()), now)).lastrowid
                for key in value["aliases"]:
                    db.execute("INSERT OR IGNORE INTO competition_aliases(external_key,competition_id) VALUES (?,?)", (key, value["id"]))
            for record in plan["rounds"]:
                r = record["round"]
                ids = [plan["players"][pk]["id"] for pk in record["players"]]
                payload = {"competition_id": plan["competitions"][record["competition"]]["id"],
                           "played_at": r["played_at"], "note": r["note"], "scoring_mode": r["mode"], "rate": r["rate"],
                           "record_kind": r["record_kind"],
                           "request_key": "import-" + str(uuid.uuid4()), "revision": record["revision"],
                           "entries": [{**e, "player_id": ids[i]} for i, e in enumerate(r["entries"])],
                           "rank_order": [ids[seat-1] for seat in r["order"]] if r["mode"] == "points" else None,
                           "knockouts": {str(ids[int(loser)-1]): ids[winner-1] for loser, winner in r["awards"].items()}}
                self._save_match(db, payload, record["id"], allow_archived=True,
                                 public_key=str(uuid.uuid4()) if mode == "copy" else r["key"])
            return preview

    @staticmethod
    def _import_plan(db, rounds, mode, target):
        plan = {"players": {}, "competitions": {}, "rounds": [], "created": 0, "updated": 0, "skipped": 0}
        catalogs = {}
        for table, alias_table, foreign in (("players", "player_aliases", "player_id"),
                                             ("competitions", "competition_aliases", "competition_id")):
            existing = [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
            by_id = {r["id"]: r for r in existing}
            by_key = {r["public_key"]: r["id"] for r in existing}
            by_key.update({r[0]: r[1] for r in db.execute(f"SELECT external_key,{foreign} FROM {alias_table}")})
            catalogs[table] = (by_id, by_key, {r["name"].casefold(): r["id"] for r in existing})

        def resolve(table, name, key, note=""):
            by_id, by_key, by_name = catalogs[table]
            found = by_key.get(key) if key else None
            if found is None:
                found = by_name.get(name.casefold())
            token = str(found) if found is not None else "new:" + name.casefold()
            is_new = found is None or isinstance(found, str)
            values = plan[table]
            if token not in values:
                row = by_id.get(found)
                values[token] = {"id": None if is_new else found, "name": row["name"] if row else name,
                                 "note": row.get("note", "") if row else note,
                                 "public_key": row["public_key"] if row else key, "aliases": set()}
            elif is_new and values[token]["note"] != note:
                raise AppError(f'比赛 "{name}" 的备注在表格中不一致, 请统一后导入.')
            if key:
                if key in by_key and str(by_key[key]) != token:
                    raise AppError("表中标识与姓名映射冲突, 请检查对局明细.")
                values[token]["aliases"].add(key)
                by_key[key] = found if not is_new else token
            # New entities can be resolved on subsequent rows without writing the DB.
            if is_new:
                by_name[name.casefold()] = token
            return token

        # resolve also handles string tokens for newly planned entities.
        existing_matches = {r["public_key"]: r for r in db.execute("SELECT id,public_key,revision FROM matches")}
        for r in rounds:
            existing = existing_matches.get(r["key"])
            if existing and mode == "skip":
                plan["skipped"] += 1
                continue
            if target:
                competition = catalogs["competitions"][0][target]
                cid = resolve("competitions", competition["name"], competition["public_key"], competition["note"])
            else:
                cid = resolve("competitions", r["competition"], r["competition_key"], r["competition_note"])
            pids = [resolve("players", e["name"], e["public_key"]) for e in r["entries"]]
            if len(set(pids)) != len(pids):
                raise AppError(f'对局 {r["key"]} 的选手映射后重复, 请检查姓名与选手标识.')
            update = existing is not None and mode == "overwrite"
            plan["updated" if update else "created"] += 1
            plan["rounds"].append({"round": r, "competition": cid, "players": pids,
                                   "id": existing["id"] if update else None,
                                   "revision": existing["revision"] if update else None,
                                   "action": "覆盖" if update else "新增"})
        if db.execute("SELECT COUNT(*) FROM players").fetchone()[0] + sum(v["id"] is None for v in plan["players"].values()) > 200:
            raise AppError("导入后超过 200 名选手, 请先核对同名与别名选手.")
        return plan

    def backup(self):
        # sqlite3.backup includes committed WAL changes and produces a portable file.
        with tempfile.TemporaryDirectory(prefix="vibe-backup-") as directory:
            backup_path = Path(directory) / "scoreboard.sqlite3"
            target = sqlite3.connect(str(backup_path))
            try:
                with self.connection() as db:
                    db.backup(target)
            finally:
                target.close()
            return backup_path.read_bytes()

    def seed_demo(self):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='demo_seeded'").fetchone():
                return
        if self.state()["matches"]:
            return
        with self.connection(write=True) as db:
            for i, name in enumerate(["阿澄", "小林", "小周"], 1):
                db.execute("UPDATE players SET name=? WHERE id=?", (name, i))
            db.execute("UPDATE competitions SET name='周末友谊赛' WHERE id=1")
        practice_id = self.create_competition({"name":"常规练习赛", "note":"不同比赛可以独立统计, 也可以批量汇总."})
        for name in ["阿远", "可可", "阿哲"]:
            self.create_player({"name": name})
        records = [([1,2,3],[32,-12,-20],[4,0,0]), ([1,4,5],[-15,45,-30],[0,6,0]),
                   ([2,3,6],[18,7,-25],[0,3,0]), ([1,2,4],[48,-19,-29],[5,0,0]),
                   ([3,5,6],[-8,28,-20],[0,2,0]), ([1,3,4],[21,9,-30],[3,0,0]),
                   ([2,4,5],[-13,37,-24],[0,5,0]), ([1,2,6],[36,-11,-25],[4,0,0])]
        for i, (ids, bases, extras) in enumerate(records):
            day = datetime.now().replace(hour=19, minute=30) - timedelta(days=7-i)
            self.save_match({"played_at": day.isoformat(timespec="minutes"),
                             "competition_id": 1 if i < 4 else practice_id,
                             "note": ["周末友谊赛", "晚间练习", "常规赛", "三人对局"][i % 4],
                             "request_key": f"demo-match-{i}",
                             "entries": [{"player_id": p, "base": str(b), "extra": str(e)}
                                         for p, b, e in zip(ids, bases, extras)]})
        with self.connection(write=True) as db:
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('demo_seeded','1')")


def external_key(value, label, required=False, namespace=""):
    value = clean_text(value, 80, label)
    if not value:
        if required:
            raise AppError(f"请填写{label}. 同一局的三行使用同一个标识.")
        return ""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "vibe:" + namespace + ":" + value))


def parse_import_rounds(rows):
    groups = {}
    entity_names = {"player": {}, "competition": {}}
    for row in rows:
        version = row.get("格式版本", "").strip()
        if version and version != tables.FORMAT:
            raise AppError(f'第 {row["_line"]} 行的格式版本不受支持.')
        name = clean_text(row["比赛"], 60, "比赛名称")
        player_name = clean_text(row["选手"], 24, "选手姓名")
        if not name or not player_name or any(c in name + player_name for c in "\n\t"):
            raise AppError(f'第 {row["_line"]} 行需要有效的比赛名称和选手姓名.')
        ckey = external_key(row.get("比赛标识", ""), "比赛标识", namespace="competition")
        pkey = external_key(row.get("选手标识", ""), "选手标识", namespace="player")
        key = external_key(row["对局标识"], "对局标识", required=True, namespace=ckey or name.casefold())
        for kind, identity, label in (("player", pkey, player_name), ("competition", ckey, name)):
            if identity and identity in entity_names[kind] and entity_names[kind][identity] != label:
                raise AppError(f'第 {row["_line"]} 行的同一标识对应多个名称, 请统一后导入.')
            if identity:
                entity_names[kind][identity] = label
        seat = integer_input(row["座位"], "座位")
        if not 1 <= seat <= 200:
            raise AppError(f'第 {row["_line"]} 行的座位须在 1 到 200 之间.')
        date = row["比赛时间"].strip().replace(" ", "T")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00", date):
            date = date[:-3]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", date):
            raise AppError(f'第 {row["_line"]} 行的时间须为 YYYY-MM-DD HH:MM.')
        try:
            datetime.fromisoformat(date)
        except ValueError:
            raise AppError(f'第 {row["_line"]} 行的时间无效.') from None
        mode = {"点数": "points", "手动": "manual", "多人": "multi"}.get(row["计分方式"].strip(), row["计分方式"].strip())
        if mode not in ("points", "manual", "multi"):
            raise AppError(f'第 {row["_line"]} 行的计分方式须为 points、manual 或 multi.')
        kind = row.get("记录类型", "").strip() or "score"
        if kind not in ("score", "counts") or (kind == "counts" and mode != "multi"):
            raise AppError(f'第 {row["_line"]} 行的记录类型须为 score, 或多人模式的 counts.')
        metadata = (name, ckey, clean_text(row.get("比赛备注", ""), 200, "比赛备注"), date,
                    clean_text(row.get("备注", ""), 200, "备注"), mode, rate_value(row["rate"]), kind)
        if key not in groups:
            groups[key] = {"metadata": metadata, "seats": {}}
        if groups[key]["metadata"] != metadata or seat in groups[key]["seats"]:
            raise AppError(f'第 {row["_line"]} 行的同局信息不一致或座位重复.')
        extra = "0" if mode == "multi" else score_text(score_cents(row["原始额外分"], "原始额外分"))
        entry = {"player_id": seat, "name": player_name, "public_key": pkey, "extra": extra,
                 "battle_count": battle_count(row.get("对战数量", "").strip() or 1)}
        if mode == "points":
            entry.update(points=integer_input(row.get("点数", ""), "点数"), chip=integer_input(row.get("chip", ""), "chip"))
        else:
            entry["base"] = score_text(score_cents(row.get("原始基础分", ""), "原始基础分"))
        groups[key]["seats"][seat] = {"entry": entry, "rank": row.get("名次", "").strip(),
                                       "scoring_order": row.get("计分顺序", "").strip(),
                                       "recipient": row.get("击飞赏归属", "").strip()}
    if len(groups) > 10_000:
        raise AppError("一次最多导入 10,000 局.")
    result = []
    for key, group in groups.items():
        seats = group["seats"]
        name, ckey, cnote, date, note, mode, rate, kind = group["metadata"]
        if mode != "multi" and set(seats) != {1,2,3}:
            raise AppError(f'对局 {key} 必须恰好有三行, 座位分别为 1、2、3.')
        if mode == "multi" and set(seats) != set(range(1,len(seats)+1)):
            raise AppError(f'对局 {key} 的多人座位须从 1 开始连续编号.')
        entries = [seats[i]["entry"] for i in sorted(seats)]
        names = {e["name"].casefold(): e["player_id"] for e in entries}
        if len(names) != len(entries):
            raise AppError(f'对局 {key} 的选手姓名不可重复.')
        order, awards = [], {}
        if mode == "points":
            ranks = [seats[i]["rank"] for i in (1,2,3)]
            scoring_order = [seats[i]["scoring_order"] for i in (1,2,3)]
            expected_ranks = [1 + sum(other["points"] > entry["points"] for other in entries)
                              for entry in entries]
            parsed_ranks = [integer_input(r, "名次") for r in ranks] if any(ranks) else None
            if any(scoring_order):
                positions = [integer_input(r, "计分顺序") for r in scoring_order]
                if set(positions) != {1,2,3}:
                    raise AppError(f'对局 {key} 的计分顺序须分别为 1、2、3.')
                order = sorted((1,2,3), key=lambda i: positions[i-1])
                if parsed_ranks is not None and parsed_ranks != expected_ranks:
                    raise AppError(f'对局 {key} 的名次必须只按点数确定, 同点数同名次.')
            elif parsed_ranks is not None and set(parsed_ranks) == {1,2,3}:
                # Older exports recorded calculation order in the rank column.
                order = sorted((1,2,3), key=lambda i: parsed_ranks[i-1])
            elif parsed_ranks is not None and parsed_ranks != expected_ranks:
                raise AppError(f'对局 {key} 的名次必须只按点数确定, 同点数同名次.')
            for i in (1,2,3):
                recipient = seats[i]["recipient"]
                if recipient:
                    if recipient.casefold() not in names:
                        raise AppError(f'对局 {key} 的击飞赏须归属于本局另一位选手.')
                    awards[str(i)] = names[recipient.casefold()]
            bases, order, awards = point_scores(entries, order or None, awards)
        else:
            bases = {e["player_id"]: score_cents(e["base"]) for e in entries}
            if sum(bases.values()):
                raise AppError(f'对局 {key} 的原始基础分之和必须为 0.')
        if kind == "counts" and any(bases.values()):
            raise AppError(f'对局 {key} 的数量汇总不可包含成绩.')
        multiplier = score_cents(rate)
        totals = [(bases[e["player_id"]] + score_cents(e["extra"])) * multiplier for e in entries]
        if any(max(abs(bases[e["player_id"]]*multiplier), abs(score_cents(e["extra"])*multiplier), abs(totals[i]))
               > MAX_SCORE*SCORE_SCALE for i, e in enumerate(entries)):
            raise AppError(f'对局 {key} 乘以 rate 后的分数超过范围.')
        result.append({"key": key, "competition": name, "competition_key": ckey, "competition_note": cnote,
                       "played_at": date, "note": note, "mode": mode, "rate": rate, "record_kind": kind, "entries": entries,
                       "order": order, "awards": awards, "totals": totals})
    return result


def finishing_ranks(match):
    """Points alone determine ranks; calculation order never breaks a rank tie."""
    if match.get("record_kind") == "counts":
        return {entry["player_id"]: None for entry in match["entries"]}
    if match.get("scoring_mode") == "points":
        points = [entry["points"] for entry in match["entries"]]
        return {entry["player_id"]: 1 + sum(point > entry["points"] for point in points)
                for entry in match["entries"]}
    totals = [entry["total_units"] for entry in match["entries"]]
    return {entry["player_id"]: 1 + sum(total > entry["total_units"] for total in totals)
            for entry in match["entries"]}


def _proportional_integers(amount, weights):
    total = sum(weights.values())
    parts = {pid: divmod(amount * weight, total) for pid, weight in weights.items()}
    result = {pid: part[0] for pid, part in parts.items()}
    for pid in sorted(parts, key=lambda p: (-parts[p][1], p))[:amount - sum(result.values())]:
        result[pid] += 1
    return result


def score_flows(match):
    """Balanced whole-point transfers, with proportional cells rounded down/up."""
    if match.get("record_kind", "score") != "score":
        return {}
    positive = {e["player_id"]: e["total_units"] for e in match["entries"] if e["total_units"] > 0}
    negative = {e["player_id"]: -e["total_units"] for e in match["entries"] if e["total_units"] < 0}
    if not positive or not negative:
        return {}
    budget = (min(sum(positive.values()), sum(negative.values())) + SCORE_SCALE // 2) // SCORE_SCALE
    if not budget:
        return {}
    incoming, outgoing = _proportional_integers(budget, positive), _proportional_integers(budget, negative)
    winners, losers = sorted(positive), sorted(negative)
    source, sink = len(winners) + len(losers), len(winners) + len(losers) + 1
    graph = [[] for _ in range(sink + 1)]

    def edge(a, b, capacity):
        forward = [b, capacity, len(graph[b])]
        graph[a].append(forward)
        graph[b].append([a, 0, len(graph[a]) - 1])
        return forward

    cells, row_used, col_used = {}, {p: 0 for p in winners}, {p: 0 for p in losers}
    for i, winner in enumerate(winners):
        portions = []
        for j, loser in enumerate(losers):
            whole, remainder = divmod(incoming[winner] * outgoing[loser], budget)
            cells[winner, loser] = [whole, None]
            row_used[winner] += whole; col_used[loser] += whole
            if remainder:
                portions.append((remainder, loser, j))
        for _, loser, j in sorted(portions, key=lambda item: (-item[0], item[1])):
            cells[winner, loser][1] = edge(i, len(winners) + j, 1)
    for i, winner in enumerate(winners):
        edge(source, i, incoming[winner] - row_used[winner])
    for j, loser in enumerate(losers):
        edge(len(winners) + j, sink, outgoing[loser] - col_used[loser])
    while True:
        levels = [-1] * len(graph); levels[source] = 0; queue = [source]
        for node in queue:
            for target, capacity, _ in graph[node]:
                if capacity and levels[target] < 0:
                    levels[target] = levels[node] + 1; queue.append(target)
        if levels[sink] < 0:
            break
        cursor = [0] * len(graph)

        def send(node, amount):
            if node == sink:
                return amount
            while cursor[node] < len(graph[node]):
                link = graph[node][cursor[node]]
                target, capacity, reverse = link
                if capacity and levels[target] == levels[node] + 1:
                    sent = send(target, min(amount, capacity))
                    if sent:
                        link[1] -= sent; graph[target][reverse][1] += sent
                        return sent
                cursor[node] += 1
            return 0

        while send(source, budget):
            pass
    flows = {}
    for (winner, loser), (whole, rounding) in cells.items():
        units = (whole + (1 - rounding[1] if rounding else 0)) * SCORE_SCALE
        if units:
            flows[winner, loser] = units; flows[loser, winner] = -units
    return flows


def technical_stats(state, matches, player_id=None):
    if player_id is not None and player_id not in {p["id"] for p in state["players"]}:
        raise AppError("所选选手不存在.")
    records = {p["id"]: {"id": p["id"], "name": p["name"], "matches": 0,
                          "ranked_matches": 0, "rank_sum": 0, "total_units": 0} for p in state["players"]}
    opponents = {p["id"]: {"id": p["id"], "name": p["name"], "matches": 0, "sessions": 0,
                            "wins": 0, "draws": 0, "losses": 0, "difference_units": 0}
                 for p in state["players"] if p["id"] != player_id} if player_id else {}
    for match in matches:
        ranks = finishing_ranks(match)
        chosen = next((e for e in match["entries"] if e["player_id"] == player_id), None)
        flows = score_flows(match) if chosen else {}
        for entry in match["entries"]:
            record = records[entry["player_id"]]
            record["matches"] += entry.get("battle_count", 1)
            if single_result(match, entry):
                record["ranked_matches"] += 1
                record["rank_sum"] += ranks[entry["player_id"]]
            record["total_units"] += entry["total_units"]
            if chosen and match.get("record_kind", "score") == "score" and entry["player_id"] != player_id:
                opponent = opponents[entry["player_id"]]
                opponent["matches"] += min(chosen.get("battle_count", 1), entry.get("battle_count", 1))
                opponent["sessions"] += 1
                difference = ranks[entry["player_id"]] - ranks[player_id]
                opponent["wins" if difference > 0 else "losses" if difference < 0 else "draws"] += 1
                opponent["difference_units"] += flows.get((player_id, entry["player_id"]), 0)
    for record in records.values():
        count = record["matches"]
        ranked_count = record["ranked_matches"]
        record["average_rank"] = record["rank_sum"] / ranked_count if ranked_count else None
        record["average_score_units"] = record["total_units"] / count if count else None
    for opponent in opponents.values():
        count = opponent["wins"] + opponent["draws"] + opponent["losses"]
        opponent["win_rate"] = opponent["wins"] / count if count else None
    return {"matches": sum(m.get("record_kind", "score") == "score" for m in matches),
            "manual_matches": sum(m["scoring_mode"] != "points" and any(single_result(m,e) for e in m["entries"]) for m in matches),
            "players": sorted(records.values(), key=lambda r: (not r["matches"], r["average_rank"] or 0, -r["total_units"], r["id"])),
            "player_id": player_id, "opponents": sorted(
                (r for r in opponents.values() if r["wins"] + r["draws"] + r["losses"]),
                key=lambda r: (-r["matches"], r["id"]))}


def leaderboard(state, matches):
    records = {p["id"]: {"id": p["id"], "name": p["name"], "active": p["active"],
                         "matches": 0, "wins": 0, "base": 0, "extra": 0, "total": 0}
               for p in state["players"]}
    for match in matches:
        ranks = finishing_ranks(match)
        for entry in match["entries"]:
            row = records[entry["player_id"]]
            row["matches"] += entry.get("battle_count", 1)
            row["wins"] += single_result(match, entry) and ranks[entry["player_id"]] == 1
            row["base"] += entry["base_units"]
            row["extra"] += entry["extra_units"]
            row["total"] += entry["total_units"]
    rows = sorted((r for r in records.values() if r["active"] or r["matches"]),
                  key=lambda r: (r["matches"] == 0, -r["total"], -r["wins"], r["id"]))
    previous = None
    for i, row in enumerate(rows):
        if not row["matches"]:
            row["rank"] = "-"
            continue
        row["rank"] = i+1 if previous is None or row["total"] != previous else rows[i-1]["rank"]
        previous = row["total"]
    for row in rows:
        for key in ("base", "extra", "total"):
            row[key] = Decimal(row[key]) / 100  # Original helper returns hundredths.
    return rows


def filter_matches(state, query):
    selected = {positive_id(value, "比赛") for value in query.get("competition", [])}
    if not selected.issubset({c["id"] for c in state.get("competitions", [])}):
        raise AppError("比赛选择包含不存在的比赛, 请刷新后重新选择.")
    start = query.get("start", [""])[0]
    end = query.get("end", [""])[0]
    for value in (start, end):
        if value:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise AppError("筛选日期格式不正确.")
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise AppError("筛选日期格式不正确.") from None
    if start and end and start > end:
        raise AppError("开始日期不能晚于结束日期.")
    return [m for m in state["matches"]
            if query.get("none") != ["1"] and (not selected or m["competition_id"] in selected)
            and (not start or m["played_at"][:10] >= start)
            and (not end or m["played_at"][:10] <= end)]


def ranking_exclusions(state, query):
    known = {player["id"] for player in state["players"]}
    excluded = {positive_id(value) for value in query.get("exclude", [])}
    if not excluded.issubset(known):
        raise AppError("排名选项包含不存在的选手, 请刷新后重试.")
    return excluded


def export_table(state, matches, excluded_ids=()):
    """Build the score-detail export directly from each player's own entries."""
    ordered = sorted(matches, key=lambda match: (match["played_at"], match["id"]))
    excluded = set(excluded_ids)
    entries = {player["id"]: [] for player in state["players"]}
    for match in ordered:
        if match.get("record_kind") == "counts":
            continue
        for entry in match["entries"]:
            entries[entry["player_id"]].append(entry)
    players = []
    for player in state["players"]:
        personal_entries = entries[player["id"]]
        if player["active"] or personal_entries:
            players.append({"id": player["id"], "name": player["name"],
                            "matches": len(personal_entries),
                            "total": sum(entry["total_units"]
                                         for entry in personal_entries)})
    players.sort(key=lambda player: (not player["matches"], -player["total"], player["id"]))
    match_count = max((player["matches"] for player in players), default=0)
    headers = ["排名", "选手"]
    for number in range(1, match_count + 1):
        headers.extend([f"第{number}场基础分", f"第{number}场额外分"])
    headers.append("总分")
    ranked = [player for player in players if player["matches"] and player["id"] not in excluded]
    unranked = [player for player in players if not player["matches"] or player["id"] in excluded]
    for index, player in enumerate(ranked):
        player["rank"] = (ranked[index-1]["rank"] if index and
                          player["total"] == ranked[index-1]["total"] else index+1)
    for player in unranked:
        player["rank"] = "-"
    records = []
    for player in ranked + unranked:
        record = [player["rank"], player["name"]]
        for entry in entries[player["id"]]:
            record.extend([Decimal(entry["base_units"]) / SCORE_SCALE,
                           Decimal(entry["extra_units"]) / SCORE_SCALE])
        record.extend([None, None] * (match_count - player["matches"]))
        record.append(Decimal(player["total"]) / SCORE_SCALE)
        records.append(record)
    return headers, records


def csv_bytes(state, matches, excluded_ids=()):
    headers, records = export_table(state, matches, excluded_ids)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for record in records:
        record = list(record)
        # Only names are user-authored text; scores remain numeric, including negatives.
        if record[1].startswith(("=", "+", "-", "@", "\t", "\r")):
            record[1] = "'" + record[1]
        writer.writerow(record)
    return output.getvalue().encode("utf-8-sig")


def excel_column(number):
    """Convert a 1-based column index to Excel letters, including AA and beyond."""
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


DETAIL_HEADERS = ["格式版本", "对局标识", "比赛标识", "比赛", "比赛备注", "比赛时间", "备注", "计分方式", "rate",
                  "座位", "选手标识", "选手", "点数", "chip", "名次", "原始基础分", "原始额外分", "击飞赏归属", "基础分", "额外分", "当局总分", "计分顺序", "对战数量", "记录类型"]


def detail_table(state, matches):
    players = {p["id"]: p for p in state["players"]}
    competitions = {c["id"]: c for c in state.get("competitions", [])}
    rows = []
    for match in sorted(matches, key=lambda m: (m["played_at"], m["id"])):
        competition = competitions.get(match.get("competition_id"), {})
        ranks = finishing_ranks(match)
        positions = {pid: i+1 for i, pid in enumerate(match["rank_order"])}
        for entry in sorted(match["entries"], key=lambda e: e["seat"]):
            pid = entry["player_id"]
            recipient = match["knockouts"].get(str(pid))
            rows.append([tables.FORMAT, match.get("public_key", ""), competition.get("public_key", ""),
                         competition.get("name", "默认比赛"), competition.get("note", ""), match["played_at"], match["note"],
                         match["scoring_mode"], Decimal(match["rate"]), entry["seat"]+1, players[pid].get("public_key", ""),
                         entry["name"], entry["points"], entry["chip"], ranks[pid], Decimal(entry["base_cents"])/100,
                         Decimal(entry["extra_cents"])/100, players[recipient]["name"] if recipient else "",
                         Decimal(entry["base_units"])/SCORE_SCALE, Decimal(entry["extra_units"])/SCORE_SCALE,
                         Decimal(entry["total_units"])/SCORE_SCALE, positions.get(pid), entry.get("battle_count", 1), match.get("record_kind", "score")])
    return DETAIL_HEADERS, rows


def detail_csv_bytes(state, matches):
    return tables.csv_table(*detail_table(state, matches), text_columns=(3,4,6,11,17))


def excel_bytes(state, matches, excluded_ids=(), include_details=True):
    """Preserve the compact personal score table and add lossless round details."""
    headers, records = export_table(state, matches, excluded_ids)
    if len(headers) > 16384:
        raise AppError("所选范围超过 Excel 列数上限, 请缩小比赛日期范围后再导出.")
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    final_column = excel_column(len(headers))
    score_end_column = excel_column(len(headers) - 1)
    last_row = len(records) + 1

    def cell(ref, value, style=0, formula=None):
        if formula is not None:
            return f'<c r="{ref}" s="{style}"><f>{escape(formula)}</f><v>{value}</v></c>'
        if value is None:
            return f'<c r="{ref}" s="{style}"/>'
        if isinstance(value, (int, Decimal)):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        # inlineStr prevents names beginning with '=' from executing as formulas.
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'

    name_width = max(18, min(52, max((sum(2 if ord(c) > 127 else 1 for c in row[1]) + 3
                                    for row in records), default=18)))
    widths = [9, name_width] + [18] * (len(headers) - 3) + [16]
    columns = ''.join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
                      for i, width in enumerate(widths, 1))
    head = ''.join(cell(f'{excel_column(i)}1', title, 1)
                   for i, title in enumerate(headers, 1))
    body = []
    for row_number, record in enumerate(records, 2):
        cells = [cell(f"A{row_number}", record[0], 5), cell(f"B{row_number}", record[1])]
        for column_number, value in enumerate(record[2:-1], 3):
            style = 2 if ((column_number - 3) // 2) % 2 == 0 else 4
            cells.append(cell(f"{excel_column(column_number)}{row_number}", value, style))
        formula = f"SUM(C{row_number}:{score_end_column}{row_number})" if matches else "0"
        cells.append(cell(f"{final_column}{row_number}", record[-1], 3, formula))
        body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    summary = (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns}">'
        f'<dimension ref="A1:{final_column}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane xSplit="2" ySplit="1" topLeftCell="C2" activePane="bottomRight" state="frozen"/>'
        '<selection pane="topRight" activeCell="C1" sqref="C1"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>'
        '<selection pane="bottomRight" activeCell="C2" sqref="C2"/>'
        '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="26"/>'
        f'<cols>{columns}</cols><sheetData><row r="1" ht="34" customHeight="1">{head}</row>'
        f'{"".join(body)}</sheetData><autoFilter ref="A1:{final_column}{last_row}"/>'
        '<pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '</worksheet>'
    )
    styles = f'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{ns}">
<numFmts count="3"><numFmt numFmtId="164" formatCode="+0.####;-0.####;0"/><numFmt numFmtId="165" formatCode="0.####"/><numFmt numFmtId="166" formatCode="0.00%"/></numFmts>
<fonts count="3"><font><sz val="11"/><color rgb="FF293247"/><name val="Microsoft YaHei"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>
<font><b/><sz val="11"/><color rgb="FF5878F4"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF344A87"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF4F7FD"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="2" fillId="3" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''
    relationship_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    office_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    files = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": f'<Relationships xmlns="{relationship_ns}"><Relationship Id="rId1" Type="{office_rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="{office_rel}"><sheets>'
                           '<sheet name="比赛统计" sheetId="1" r:id="rId1"/>'
                           '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{relationship_ns}">'
                         + f'<Relationship Id="rId1" Type="{office_rel}/worksheet" Target="worksheets/sheet1.xml"/>'
                         + f'<Relationship Id="rId2" Type="{office_rel}/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": summary,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in files.items():
            archive.writestr(name, xml.encode("utf-8"))
    result = output.getvalue()
    if include_details:
        result = tables.append_sheets(result, [("对局明细", tables.sheet_xml(*detail_table(state, matches), signed=(13,15,16,18,19,20)))])
    return result


def technical_excel_bytes(state, matches, player_id=None):
    stats = technical_stats(state, matches, player_id)
    headers = ["选手", "半庄回数", "半庄平均得分", "累计得分"]
    rows = [[r["name"], r["matches"],
             Decimal(r["total_units"])/SCORE_SCALE/r["matches"] if r["matches"] else None,
             Decimal(r["total_units"])/SCORE_SCALE] for r in stats["players"]]
    sheets = [("技术统计", tables.sheet_xml(headers, rows, signed=(2,3), freeze=1))]
    if player_id is not None:
        name = next(p["name"] for p in state["players"] if p["id"] == player_id)
        headers = ["查询选手", "对手", "同桌局数", "同桌场数", "胜", "平", "负", "胜率", "累计分数流向"]
        rows = [[name, r["name"], r["matches"], r["sessions"], r["wins"], r["draws"], r["losses"],
                 Decimal(r["wins"])/(r["wins"]+r["draws"]+r["losses"]),
                 Decimal(r["difference_units"])/SCORE_SCALE] for r in stats["opponents"]]
        sheets.append(("对手统计", tables.sheet_xml(headers, rows, signed=(8,), percentages=(7,))))
    notes = [["范围", "已选比赛与日期范围内的成绩和数量汇总记录"],
             ["同桌局数", "累加共同成绩记录中双方对战数量的较小值. 场数为 0 的历史记录不推算同桌局数. 未交手的对手不列出."],
             ["同桌场数", "每条共同成绩记录计 1 场, 不乘对战数量. 独立数量汇总不形成交手记录."],
             ["胜平负与胜率", "每条共同成绩记录比较一次名次, 不乘对战数量. 胜率 = 胜 / (胜 + 平 + 负), 平局计入分母."],
             ["累计分数流向", "正数表示从对手获得, 负数表示付给对手. 按最终得分的正负比例分配为整数, 每笔双方收支相抵. 整数正负得分保留各自总额, 小数使用最大余数法平衡取整. 非零和记录仅分配正负双方可匹配的部分."],
             ["半庄回数", "累加每条记录中填写的对战数量, 包括独立的数量汇总记录. 缺省为 1, 已在别处汇总的历史成绩填 0."],
             ["半庄平均得分", "累计得分 / 半庄回数; 数量汇总不增加成绩, 不推算名次和胜负. 绘图不增加局数或改变平均值."]]
    sheets.append(("统计口径", tables.sheet_xml(["项目", "说明"], notes, widths=[18,100], freeze=1)))
    return tables.append_sheets(excel_bytes(state, [], include_details=False), sheets, replace=True)


def import_template_bytes(state):
    key = str(uuid.uuid4())
    date = datetime.now().isoformat(timespec="minutes")
    rows = [[tables.FORMAT, key, "", "导入比赛示例", "", date, "示例: 可修改或删除后填写实际对局", "points", 5,
             i+1, "", f"示例选手 {chr(65+i)}", points, chip, i+1, base, 0, "", base*5, 0, base*5, i+1, 1, "score"]
            for i, (points, chip, base) in enumerate(((500,2,41), (400,-1,-3), (150,-1,-38))) ]
    return tables.append_sheets(excel_bytes(state, [], include_details=False),
                                [("对局明细", tables.sheet_xml(DETAIL_HEADERS, rows, signed=(13,15,16,18,19,20)))], replace=True)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, demo=False):
        self.store = store
        self.app_token = secrets.token_urlsafe(32)
        self.demo = demo
        super().__init__(address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, message, *args):
        logging.debug(message, *args)

    def send_bytes(self, body, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data: blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/state":
                state = self.server.store.state()
                state.update(token=self.server.app_token, demo=self.server.demo)
                self.send_json(state)
            elif path in ("/api/export.xlsx", "/api/export.csv", "/api/export-details.csv", "/api/technical.xlsx", "/api/technical"):
                state = self.server.store.state()
                query = parse_qs(parsed.query)
                matches = filter_matches(state, query)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                if path.startswith("/api/technical"):
                    pid = positive_id(query["player"][0]) if query.get("player") else None
                    if path.endswith("xlsx"):
                        self.send_bytes(technical_excel_bytes(state, matches, pid),
                                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        filename=f"scoreboard-technical-{stamp}.xlsx")
                    else:
                        self.send_json(technical_stats(state, matches, pid))
                elif path == "/api/export-details.csv":
                    self.send_bytes(detail_csv_bytes(state, matches), "text/csv; charset=utf-8",
                                    filename=f"scoreboard-round-details-{stamp}.csv")
                elif path.endswith("xlsx"):
                    excluded = ranking_exclusions(state, query)
                    self.send_bytes(excel_bytes(state, matches, excluded),
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    filename=f"scoreboard-match-scores-{stamp}.xlsx")
                else:
                    excluded = ranking_exclusions(state, query)
                    self.send_bytes(csv_bytes(state, matches, excluded), "text/csv; charset=utf-8",
                                    filename=f"scoreboard-match-scores-{stamp}.csv")
            elif path == "/api/import-template.xlsx":
                self.send_bytes(import_template_bytes(self.server.store.state()),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                filename="scoreboard-import-template.xlsx")
            elif path == "/api/backup":
                self.send_bytes(self.server.store.backup(), "application/vnd.sqlite3",
                                filename=f"scoreboard-backup-{datetime.now():%Y%m%d-%H%M%S}.sqlite3")
            elif path == "/health":
                self.send_json({"ok": True})
            elif path in ("/", "/index.html", "/static/styles.css", "/static/app.js", "/static/favicon.svg"):
                if path in ("/", "/index.html"):
                    name, content_type = "index.html", "text/html; charset=utf-8"
                else:
                    name = path.removeprefix("/static/")
                    content_type = {"styles.css": "text/css; charset=utf-8",
                                    "app.js": "text/javascript; charset=utf-8",
                                    "favicon.svg": "image/svg+xml"}[name]
                self.send_bytes((ROOT / "static" / name).read_bytes(), content_type)
            else:
                self.send_json({"error": "页面不存在."}, 404)
        except AppError as error:
            self.send_json({"error": str(error)}, error.status)
        except Exception:
            logging.exception("GET request failed")
            self.send_json({"error": "读取失败, 请稍后重试."}, 500)

    def mutate(self, method):
        try:
            token = self.headers.get("X-App-Token", "")
            if not token.isascii() or not secrets.compare_digest(token, self.server.app_token):
                raise AppError("页面连接已更新, 请刷新后重试.", 403)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise AppError("不允许来自其他页面的修改请求.", 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise AppError("请求长度不正确.") from None
            path = urlparse(self.path).path
            limit = 8*1024*1024 if path in ("/api/import", "/api/import/preview", "/api/multi-scores/preview") else 524288
            if not 0 < length <= limit:
                raise AppError("请求内容过大或为空.", 413)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise AppError("请求格式不正确.") from None
            if not isinstance(data, dict):
                raise AppError("请求须为 JSON 对象.")
            player = re.fullmatch(r"/api/players/([1-9]\d*)", path)
            match = re.fullmatch(r"/api/matches/([1-9]\d*)", path)
            competition = re.fullmatch(r"/api/competitions/([1-9]\d*)", path)
            store = self.server.store
            status = 200
            result = {"ok": True}
            if method == "POST" and path == "/api/players":
                result["id"] = store.create_player(data)
                status = 201
            elif method == "POST" and path == "/api/competitions":
                result["id"] = store.create_competition(data)
                status = 201
            elif method == "PATCH" and competition:
                store.update_competition(int(competition[1]), data)
            elif method == "DELETE" and competition:
                result["deleted"] = store.delete_competition(int(competition[1]), data)
            elif method == "PATCH" and player:
                store.update_player(int(player[1]), data)
            elif method == "POST" and path == "/api/matches":
                result["id"] = store.save_match(data)
                status = 201
            elif method == "PUT" and match:
                result["id"] = store.save_match(data, int(match[1]))
            elif method == "DELETE" and match:
                store.delete_match(int(match[1]), data.get("revision"))
            elif method == "POST" and path == "/api/matches/bulk-delete":
                result["deleted"] = store.delete_matches(data)
            elif method == "POST" and path == "/api/matches/bulk-move":
                result["moved"] = store.move_matches(data)
            elif method == "POST" and path in ("/api/import", "/api/import/preview"):
                result["import"] = store.import_rounds(data, commit=path == "/api/import")
            elif method == "POST" and path == "/api/multi-scores/preview":
                result["draft"] = store.preview_multi_scores(data)
            elif method == "POST" and path == "/api/players/bulk-delete":
                result["deleted"] = store.delete_players(data)
            elif method == "PUT" and path == "/api/settings":
                result["settings"] = store.update_settings(data)
            else:
                raise AppError("操作不存在.", 404)
            self.send_json(result, status)
        except AppError as error:
            self.send_json({"error": str(error)}, error.status)
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            logging.exception("Database request failed")
            self.send_json({"error": "数据暂时无法写入, 请稍后重试."}, 503)
        except Exception:
            logging.exception("Write request failed")
            self.send_json({"error": "保存失败, 请稍后重试."}, 500)

    def do_POST(self):
        self.mutate("POST")

    def do_PATCH(self):
        self.mutate("PATCH")

    def do_PUT(self):
        self.mutate("PUT")

    def do_DELETE(self):
        self.mutate("DELETE")


def main():
    parser = argparse.ArgumentParser(description="Vibe Scoreboard | 本地比赛记分工具")
    parser.add_argument("--port", type=int, default=8765, help="端口, 默认 8765")
    parser.add_argument("--data-dir", type=Path, help="数据目录, 默认为项目下的 data/")
    parser.add_argument("--demo", action="store_true", help="使用独立的演示数据库")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口须在 1 到 65535 之间.")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    data_dir = args.data_dir or ROOT / ("data-demo" if args.demo else "data")
    store = ScoreStore(data_dir / "scoreboard.sqlite3")
    if args.demo:
        store.seed_demo()
    try:
        server = AppServer(("127.0.0.1", args.port), store, args.demo)
    except OSError as error:
        print(f"\n无法启动: {error}\n请尝试: python app.py --port {args.port+1}\n")
        return 1
    address = f"http://127.0.0.1:{args.port}"
    print(f"\n  VIBE SCOREBOARD\n  本机浏览器地址: {address}\n  数据文件: {store.path}")
    print("  请保持此窗口运行, 按 Ctrl+C 停止服务.\n")
    if not args.no_browser:
        timer = threading.Timer(0.7, lambda: webbrowser.open(address))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止, 数据已保留.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
