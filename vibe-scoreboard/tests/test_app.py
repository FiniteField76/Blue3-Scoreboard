"""Run with: python -m unittest discover -s tests -v. No test dependencies."""

import concurrent.futures
import csv
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app import AppError, AppServer, ScoreStore, csv_bytes, excel_bytes, export_table, leaderboard, score_cents


def fixture(bases=("30", "-10", "-20"), extras=("5", "0", "2"), key="request-0001"):
    return {"played_at": "2026-10-09T19:30", "note": "测试比赛", "request_key": key,
            "entries": [{"player_id": i+1, "base": b, "extra": e}
                        for i, (b, e) in enumerate(zip(bases, extras))]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ScoreStore(Path(self.temp.name) / "scores.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_decimal_zero_sum_and_independent_extra(self):
        self.store.save_match(fixture(("0.1", "0.2", "-0.3"), ("100", "1.55", "-2")))
        state = self.store.state()
        self.assertEqual(sum(e["base_cents"] for e in state["matches"][0]["entries"]), 0)
        self.assertEqual(sum(r["total"] for r in leaderboard(state, state["matches"])), 9955)

    def test_invalid_sum_never_persists(self):
        with self.assertRaisesRegex(AppError, "必须为 0"):
            self.store.save_match(fixture(("30", "-10", "-19.99")))
        self.assertEqual(self.store.state()["matches"], [])

    def test_distinct_three_players_required(self):
        for length in (0, 2, 4):
            record = fixture()
            record["entries"] = (record["entries"] * 2)[:length]
            with self.assertRaises(AppError):
                self.store.save_match(record)
        record = fixture()
        record["entries"][1]["player_id"] = 1
        with self.assertRaisesRegex(AppError, "不能重复"):
            self.store.save_match(record)
        record["entries"][1]["player_id"] = 99
        with self.assertRaises(AppError):
            self.store.save_match(record)
        self.assertFalse(self.store.state()["matches"])

    def test_invalid_score_formats_and_bounds(self):
        for value in (True, None, "", "1.001", "nan", "Infinity", "1e2", "1000000.01", "1,000"):
            with self.subTest(value=value), self.assertRaises(AppError):
                score_cents(value)
        self.assertEqual(score_cents("-.25"), -25)
        self.assertEqual(score_cents("+1000000"), 100000000)

    def test_idempotent_submit_and_conflicting_reuse(self):
        one = self.store.save_match(fixture())
        two = self.store.save_match(fixture())
        self.assertEqual(one, two)
        self.assertEqual(len(self.store.state()["matches"]), 1)
        with self.assertRaisesRegex(AppError, "另一局"):
            self.store.save_match(fixture(extras=("8", "0", "0")))

    def test_edit_and_delete_recompute_without_orphans(self):
        match_id = self.store.save_match(fixture())
        update = fixture(("-5", "10", "-5"), ("0", "3", "0"))
        update["revision"] = 1
        self.store.save_match(update, match_id)
        state = self.store.state()
        rows = leaderboard(state, state["matches"])
        self.assertEqual(state["matches"][0]["revision"], 2)
        self.assertEqual(rows[0]["id"], 2)
        self.assertEqual(rows[0]["total"], 1300)
        self.assertEqual(rows[0]["wins"], 1)
        with self.assertRaises(AppError):
            self.store.delete_match(match_id, 1)
        self.store.delete_match(match_id, 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
        self.assertEqual(sum(r["total"] for r in leaderboard(self.store.state(), [])), 0)

    def test_invalid_edit_keeps_original(self):
        match_id = self.store.save_match(fixture())
        original = self.store.state()
        update = fixture(("99", "0", "0"))
        update["revision"] = 1
        with self.assertRaises(AppError):
            self.store.save_match(update, match_id)
        self.assertEqual(self.store.state(), original)

    def test_concurrent_edits_reject_stale_revision(self):
        match_id = self.store.save_match(fixture())
        barrier = threading.Barrier(2)

        def edit(extra):
            record = fixture(extras=(extra, "0", "0"))
            record["revision"] = 1
            barrier.wait(timeout=3)
            try:
                self.store.save_match(record, match_id)
                return 200
            except AppError as error:
                return error.status
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(edit, ("8", "9")))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(self.store.state()["matches"][0]["revision"], 2)

    def test_rename_archive_and_restore_preserve_history(self):
        match_id = self.store.save_match(fixture())
        self.store.update_player(1, {"name": "Alice", "active": False})
        with self.assertRaises(AppError):
            self.store.create_player({"name": "alice"})
        self.assertEqual(self.store.state()["matches"][0]["entries"][0]["name"], "Alice")
        with self.assertRaises(AppError):
            self.store.save_match(fixture(key="request-0002"))
        update = fixture()
        update["revision"] = 1
        self.store.save_match(update, match_id)  # Editing that player's old match is allowed.
        self.store.update_player(1, {"active": True})
        self.store.save_match(fixture(key="request-0003"))
        self.assertEqual(len(self.store.state()["matches"]), 2)

    def test_ties_and_inactive_players(self):
        self.store.save_match(fixture(("10", "10", "-20"), ("0", "0", "0")))
        self.store.update_player(1, {"active": False})
        state = self.store.state()
        rows = leaderboard(state, state["matches"])
        self.assertEqual([r["rank"] for r in rows], [1, 1, 3])
        self.assertEqual([r["wins"] for r in rows], [1, 1, 0])

    def test_unplayed_players_do_not_outrank_participants(self):
        fourth = self.store.create_player({"name": "未参赛"})
        self.store.save_match(fixture(("0", "0", "0"), ("-1", "-2", "-3")))
        state = self.store.state()
        rows = leaderboard(state, state["matches"])
        self.assertEqual(rows[-1]["id"], fourth)
        self.assertEqual(rows[-1]["rank"], "-")
        self.assertEqual(rows[0]["total"], -100)

    def test_restart_and_backup_include_wal(self):
        self.store.save_match(fixture())
        restored = ScoreStore(self.store.path)
        self.assertEqual(restored.state(), self.store.state())
        backup = Path(self.temp.name) / "backup.sqlite3"
        backup.write_bytes(self.store.backup())
        self.assertEqual(ScoreStore(backup).state(), self.store.state())

    def test_export_values_formulas_and_safe_text(self):
        self.store.update_player(1, {"name": "=HYPERLINK(test)"})
        record = fixture(("0.1", "0.2", "-0.3"), ("1.25", "0", "0"))
        record["note"] = "=1+2 & <note>"
        self.store.save_match(record)
        state = self.store.state()
        exported = excel_bytes(state, state["matches"])
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(io.BytesIO(exported)) as z:
            for name in z.namelist():
                ET.fromstring(z.read(name))
            self.assertIn("xl/worksheets/sheet2.xml", z.namelist())
            detail = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            formula = detail.find(".//x:c[@r='E2']/x:f", ns).text
            value = detail.find(".//x:c[@r='E2']/x:v", ns).text
            player = detail.find(".//x:c[@r='B2']", ns)
            self.assertEqual(formula, "SUM(C2:D2)")
            self.assertEqual(value, "1.35")
            self.assertEqual(player.get("t"), "inlineStr")
            self.assertIsNone(player.find("x:f", ns))
        exported_csv = csv_bytes(state, state["matches"]).decode("utf-8-sig")
        self.assertIn("'=HYPERLINK(test)", exported_csv)

    def test_horizontal_export_chronology_and_nonparticipants(self):
        fourth = self.store.create_player({"name": "第四人"})
        self.store.save_match(fixture())
        older = fixture(("-5", "10", "-5"), ("0", "1", "0"), key="older-match")
        older["played_at"] = "2026-10-08T19:30"
        older["entries"][2]["player_id"] = fourth
        self.store.save_match(older)
        state = self.store.state()
        headers, records = export_table(state, list(reversed(state["matches"])))
        self.assertEqual(headers, ["排名", "选手", "第1场基础分", "第1场额外分",
                                   "第2场基础分", "第2场额外分", "总分"])
        by_name = {record[1]: record for record in records}
        self.assertEqual(by_name["选手 A"][2:], [-5, 0, 30, 5, 30])
        self.assertEqual(by_name["选手 C"][2:], [-20, 2, None, None, -18])
        self.assertEqual(by_name["第四人"][2:], [-5, 0, None, None, -5])
        self.assertEqual([record[0] for record in records], [1, 2, 3, 4])
        parsed = list(csv.reader(io.StringIO(csv_bytes(state, state["matches"]).decode("utf-8-sig"))))
        self.assertEqual(parsed[0], headers)
        self.assertEqual(len(parsed), 5)
        c_record = next(record for record in parsed if record[1] == "选手 C")
        self.assertEqual(c_record[2:6], ["-20", "2", "", ""])

    def test_wide_excel_export_beyond_column_z(self):
        for i in range(13):
            self.store.save_match(fixture(key=f"wide-match-{i:02d}"))
        state = self.store.state()
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(io.BytesIO(excel_bytes(state, state["matches"]))) as z:
            detail = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(detail.find("x:dimension", ns).get("ref"), "A1:AC4")
            self.assertEqual(detail.find("x:autoFilter", ns).get("ref"), "A1:AC4")
            self.assertEqual(detail.find(".//x:c[@r='AC2']/x:f", ns).text, "SUM(C2:AB2)")
            self.assertEqual(detail.find(".//x:c[@r='AC2']/x:v", ns).text, "455")
            self.assertEqual(detail.find(".//x:pane", ns).get("topLeftCell"), "C2")
            self.assertEqual(detail.find(".//x:c[@r='AA1']/x:is/x:t", ns).text, "第13场基础分")

    def test_export_without_matches_has_only_rank_name_total(self):
        state = self.store.state()
        headers, records = export_table(state, [])
        self.assertEqual(headers, ["排名", "选手", "总分"])
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record[0] == "-" and record[-1] == 0 for record in records))
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(io.BytesIO(excel_bytes(state, []))) as z:
            detail = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(detail.find(".//x:c[@r='C2']/x:f", ns).text, "0")

    def test_export_uses_each_players_own_first_match(self):
        extra_ids = [self.store.create_player({"name": name}) for name in ("D", "E", "F")]
        self.store.save_match(fixture())
        other = fixture(("-5", "10", "-5"), ("0", "0", "0"), key="disjoint-match")
        for entry, player_id in zip(other["entries"], extra_ids):
            entry["player_id"] = player_id
        self.store.save_match(other)
        state = self.store.state()
        headers, records = export_table(state, state["matches"])
        self.assertEqual(len(state["matches"]), 2)
        self.assertEqual(headers, ["排名", "选手", "第1场基础分", "第1场额外分", "总分"])
        self.assertTrue(all(value is not None for record in records for value in record))
        by_name = {record[1]: record for record in records}
        self.assertEqual(by_name["D"][2:], [-5, 0, -5])
        self.assertEqual(by_name["选手 A"][2:], [30, 5, 35])

    def test_excluded_player_keeps_scores_and_other_ranks_close_gap(self):
        self.store.save_match(fixture())
        state = self.store.state()
        original = self.store.state()
        headers, records = export_table(state, state["matches"], excluded_ids={1})
        self.assertEqual([(record[0], record[1]) for record in records],
                         [(1, "选手 B"), (2, "选手 C"), ("-", "选手 A")])
        self.assertEqual(records[-1][2:], [30, 5, 35])
        self.assertEqual(self.store.state(), original)
        parsed = list(csv.reader(io.StringIO(csv_bytes(state, state["matches"], {1}).decode("utf-8-sig"))))
        self.assertEqual(parsed[-1], ["-", "选手 A", "30", "5", "35"])
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with zipfile.ZipFile(io.BytesIO(excel_bytes(state, state["matches"], {1}))) as z:
            detail = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(detail.find(".//x:c[@r='A4']/x:is/x:t", ns).text, "-")
            self.assertEqual(detail.find(".//x:c[@r='E4']/x:v", ns).text, "35")

    def test_excluding_tied_player_recomputes_ranks(self):
        self.store.save_match(fixture(("10", "10", "-20"), ("0", "0", "0")))
        state = self.store.state()
        _, records = export_table(state, state["matches"], {2})
        self.assertEqual([(row[0], row[1]) for row in records],
                         [(1, "选手 A"), (2, "选手 C"), ("-", "选手 B")])
        _, records = export_table(state, state["matches"], {1, 2, 3})
        self.assertTrue(all(row[0] == "-" for row in records))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = AppServer(("127.0.0.1", 0), ScoreStore(Path(self.temp.name) / "scores.sqlite3"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, token=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-App-Token"] = self.server.app_token
        if origin:
            headers["Origin"] = origin
        req = urllib.request.Request(self.base+path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def test_backend_validates_even_without_frontend(self):
        status, body, _ = self.request("/api/matches", "POST", fixture(("1", "2", "3")))
        self.assertEqual(status, 400)
        self.assertIn("必须为 0", json.loads(body)["error"])
        self.assertFalse(self.server.store.state()["matches"])

    def test_chart_export_can_load_temporary_blob_images(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="chart-export-button"', body)
        policy = {parts[0]: parts[1:] for directive in headers["Content-Security-Policy"].split(";")
                  if (parts := directive.split())}
        self.assertIn("blob:", policy["img-src"])
        self.assertIn("data:", policy["img-src"])
        self.assertEqual(policy["script-src"], ["'self'"])
        self.assertEqual(policy["connect-src"], ["'self'"])

    def test_token_and_origin_required_for_mutation(self):
        self.assertEqual(self.request("/api/matches", "POST", fixture(), token=False)[0], 403)
        self.assertEqual(self.request("/api/matches", "POST", fixture(), origin="http://other.test")[0], 403)
        self.assertEqual(self.request("/api/matches", "POST", fixture())[0], 201)

    def test_http_crud_export_and_filter(self):
        _, body, _ = self.request("/api/matches", "POST", fixture())
        match_id = json.loads(body)["id"]
        update = fixture(("5", "-2", "-3")); update["revision"] = 1
        self.assertEqual(self.request(f"/api/matches/{match_id}", "PUT", update)[0], 200)
        status, content, headers = self.request("/api/export.xlsx?start=2026-10-09&end=2026-10-09")
        self.assertEqual(status, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(content)))
        self.assertIn(".xlsx", headers["Content-Disposition"])
        self.assertEqual(self.request("/api/export.csv?start=2026-10-10&end=2026-10-01")[0], 400)
        self.assertEqual(self.request(f"/api/matches/{match_id}", "DELETE", {"revision": 2})[0], 200)
        self.assertEqual(json.loads(self.request("/api/state")[1])["matches"], [])

    def test_export_rank_selection_through_http(self):
        self.request("/api/matches", "POST", fixture())
        status, body, _ = self.request("/api/export.csv?exclude=1&exclude=3")
        self.assertEqual(status, 200)
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[1][:2], ["1", "选手 B"])
        self.assertEqual(rows[2][:2], ["-", "选手 A"])
        self.assertEqual(rows[3][:2], ["-", "选手 C"])
        self.assertEqual(self.request("/api/export.xlsx?exclude=99")[0], 400)
        self.assertEqual(self.request("/api/export.csv?exclude=not-a-number")[0], 400)


if __name__ == "__main__":
    unittest.main()
