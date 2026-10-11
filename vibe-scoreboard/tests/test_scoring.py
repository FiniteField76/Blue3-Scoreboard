"""Point scoring, migration, bulk deletion and HTTP regression checks."""

import csv
import io
import json
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path

from app import AppError, ScoreStore, SCORE_SCALE, csv_bytes, export_table, point_scores
import test_app as legacy_tests

fixture = legacy_tests.fixture


def points_fixture(points=(500, 400, 150), chips=(2, -1, -1), rate="5", key="points-0001", **extra):
    record = {"played_at": "2026-10-10T19:30", "request_key": key, "scoring_mode": "points",
              "rate": rate, "entries": [{"player_id": i+1, "points": p, "chip": c, "extra": "0"}
                                        for i, (p, c) in enumerate(zip(points, chips))]}
    record.update(extra)
    return record


class PointTests(unittest.TestCase):
    setUp = legacy_tests.StoreTests.setUp
    tearDown = legacy_tests.StoreTests.tearDown

    def match(self):
        return self.store.state()["matches"][0]

    def totals(self):
        return [Decimal(e["total_units"]) / SCORE_SCALE for e in self.match()["entries"]]

    def test_user_example_chip_and_rate(self):
        self.store.save_match(points_fixture())
        self.assertEqual(self.totals(), [205, -15, -190])
        self.assertEqual([e["base_cents"] for e in self.match()["entries"]], [4100, -300, -3800])

    def test_user_example_zero_points_and_transfer(self):
        self.store.save_match(points_fixture((830, 290, 0), (0, 0, 0), knockouts={"3": 2}))
        self.assertEqual(self.totals(), [355, -55, -300])
        self.assertEqual(self.match()["knockouts"], {"3": 2})
        self.assertEqual(sum(e["base_units"] for e in self.match()["entries"]), 0)

    def test_second_place_strict_boundary(self):
        for second, expected in ((399, -1010), (400, 0), (401, 10)):
            entries = points_fixture((800, second, 100), (0, 0, 0))["entries"]
            self.assertEqual(point_scores(entries)[0][2], expected)

    def test_total_points_are_not_validated(self):
        self.store.save_match(points_fixture((830, 290, 50), (0, 0, 0)))
        self.assertEqual(sum(e["points"] for e in self.match()["entries"]), 1170)
        self.assertEqual(sum(e["base_cents"] for e in self.match()["entries"]), 0)

    def test_integer_points_chips_and_chip_balance(self):
        for field, invalid in (("points", "400.5"), ("points", True), ("points", "1e2"),
                               ("points", None), ("chip", "1.5"), ("chip", "0.0"), ("chip", True)):
            record = points_fixture()
            record["entries"][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(AppError):
                self.store.save_match(record)
        with self.assertRaisesRegex(AppError, "chip 之和"):
            self.store.save_match(points_fixture(chips=(1, 0, 0)))
        self.assertFalse(self.store.state()["matches"])

    def test_knockout_recipient_required_and_must_be_another_player(self):
        for awards in (None, {}, {"3": 3}, {"3": 99}, {"2": 1}, {"3": 2, "1": 2}):
            with self.subTest(awards=awards), self.assertRaises(AppError):
                self.store.save_match(points_fixture((830, 290, 0), (0, 0, 0), knockouts=awards))
        self.assertFalse(self.store.state()["matches"])

    def test_multiple_zero_players_each_have_an_award(self):
        record = points_fixture((1050, 0, 0), (0, 0, 0), rank_order=[1, 2, 3], knockouts={"2": 1, "3": 1})
        self.store.save_match(record)
        self.assertEqual(self.totals(), [600, -300, -300])
        self.assertEqual(sum(e["base_units"] for e in self.match()["entries"]), 0)

    def test_top_tie_splits_third_loss_with_or_without_order(self):
        record = points_fixture((500, 500, 50), (0, 0, 0))
        for order in (None, [1, 2, 3], [2, 1, 3]):
            with self.subTest(order=order):
                bases, _, _ = point_scores(record["entries"], order)
                self.assertEqual([bases[pid] for pid in (1,2,3)], [2250, 2250, -4500])
        for order in ([1, 1, 3], [3, 1, 2], [1, 2, 99]):
            record["rank_order"] = order
            with self.subTest(order=order), self.assertRaises(AppError):
                self.store.save_match(record)
        record.pop("rank_order")
        self.store.save_match(record)
        self.assertEqual(self.totals(), [Decimal("112.5"), Decimal("112.5"), -225])
        self.assertEqual([e["finish_rank"] for e in self.match()["entries"]], [1, 1, 3])

    def test_top_tie_split_keeps_fractional_precision(self):
        record = points_fixture((500, 500, 51), (0, 0, 0), rate="0.25")
        record["entries"][0]["extra"] = "0.01"
        self.store.save_match(record)
        self.assertEqual([e["base_cents"] for e in self.match()["entries"]], [2245,2245,-4490])
        self.assertEqual(self.totals(), [Decimal("5.615"), Decimal("5.6125"), Decimal("-11.225")])

    def test_top_tie_chip_and_knockout_apply_after_the_split(self):
        self.store.save_match(points_fixture((500,500,50), (-100,100,0)))
        self.assertEqual(self.totals(), [Decimal("-1387.5"), Decimal("1612.5"), -225])
        self.assertEqual([e["finish_rank"] for e in self.match()["entries"]], [1,1,3])
        self.store.save_match(points_fixture((500,500,0), (0,0,0), key="top-tie-knockout", knockouts={"3":2}))
        self.assertEqual(self.totals(), [125,175,-300])
        self.assertEqual(sum(e["base_cents"] for e in self.match()["entries"]), 0)

    def test_bottom_ties_keep_the_original_calculation(self):
        for second, expected in ((225, [275,Decimal("-137.5"),Decimal("-137.5")]),
                                 (400, [50,-50,0])):
            record = points_fixture((600,second,second), (0,0,0), key=f"bottom-tie-{second}")
            with self.subTest(points=second):
                with self.assertRaisesRegex(AppError, "点数相同"):
                    self.store.save_match(record)
                record["rank_order"] = [1,3,2]
                self.store.save_match(record)
                self.assertEqual(self.totals(), expected)
                self.assertEqual([e["finish_rank"] for e in self.match()["entries"]], [1,2,2])

    def test_existing_top_tie_scores_are_repaired_once(self):
        record = points_fixture((500,500,50), (-10,10,0))
        record["entries"][0]["extra"] = "0.15"
        mid = self.store.save_match(record)
        other = self.store.save_match(fixture(key="unchanged-manual"))
        before = self.store.state()
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM metadata WHERE key='top_tie_scoring_v2'")
            # Values produced by the previous first/second-place calculation.
            db.execute("UPDATE entries SET base_cents=500 WHERE match_id=? AND player_id=1", (mid,))
            db.execute("UPDATE entries SET base_cents=4000 WHERE match_id=? AND player_id=2", (mid,))
        repaired = ScoreStore(self.store.path).state()
        fixed = next(m for m in repaired["matches"] if m["id"] == mid)
        original = next(m for m in before["matches"] if m["id"] == mid)
        self.assertEqual([e["total_units"] for e in fixed["entries"]], [-367500,2625000,-2250000])
        self.assertEqual(fixed["revision"], original["revision"]+1)
        for field in ("public_key","played_at","note","rate","rank_order","knockouts","competition_id"):
            self.assertEqual(fixed[field], original[field])
        self.assertEqual(fixed["entries"], original["entries"])
        self.assertEqual(next(m for m in repaired["matches"] if m["id"] == other),
                         next(m for m in before["matches"] if m["id"] == other))
        self.assertEqual(ScoreStore(self.store.path).state(), repaired)

    def test_fractional_rate_and_extra_are_exact(self):
        record = points_fixture((501, 400, 149), (0, 0, 0), rate="0.25")
        record["entries"][0]["extra"] = "0.01"
        self.store.save_match(record)
        self.assertEqual(self.totals(), [Decimal("8.7775"), 0, Decimal("-8.775")])
        self.assertEqual(sum(e["base_units"] for e in self.match()["entries"]), 0)
        state = self.store.state()
        _, records = export_table(state, state["matches"])
        self.assertEqual(records[0][2:], [Decimal("8.775"), Decimal("0.0025"), Decimal("8.7775")])

    def test_global_rate_is_default_and_history_has_its_own_rate(self):
        self.store.save_match(points_fixture())
        old = self.match()
        self.store.update_settings({"rate": "2.5"})
        self.assertEqual(self.match(), old)
        new = points_fixture(key="points-0002")
        del new["rate"]
        self.store.save_match(new)
        self.assertEqual(self.match()["rate"], "2.5")
        self.assertEqual(self.totals(), [Decimal("102.5"), Decimal("-7.5"), -95])
        self.assertEqual(ScoreStore(self.store.path).state()["settings"], {"rate": "2.5"})
        for invalid in (0, -1, "0.001", True, "NaN", 10001):
            with self.subTest(rate=invalid), self.assertRaises(AppError):
                self.store.update_settings({"rate": invalid})

    def test_point_edit_and_idempotency_keep_raw_inputs(self):
        record = points_fixture()
        mid = self.store.save_match(record)
        self.assertEqual(self.store.save_match(record), mid)
        record["entries"][0]["points"] = 501  # Same resulting base, different raw input.
        with self.assertRaisesRegex(AppError, "另一局"):
            self.store.save_match(record)
        record["revision"] = 1
        record["rate"] = "2"
        self.store.save_match(record, mid)
        self.assertEqual(self.totals(), [82, -6, -76])
        self.assertEqual(self.match()["revision"], 2)
        self.assertEqual(self.match()["entries"][0]["points"], 501)

    def test_backend_ignores_client_base_in_points_mode(self):
        record = points_fixture()
        for e in record["entries"]:
            e["base"] = "999"
        self.store.save_match(record)
        self.assertEqual(self.totals(), [205, -15, -190])

    def test_effective_scores_export_with_requested_columns(self):
        self.store.save_match(points_fixture())
        state = self.store.state()
        rows = list(csv.reader(io.StringIO(csv_bytes(state, state["matches"], {1}).decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["排名", "选手", "第1场基础分", "第1场额外分", "总分"])
        self.assertEqual(rows[-1], ["-", "选手 A", "205", "0", "205"])
        self.assertEqual(rows[1][-1], "-15")

    def test_original_database_migrates_without_changing_scores(self):
        path = Path(self.temp.name) / "original.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE players(id INTEGER PRIMARY KEY, name TEXT UNIQUE, active INTEGER);
                CREATE TABLE matches(id INTEGER PRIMARY KEY, played_at TEXT, note TEXT, revision INTEGER,
                                     request_key TEXT UNIQUE, created_at TEXT, updated_at TEXT);
                CREATE TABLE entries(match_id INTEGER, player_id INTEGER, seat INTEGER,
                                     base_cents INTEGER, extra_cents INTEGER, PRIMARY KEY(match_id,player_id));
                INSERT INTO players VALUES(1,'旧 A',1),(2,'旧 B',1),(3,'旧 C',0);
                INSERT INTO matches VALUES(7,'2026-10-09T19:30','历史',3,'legacy-0001','old','old');
                INSERT INTO entries VALUES(7,1,0,3000,500),(7,2,1,-1000,0),(7,3,2,-2000,200);
            """)
        migrated = ScoreStore(path)
        state = migrated.state()
        match = state["matches"][0]
        self.assertEqual((match["id"], match["revision"], match["rate"], match["scoring_mode"]), (7, 3, "1", "manual"))
        self.assertEqual([e["total_units"] for e in match["entries"]], [350000, -100000, -180000])
        self.assertTrue(all(e["points"] is None and e["chip"] is None for e in match["entries"]))
        migrated.update_settings({"rate": "10"})
        self.assertEqual(migrated.state()["matches"], state["matches"])


class BulkTests(unittest.TestCase):
    setUp = legacy_tests.StoreTests.setUp
    tearDown = legacy_tests.StoreTests.tearDown

    def snapshot(self):
        return [{"id": m["id"], "revision": m["revision"]} for m in self.store.state()["matches"]]

    def test_delete_selected_matches_only_without_orphans(self):
        mids = [self.store.save_match(fixture(key=f"bulk-match-{i}")) for i in range(3)]
        self.assertEqual(self.store.delete_matches({"matches": [{"id": mids[0], "revision": 1}, {"id": mids[2], "revision": 1}]}), 2)
        self.assertEqual([m["id"] for m in self.store.state()["matches"]], [mids[1]])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 3)
        self.assertEqual(len(self.store.state()["players"]), 3)

    def test_stale_bulk_match_delete_is_atomic(self):
        self.store.save_match(fixture(key="batch-match-1"))
        self.store.save_match(fixture(key="batch-match-2"))
        selected = self.snapshot()
        selected[-1]["revision"] = 2
        original = self.store.state()
        with self.assertRaisesRegex(AppError, "未删除任何"):
            self.store.delete_matches({"matches": selected})
        self.assertEqual(self.store.state(), original)

    def test_delete_player_removes_entire_related_matches_only(self):
        ids = [self.store.create_player({"name": name}) for name in ("D", "E", "F")]
        one = self.store.save_match(fixture(key="related-match"))
        unrelated = fixture(key="unrelated-match")
        for e, pid in zip(unrelated["entries"], ids):
            e["player_id"] = pid
        keep = self.store.save_match(unrelated)
        self.store.update_player(1, {"active": False})
        result = self.store.delete_players({"ids": [1], "matches": [{"id": one, "revision": 1}]})
        self.assertEqual(result, {"players": 1, "matches": 1})
        self.assertEqual([m["id"] for m in self.store.state()["matches"]], [keep])
        self.assertEqual([p["id"] for p in self.store.state()["players"]], [2, 3, *ids])
        with self.store.connection() as db:
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_new_related_match_rejects_old_delete_preview(self):
        self.store.save_match(fixture())
        selected = self.snapshot()
        self.store.save_match(fixture(key="new-related-match"))
        original = self.store.state()
        with self.assertRaisesRegex(AppError, "关联比赛已变化"):
            self.store.delete_players({"ids": [1, 2], "matches": selected})
        self.assertEqual(self.store.state(), original)

    def test_all_players_deleted_stay_empty_after_restart(self):
        self.store.save_match(fixture())
        self.assertEqual(self.store.delete_players({"ids": [1,2,3], "matches": self.snapshot()}), {"players": 3, "matches": 1})
        restarted = ScoreStore(self.store.path).state()
        self.assertEqual((restarted["players"], restarted["matches"]), ([], []))
        pid = self.store.create_player({"name": "新选手"})
        self.assertGreater(pid, 3)

    def test_delete_unplayed_player_and_reject_invalid_selections(self):
        pid = self.store.create_player({"name": "未参赛"})
        self.assertEqual(self.store.delete_players({"ids": [pid], "matches": []}), {"players": 1, "matches": 0})
        original = self.store.state()
        for body in ({"ids": [], "matches": []}, {"ids": [1,1], "matches": []}, {"ids": [1,99], "matches": []}):
            with self.subTest(body=body), self.assertRaises(AppError):
                self.store.delete_players(body)
        self.assertEqual(self.store.state(), original)


class NewEndpointTests(unittest.TestCase):
    setUp = legacy_tests.HttpTests.setUp
    tearDown = legacy_tests.HttpTests.tearDown
    request = legacy_tests.HttpTests.request

    def test_rate_points_and_bulk_endpoints(self):
        self.assertEqual(self.request("/api/settings", "PUT", {"rate": "5"})[0], 200)
        status, body, _ = self.request("/api/matches", "POST", points_fixture())
        self.assertEqual(status, 201)
        mid = json.loads(body)["id"]
        state = json.loads(self.request("/api/state")[1])
        self.assertEqual(state["matches"][0]["entries"][0]["total_units"], 205*SCORE_SCALE)
        selected = {"matches": [{"id": mid, "revision": 1}]}
        self.assertEqual(self.request("/api/matches/bulk-delete", "POST", selected, token=False)[0], 403)
        self.assertEqual(self.request("/api/matches/bulk-delete", "POST", selected)[0], 200)
        self.assertEqual(self.request("/api/players/bulk-delete", "POST", {"ids": [1,2,3], "matches": []})[0], 200)
        self.assertFalse(json.loads(self.request("/api/state")[1])["players"])


if __name__ == "__main__":
    unittest.main()
