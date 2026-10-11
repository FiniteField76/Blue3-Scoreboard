"""Multi-player base-score entry, atomic table loading and database upgrades."""
import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import tables
from app import AppError, AppServer, DETAIL_HEADERS, ScoreStore, detail_csv_bytes, excel_bytes, export_table, filter_matches, technical_stats
from test_competitions import StoreCase, upload, semantics
import test_competitions


def multi_record(ids=(1,2,3,4,5), bases=("35.55","-10.25","0","8","-33.3"), rate="1.25", **extra):
    return {"competition_id":1,"played_at":"2026-10-11T14:30","request_key":"multi-test-round",
            "scoring_mode":"multi","rate":rate,"entries":[{"player_id":pid,"base":base} for pid,base in zip(ids,bases)], **extra}


class MultiTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store.create_player({"name":"选手 D"})
        self.store.create_player({"name":"选手 E"})

    def test_more_than_three_scores_with_zero_sum_and_exact_rate(self):
        self.store.save_match(multi_record())
        match = self.store.state()["matches"][0]
        self.assertEqual(len(match["entries"]),5)
        self.assertEqual([e["total_units"] for e in match["entries"]],[444375,-128125,0,100000,-416250])
        self.assertEqual([e["seat"] for e in match["entries"]],list(range(5)))
        self.assertTrue(all(e["extra_cents"]==0 and e["points"] is None and e["chip"] is None for e in match["entries"]))
        self.assertEqual(sum(e["base_cents"] for e in match["entries"]),0)
        self.assertEqual(sum(e["total_units"] for e in match["entries"]),0)
        self.assertEqual(ScoreStore(self.path).state(),self.store.state())

    def test_multi_ignores_chip_points_extra_and_awards(self):
        record = multi_record()
        for entry in record["entries"]:
            entry.update(chip="invalid",points="invalid",extra="999999")
        record["knockouts"] = {"1":2}
        self.store.save_match(record)
        match = self.store.state()["matches"][0]
        self.assertEqual([e["total_units"] for e in match["entries"]],[444375,-128125,0,100000,-416250])
        self.assertEqual(match["knockouts"],{})

    def test_new_names_are_created_atomically_and_existing_names_reused(self):
        record = multi_record(entries=[{"player_name":"选手 a","base":"10"},{"player_name":"新选手","base":"-10"}],rate="5")
        mid = self.store.save_match(record)
        self.assertEqual(self.store.save_match(record),mid)
        state = self.store.state()
        self.assertEqual(len(state["players"]),6)
        self.assertEqual([e["total_units"] for e in state["matches"][0]["entries"]],[500000,-500000])
        self.assertEqual(state["matches"][0]["entries"][0]["player_id"],1)
        before = state
        bad = multi_record(request_key="multi-invalid-entry",entries=[{"player_name":"不应创建","base":"10"},{"player_id":2,"base":"bad"}])
        with self.assertRaises(AppError): self.store.save_match(bad)
        self.assertEqual(self.store.state(),before)

    def test_invalid_duplicate_empty_or_out_of_range_entries_never_write(self):
        for record in (multi_record(ids=(1,1),bases=("10","-10")), multi_record(entries=[]),
                       multi_record(rate="0"),multi_record(bases=("1000000","0","0","0","0"),rate="5"),
                       multi_record(entries=[{"player_name":"same","base":"0"},{"player_name":"SAME","base":"1"}])):
            before = self.store.state()
            with self.subTest(record=record),self.assertRaises(AppError): self.store.save_match(record)
            self.assertEqual(self.store.state(),before)
        ordinary = multi_record(scoring_mode="manual")
        with self.assertRaisesRegex(AppError,"三位"): self.store.save_match(ordinary)
        ordinary["scoring_mode"]="points"
        with self.assertRaisesRegex(AppError,"三位"): self.store.save_match(ordinary)

    def test_nonzero_base_sum_rejects_save_edit_and_new_players_atomically(self):
        before = self.store.state()
        bad = multi_record(entries=[{"player_id":1,"base":"0.1"},{"player_id":2,"base":"0.2"},
                                   {"player_name":"不能创建","base":"-0.29"}])
        with self.assertRaisesRegex(AppError,"基础分合计必须为 0.*0.01"): self.store.save_match(bad)
        self.assertEqual(self.store.state(),before)
        mid = self.store.save_match(multi_record())
        before = self.store.state()
        bad["revision"] = 1
        with self.assertRaisesRegex(AppError,"基础分合计必须为 0"): self.store.save_match(bad,mid)
        self.assertEqual(self.store.state(),before)

    def test_edit_changes_roster_and_competition_without_duplicates(self):
        mid = self.store.save_match(multi_record())
        cid = self.store.create_competition({"name":"新比赛"})
        update = multi_record(ids=(1,4),bases=("10","-10"),rate="2",competition_id=cid,revision=1)
        self.store.save_match(update,mid)
        state = self.store.state()
        self.assertEqual(len(state["matches"]),1)
        self.assertEqual([e["total_units"] for e in state["matches"][0]["entries"]],[200000,-200000])
        self.assertEqual(state["matches"][0]["revision"],2)
        self.assertEqual(filter_matches(state,{"competition":["1"]}),[])
        self.assertEqual(len(filter_matches(state,{"competition":[str(cid)]})),1)
        with self.assertRaisesRegex(AppError,"修改"): self.store.save_match(update,mid)

    def test_stats_use_all_multi_entries_and_tied_scores(self):
        self.store.save_match(multi_record(bases=("20","10","10","-40","0"),rate="5"))
        state = self.store.state()
        stats = technical_stats(state,state["matches"],2)
        self.assertEqual({r["id"]:r["average_rank"] for r in stats["players"]},{1:1,2:2,3:2,4:5,5:4})
        self.assertEqual({r["id"]:r["matches"] for r in stats["players"]},{i:1 for i in range(1,6)})
        opponent = next(r for r in stats["opponents"] if r["id"]==3)
        self.assertEqual((opponent["wins"],opponent["draws"],opponent["losses"]),(0,1,0))
        self.assertEqual(next(r for r in stats["players"] if r["id"]==2)["average_score_units"],500000)
        self.assertEqual(len(export_table(state,state["matches"])[1]),5)

    def test_two_hundred_players_fit_a_single_round(self):
        entries = [{"player_id":pid,"base":"1"} for pid in range(1,6)]
        entries += [{"player_name":f"新选手 {i}","base":"0.01"} for i in range(195)]
        entries[-1]["base"] = "-6.94"
        self.store.save_match(multi_record(entries=entries,rate="0.25"))
        state = self.store.state()
        self.assertEqual(len(state["players"]),200)
        self.assertEqual(len(state["matches"][0]["entries"]),200)
        self.assertEqual(state["matches"][0]["entries"][-1]["total_units"],-17350)
        self.assertEqual(len(tables.read_import_table(detail_csv_bytes(state,state["matches"]),'multi.csv')),200)
        with self.assertRaises(AppError): self.store.save_match(multi_record(entries=entries+[entries[0]]))
        self.assertEqual(self.store.state(),state)

    def test_excel_and_csv_roundtrip_preserve_multi_roster(self):
        self.store.save_match(multi_record())
        state = self.store.state()
        for raw,filename in ((excel_bytes(state,state["matches"]),"multi.xlsx"),(detail_csv_bytes(state,state["matches"]),"multi.csv")):
            with self.subTest(filename=filename):
                destination = ScoreStore(Path(self.temp.name)/filename.replace('.','-'))
                payload = upload(raw,filename)
                preview = destination.import_rounds(payload)
                self.assertEqual(preview["created"],1)
                self.assertEqual(len(preview["rounds"][0]["players"]),5)
                destination.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)
                self.assertEqual(semantics(destination.state()),semantics(state))
                self.assertEqual(destination.import_rounds(payload)["skipped"],1)

    def test_score_table_preview_supports_paste_csv_and_xlsx_without_writing(self):
        before = self.store.state()
        pasted = "选手 A\t35.55\n选手 B\t-10.25\n选手 C\t0\n新 D\t8\n新 E\t-33.3"
        draft = self.store.preview_multi_scores({"text":pasted})
        self.assertEqual(len(draft["entries"]),5)
        self.assertEqual(draft["new_players"],["新 D","新 E"])
        self.assertEqual(draft["entries"][0]["base"],"35.55")
        rows = [[e["player_name"],Decimal(e["base"])] for e in draft["entries"]]
        raw = tables.csv_table(["选手","基础分"],rows)
        self.assertEqual(self.store.preview_multi_scores(upload(raw,"scores.csv")),draft)
        raw = tables.append_sheets(excel_bytes(before,[],include_details=False),[("成绩",tables.sheet_xml(["选手","基础分"],rows))],replace=True)
        self.assertEqual(self.store.preview_multi_scores(upload(raw,"scores.xlsx")),draft)
        self.assertEqual(self.store.state(),before)
        self.store.save_match(multi_record(entries=draft["entries"]))
        self.assertEqual(len(self.store.state()["players"]),7)

    def test_nonzero_score_tables_reject_paste_csv_xlsx_and_detail_import(self):
        before = self.store.state()
        rows = [["选手 A",Decimal("0.1")],["新 B",Decimal("0.2")],["新 C",Decimal("-0.29")]]
        csv = tables.csv_table(["选手","基础分"],rows)
        xlsx = tables.append_sheets(excel_bytes(before,[],include_details=False),[
            ("成绩",tables.sheet_xml(["选手","基础分"],rows))],replace=True)
        for payload in ({"text":"选手 A\t0.1\n新 B\t0.2\n新 C\t-0.29"},upload(csv,"scores.csv"),upload(xlsx,"scores.xlsx")):
            with self.subTest(payload=payload),self.assertRaisesRegex(AppError,"基础分合计必须为 0"):
                self.store.preview_multi_scores(payload)
            self.assertEqual(self.store.state(),before)
        self.store.save_match(multi_record())
        state = self.store.state()
        detail = tables.read_import_table(detail_csv_bytes(state,state["matches"]),"rounds.csv")
        detail[-1]["原始基础分"] = "-33.29"
        raw = tables.csv_table(DETAIL_HEADERS,[[row.get(key,"") for key in DETAIL_HEADERS] for row in detail])
        destination = ScoreStore(Path(self.temp.name)/"nonzero-detail.sqlite3")
        unchanged = destination.state()
        with self.assertRaisesRegex(AppError,"基础分.*0"):
            destination.import_rounds(upload(raw,"rounds.csv"))
        self.assertEqual(destination.state(),unchanged)

    def test_score_table_rejects_duplicate_names_bad_bases_and_missing_columns(self):
        for payload in ({"text":"A\t10\nA\t20"},{"text":"A\tNaN"},
                        upload(b'name,score\nA,10','bad.csv'), {"text":"A"}):
            before = self.store.state()
            with self.subTest(payload=payload),self.assertRaises(AppError): self.store.preview_multi_scores(payload)
            self.assertEqual(self.store.state(),before)

    def test_global_rate_does_not_rewrite_saved_multi_rounds(self):
        record = multi_record()
        record.pop("rate")
        self.store.save_match(record)
        before = self.store.state()["matches"]
        self.assertEqual(before[0]["rate"],"1")
        self.store.update_settings({"rate":"2"})
        self.assertEqual(self.store.state()["matches"],before)

    def test_delete_player_removes_the_complete_multi_round(self):
        mid = self.store.save_match(multi_record())
        result = self.store.delete_players({"ids":[4],"matches":[{"id":mid,"revision":1}]})
        self.assertEqual(result,{"players":1,"matches":1})
        self.assertFalse(self.store.state()["matches"])
        with self.store.connection() as db:
            self.assertFalse(db.execute("SELECT * FROM entries").fetchall())
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())

    def test_old_three_seat_database_migrates_and_preserves_scores(self):
        path = Path(self.temp.name)/"old-seats.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE players(id INTEGER PRIMARY KEY, name TEXT UNIQUE, active INTEGER);
                CREATE TABLE matches(id INTEGER PRIMARY KEY, played_at TEXT, note TEXT, revision INTEGER,
                    request_key TEXT UNIQUE, created_at TEXT, updated_at TEXT);
                CREATE TABLE entries(match_id INTEGER, player_id INTEGER, seat INTEGER CHECK(seat BETWEEN 0 AND 2),
                    base_cents INTEGER, extra_cents INTEGER, PRIMARY KEY(match_id,player_id), UNIQUE(match_id,seat));
                INSERT INTO players VALUES(1,'A',1),(2,'B',1),(3,'C',1);
                INSERT INTO matches VALUES(7,'2026-10-09T19:30','历史',3,'old-multi-upgrade','old','old');
                INSERT INTO entries VALUES(7,1,0,3000,500),(7,2,1,-1000,0),(7,3,2,-2000,200);
            """)
        store = ScoreStore(path)
        old = store.state()["matches"][0]
        self.assertEqual([e["total_units"] for e in old["entries"]],[350000,-100000,-180000])
        store.create_player({"name":"D"}); store.create_player({"name":"E"})
        store.save_match(multi_record())
        self.assertEqual(next(m for m in store.state()["matches"] if m["id"]==7),old)
        self.assertEqual(len(store.state()["matches"][0]["entries"]),5)
        self.assertEqual(ScoreStore(path).state(),store.state())


class MultiHttpTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.server = AppServer(('127.0.0.1',0),self.store)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        super().tearDown()

    request = test_competitions.NewHttpTests.request

    def test_load_save_stats_and_edit_multi_round_through_http(self):
        pasted = "选手 A\t10\n选手 B\t-5\n选手 C\t8.5\n新 D\t0\n新 E\t-13.5"
        status,body = self.request('/api/multi-scores/preview','POST',{"text":pasted})
        self.assertEqual(status,200)
        draft = json.loads(body)["draft"]
        self.assertFalse(self.store.state()["matches"])
        status,body = self.request('/api/matches','POST',multi_record(entries=draft["entries"]))
        self.assertEqual(status,201)
        mid = json.loads(body)["id"]
        state = self.store.state()
        self.assertEqual([e["total_units"] for e in state["matches"][0]["entries"]],[125000,-62500,106250,0,-168750])
        self.assertEqual(len(technical_stats(state,state["matches"])["players"]),5)
        payload = multi_record(entries=[{"player_id":e["player_id"],"base":str(base)} for e,base in zip(state["matches"][0]["entries"],(0,1,2,3,-6))],revision=1,rate="2")
        self.assertEqual(self.request(f'/api/matches/{mid}','PUT',payload)[0],200)
        self.assertEqual([e["total_units"] for e in self.store.state()["matches"][0]["entries"]],[0,20000,40000,60000,-120000])

    def test_http_rejects_nonzero_preview_create_and_edit(self):
        before = self.store.state()
        status,body = self.request('/api/multi-scores/preview','POST',{"text":"选手 A\t0.1\n选手 B\t0.2\n选手 C\t-0.29"})
        self.assertEqual(status,400)
        self.assertIn("基础分合计必须为 0",json.loads(body)["error"])
        bad = multi_record(ids=(1,2,3),bases=("0.1","0.2","-0.29"))
        self.assertEqual(self.request('/api/matches','POST',bad)[0],400)
        self.assertEqual(self.store.state(),before)
        mid = self.store.save_match(multi_record(ids=(1,2,3),bases=("0.1","0.2","-0.3")))
        before = self.store.state()
        self.assertEqual(self.request(f'/api/matches/{mid}','PUT',{**bad,"revision":1})[0],400)
        self.assertEqual(self.store.state(),before)
