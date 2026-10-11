"""Battle counts, aggregate histories and lossless count imports."""
import copy
import csv
import io
import sqlite3
import unittest
import random
from pathlib import Path

import tables
from app import (AppError, DETAIL_HEADERS, ScoreStore, detail_csv_bytes, excel_bytes,
                 export_table, leaderboard, technical_excel_bytes, technical_stats, score_flows, SCORE_SCALE)
from test_competitions import StoreCase, manual, points, upload
from test_multi import multi_record


class CountTests(StoreCase):
    def test_defaults_and_counts_change_averages_without_multiplying_scores(self):
        self.store.save_match(manual())
        state = self.store.state()
        self.assertEqual([e["battle_count"] for e in state["matches"][0]["entries"]], [1,1,1])
        record = points(key="nine-game-report", rate="1")
        for entry in record["entries"]: entry["battle_count"] = 9
        self.store.save_match(record)
        state = self.store.state()
        p = next(r for r in technical_stats(state,state["matches"],1)["players"] if r["id"]==1)
        self.assertEqual(p["matches"],10)
        self.assertEqual(p["total_units"],710000)
        self.assertEqual(p["average_score_units"],71000)
        self.assertEqual(p["ranked_matches"],1)
        self.assertEqual(p["average_rank"],1)
        opponent = next(r for r in technical_stats(state,state["matches"],1)["opponents"] if r["id"]==2)
        self.assertEqual(opponent["matches"],10)
        self.assertEqual((opponent["wins"],opponent["draws"],opponent["losses"]),(2,0,0))
        self.assertEqual(opponent["win_rate"],1)
        row = next(r for r in leaderboard(state,state["matches"]) if r["id"]==1)
        self.assertEqual(row["matches"],10)
        self.assertEqual(row["wins"],1)
        self.assertEqual(ScoreStore(self.path).state(),state)

    def test_season_counts_are_separate_and_future_rounds_add_to_them(self):
        self.store.save_match(multi_record(ids=(1,2,3),bases=("20","-10","-10"),rate="1",
                              entries=[{"player_id":i,"base":b,"battle_count":0} for i,b in [(1,"20"),(2,"-10"),(3,"-10")]]))
        report = multi_record(ids=(1,2,3),bases=("0","0","0"),rate="1",request_key="count-report-2024",record_kind="counts")
        for entry,count in zip(report["entries"],[63,126,36]): entry["battle_count"] = count
        report_id = self.store.save_match(report)
        state = self.store.state()
        summary = next(m for m in state["matches"] if m["id"]==report_id)
        self.assertTrue(all(e["finish_rank"] is None and e["total_units"]==0 for e in summary["entries"]))
        stats = technical_stats(state,state["matches"],1)
        p = next(r for r in stats["players"] if r["id"]==1)
        self.assertEqual((p["matches"],p["total_units"],p["ranked_matches"]),(63,200000,0))
        self.assertIsNone(p["average_rank"])
        self.assertTrue(all(r["matches"]==0 for r in stats["opponents"]))
        self.assertEqual({r["id"] for r in stats["opponents"]},{2,3})
        self.assertTrue(all(r["wins"]==1 and r["draws"]==0 and r["win_rate"]==1 for r in stats["opponents"]))
        headers,rows = export_table(state,state["matches"])
        self.assertEqual(headers,["排名","选手","第1场基础分","第1场额外分","总分"])
        self.assertEqual(next(r[-1] for r in rows if r[1]=="选手 A"),20)
        self.store.save_match(manual(key="later-round-2024"))
        state = self.store.state()
        p = next(r for r in technical_stats(state,state["matches"])["players"] if r["id"]==1)
        self.assertEqual((p["matches"],p["total_units"],p["ranked_matches"]),(64,500000,1))
        current = next(m for m in state["matches"] if m["id"]==report_id)
        report["revision"] = current["revision"]
        report["entries"][0]["battle_count"] = 70
        self.store.save_match(report,report_id)
        p = next(r for r in technical_stats(self.store.state(),self.store.state()["matches"])["players"] if r["id"]==1)
        self.assertEqual(p["matches"],71)

    def test_six_games_compare_each_opponent_once_including_ties(self):
        self.store.create_player({"name":"未交手"})
        record = multi_record(ids=(1,2,3),bases=("100","-50","-50"),rate="1")
        for entry in record["entries"]: entry["battle_count"] = 6
        self.store.save_match(record)
        state = self.store.state()
        a = technical_stats(state,state["matches"],1)["opponents"]
        self.assertEqual({r["id"] for r in a},{2,3})
        self.assertTrue(all((r["matches"],r["wins"],r["draws"],r["losses"],r["win_rate"]) == (6,1,0,0,1) for r in a))
        self.assertTrue(all(r["sessions"]==1 and r["difference_units"]==500000 for r in a))
        b = {r["id"]:r for r in technical_stats(state,state["matches"],2)["opponents"]}
        self.assertEqual((b[1]["matches"],b[1]["wins"],b[1]["draws"],b[1]["losses"]),(6,0,0,1))
        self.assertEqual((b[3]["matches"],b[3]["wins"],b[3]["draws"],b[3]["losses"]),(6,0,1,0))
        self.assertTrue(all("average_difference_units" not in r for r in a))

    def test_shared_counts_are_symmetric_and_zero_count_scores_still_have_results(self):
        record = multi_record(ids=(1,2,3),bases=("100","-50","-50"),rate="1")
        for entry,count in zip(record["entries"],(9,6,0)): entry["battle_count"] = count
        self.store.save_match(record)
        state = self.store.state()
        for player,other,count in ((1,2,6),(2,1,6),(1,3,0),(3,1,0)):
            with self.subTest(player=player,other=other):
                opponent = next(r for r in technical_stats(state,state["matches"],player)["opponents"] if r["id"]==other)
                self.assertEqual(opponent["matches"],count)
                self.assertEqual(opponent["wins"]+opponent["draws"]+opponent["losses"],1)

    def test_aggregate_point_ties_ignore_chip_when_comparing_opponents(self):
        record = points(values=(500,500,50),chips=(-100,100,0))
        for entry in record["entries"]: entry["battle_count"] = 6
        self.store.save_match(record)
        state = self.store.state()
        opponents = {r["id"]:r for r in technical_stats(state,state["matches"],1)["opponents"]}
        self.assertEqual((opponents[2]["matches"],opponents[2]["wins"],opponents[2]["draws"],opponents[2]["losses"]),(6,0,1,0))
        self.assertEqual((opponents[3]["matches"],opponents[3]["wins"],opponents[3]["draws"],opponents[3]["losses"]),(6,1,0,0))
        self.assertLess(opponents[2]["difference_units"],0)
        self.assertEqual(opponents[3]["difference_units"],0)

    def test_invalid_counts_and_nonzero_count_reports_are_atomic(self):
        for count in (-1,"1.5","NaN",True,None,1000001):
            record = multi_record(ids=(1,),bases=("0",), entries=[{"player_name":"不应创建","base":"0","battle_count":count}])
            before = self.store.state()
            with self.subTest(count=count),self.assertRaisesRegex(AppError,"对战数量"):
                self.store.save_match(record)
            self.assertEqual(self.store.state(),before)
        for record in (multi_record(ids=(1,2),bases=("20","-20"),record_kind="counts"),
                       {**manual(),"record_kind":"counts"},
                       {**manual(),"record_kind":"invalid"}):
            before = self.store.state()
            with self.assertRaises(AppError): self.store.save_match(record)
            self.assertEqual(self.store.state(),before)

    def test_idempotent_retry_checks_counts_and_record_kind(self):
        record = multi_record(ids=(1,2,3),bases=("0","0","0"))
        mid = self.store.save_match(record)
        self.assertEqual(self.store.save_match(record),mid)
        before = self.store.state()
        record["entries"][0]["battle_count"] = 2
        with self.assertRaises(AppError): self.store.save_match(record)
        self.assertEqual(self.store.state(),before)
        record["entries"][0]["battle_count"] = 1
        record["record_kind"] = "counts"
        with self.assertRaises(AppError): self.store.save_match(record)
        self.assertEqual(self.store.state(),before)

    def test_paste_csv_and_xlsx_counts_and_two_column_compatibility(self):
        draft = self.store.preview_multi_scores({"text":"选手\t基础分\t对战数量\n选手 A\t20\t9\n选手 B\t-20\t12"})
        self.assertEqual([e["battle_count"] for e in draft["entries"]],[9,12])
        self.assertEqual(self.store.preview_multi_scores({"text":"选手 A\t20\t9\n选手 B\t-20\t12"}),draft)
        rows = [["选手 A",20,9],["选手 B",-20,12]]
        for raw,name in ((tables.csv_table(["选手","基础分","对战数量"],rows),"counts.csv"),
                         (tables.append_sheets(excel_bytes(self.store.state(),[],include_details=False),[("成绩",tables.sheet_xml(["选手","基础分","对战数量"],rows))],replace=True),"counts.xlsx")):
            self.assertEqual(self.store.preview_multi_scores(upload(raw,name)),draft)
        two = self.store.preview_multi_scores({"text":"选手 A\t20\n选手 B\t-20"})
        self.assertEqual([e["battle_count"] for e in two["entries"]],[1,1])
        for value in ("-1","1.1","1000001","bad"):
            with self.assertRaises(AppError): self.store.preview_multi_scores({"text":f"选手 A\t20\t{value}\n选手 B\t-20\t1"})

    def test_detail_csv_excel_roundtrip_and_old_export_defaults(self):
        score = multi_record(ids=(1,2,3),bases=("20","-10","-10"),rate="1")
        for entry in score["entries"]: entry["battle_count"] = 0
        self.store.save_match(score)
        counts = multi_record(ids=(1,2,3),bases=("0","0","0"),rate="1",request_key="season-summary-import",record_kind="counts")
        for entry,value in zip(counts["entries"],[10,20,30]): entry["battle_count"] = value
        self.store.save_match(counts)
        state = self.store.state()
        for raw,name in ((detail_csv_bytes(state,state["matches"]),"counts.csv"),(excel_bytes(state,state["matches"]),"counts.xlsx")):
            target = ScoreStore(Path(self.temp.name)/f"target-{name}")
            payload = upload(raw,name)
            preview = target.import_rounds(payload)
            target.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)
            restored = target.state()
            self.assertEqual([(m["record_kind"],[e["battle_count"] for e in m["entries"]]) for m in restored["matches"]],
                             [(m["record_kind"],[e["battle_count"] for e in m["entries"]]) for m in state["matches"]])
            self.assertEqual(technical_stats(restored,restored["matches"]),technical_stats(state,state["matches"]))
        reader = list(csv.reader(io.StringIO(detail_csv_bytes(state,[next(m for m in state["matches"] if m["record_kind"]=="score")]).decode("utf-8-sig"))))
        old = tables.csv_table(reader[0][:-2],[r[:-2] for r in reader[1:]])
        target = ScoreStore(Path(self.temp.name)/"old-export.sqlite3")
        payload = upload(old,"old.csv");preview = target.import_rounds(payload)
        target.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)
        self.assertEqual([e["battle_count"] for e in target.state()["matches"][0]["entries"]],[1,1,1])

    def test_legacy_database_migrates_defaults_without_rewriting_scores(self):
        self.store.save_match(manual())
        before = self.store.state()
        with self.store.connection(write=True) as db:
            db.execute("ALTER TABLE entries DROP COLUMN battle_count")
            db.execute("ALTER TABLE matches DROP COLUMN record_kind")
            db.execute("UPDATE metadata SET value='5' WHERE key='rate'")
        upgraded = ScoreStore(self.path).state()
        self.assertEqual(upgraded["settings"]["rate"],"5")
        self.assertEqual([e["battle_count"] for e in upgraded["matches"][0]["entries"]],[1,1,1])
        self.assertEqual([e["total_units"] for e in upgraded["matches"][0]["entries"]],
                         [e["total_units"] for e in before["matches"][0]["entries"]])
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertFalse(db.execute("PRAGMA foreign_key_check").fetchall())


class FlowTests(unittest.TestCase):
    @staticmethod
    def match(scores, scale=SCORE_SCALE):
        return {"entries":[{"player_id":i+1,"total_units":value*scale} for i,value in enumerate(scores)]}

    def test_three_player_example_and_two_winner_proportions(self):
        flows = score_flows(self.match([100,-50,-50]))
        self.assertEqual(flows,{(1,2):500000,(2,1):-500000,(1,3):500000,(3,1):-500000})
        flows = score_flows(self.match([100,50,-30,-120]))
        self.assertEqual({pair:value for pair,value in flows.items() if value>0},
                         {(1,3):200000,(1,4):800000,(2,3):100000,(2,4):400000})

    def test_rounding_small_fractions_is_balanced_and_order_independent(self):
        match = self.match([2,1,-1,-1,-1])
        flows = score_flows(match)
        self.assertEqual([sum(v for (p,_),v in flows.items() if p==pid)//SCORE_SCALE for pid in range(1,6)],[2,1,-1,-1,-1])
        match["entries"].reverse()
        self.assertEqual(score_flows(match),flows)
        fractional = score_flows(self.match([1005000,-502500,-502500],scale=1))
        self.assertEqual(fractional,{(1,2):510000,(2,1):-510000,(1,3):500000,(3,1):-500000})

    def test_random_integer_records_preserve_every_players_total(self):
        rng = random.Random(130640)
        samples = []
        for _ in range(100):
            scores = [rng.randint(-10000,10000) for _ in range(rng.randint(2,20))]
            scores[-1] = -sum(scores[:-1]); samples.append(scores)
        samples.append([i for i in range(1,101)]+[-i for i in range(1,101)])
        for scores in samples:
            with self.subTest(players=len(scores)):
                flows = score_flows(self.match(scores))
                self.assertEqual(sum(flows.values()),0)
                for pid,value in enumerate(scores,1):
                    self.assertEqual(sum(v for (p,_),v in flows.items() if p==pid),value*SCORE_SCALE)
                for (a,b),value in flows.items():
                    self.assertEqual(flows[b,a],-value)
                    self.assertEqual(value%SCORE_SCALE,0)
                    if value>0:
                        ideal_numerator = scores[a-1]*-scores[b-1]
                        budget = sum(s for s in scores if s>0)
                        self.assertIn(value//SCORE_SCALE,{ideal_numerator//budget,(ideal_numerator+budget-1)//budget})

    def test_counts_zero_and_unmatched_extra_scores_do_not_invent_transfers(self):
        self.assertEqual(score_flows({**self.match([100,-50,-50]),"record_kind":"counts"}),{})
        self.assertEqual(score_flows(self.match([0,0,0])),{})
        self.assertEqual(score_flows(self.match([100,50,0])),{})
        self.assertEqual(score_flows(self.match([100,-60,-30])),
                         {(1,2):600000,(2,1):-600000,(1,3):300000,(3,1):-300000})

