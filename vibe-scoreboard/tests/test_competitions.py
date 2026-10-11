"""Competition scope, technical statistics and lossless import regression checks."""
import base64
import copy
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import tables
from app import (AppError, AppServer, ScoreStore, detail_csv_bytes, detail_table,
                 excel_bytes, export_table, filter_matches, import_template_bytes,
                 leaderboard, technical_excel_bytes, technical_stats)


def manual(key="manual-test", cid=1, bases=(30,-10,-20), ids=(1,2,3)):
    return {"competition_id":cid,"played_at":"2026-10-09T19:30","note":"备注", "request_key":key,
            "entries":[{"player_id":pid,"base":str(base),"extra":"0"} for pid,base in zip(ids,bases)]}


def points(key="points-test", cid=1, values=(500,400,150), chips=(2,-1,-1), rate="5", awards=None):
    return {"competition_id":cid,"played_at":"2026-10-10T20:30","note":"点数对局", "request_key":key,
            "scoring_mode":"points","rate":rate,"knockouts":awards or {},
            "entries":[{"player_id":i+1,"points":p,"chip":c,"extra":"0"} for i,(p,c) in enumerate(zip(values,chips))]}


def upload(raw, name="rounds.xlsx", **options):
    return {"filename":name,"content":base64.b64encode(raw).decode(),"mode":"skip",**options}


def semantics(state):
    names = {p["id"]:p["name"] for p in state["players"]}
    return sorted((m["public_key"], m["competition_name"], m["played_at"], m["note"], m["scoring_mode"], m["rate"],
                   tuple((e["name"],e["base_cents"],e["extra_cents"],e["points"],e["chip"],e["total_units"],e["finish_rank"])
                         for e in m["entries"]), tuple(names[pid] for pid in m["rank_order"]),
                   tuple(sorted((names[int(loser)],names[winner]) for loser,winner in m["knockouts"].items())))
                  for m in state["matches"])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"scores.sqlite3"
        self.store = ScoreStore(self.path)

    def tearDown(self):
        self.temp.cleanup()


class CompetitionTests(StoreCase):

    def test_named_competitions_and_intersected_scope(self):
        cid = self.store.create_competition({"name":"秋季联赛","note":"决赛"})
        a = self.store.save_match(manual())
        b = self.store.save_match(points(cid=cid))
        state = self.store.state()
        self.assertEqual([m["id"] for m in filter_matches(state,{"competition":[str(cid)]})],[b])
        self.assertEqual(len(filter_matches(state,{"competition":["1",str(cid)]})),2)
        self.assertEqual([m["id"] for m in filter_matches(state,{"competition":["1"],"end":["2026-10-09"]})],[a])
        self.assertEqual(filter_matches(state,{"competition":[str(cid)],"end":["2026-10-09"]}),[])
        self.assertEqual(filter_matches(state,{"none":["1"]}),[])
        with self.assertRaises(AppError): filter_matches(state,{"competition":["999"]})
        with self.assertRaises(AppError): self.store.create_competition({"name":"秋季联赛"})

    def test_move_and_delete_check_complete_revision_snapshot(self):
        cid = self.store.create_competition({"name":"联赛"})
        a = self.store.save_match(manual()); b = self.store.save_match(manual("second-game"))
        original = self.store.state()
        with self.assertRaises(AppError):
            self.store.move_matches({"competition_id":cid,"matches":[{"id":a,"revision":1},{"id":b,"revision":99}]})
        self.assertEqual(self.store.state(),original)
        self.store.move_matches({"competition_id":cid,"matches":[{"id":a,"revision":1}]})
        moved = next(m for m in self.store.state()["matches"] if m["id"]==a)
        self.assertEqual(moved["revision"],2)
        self.assertEqual(moved["entries"],next(m for m in original["matches"] if m["id"]==a)["entries"])
        with self.assertRaises(AppError): self.store.delete_competition(cid,{"revision":1,"matches":[]})
        self.store.delete_competition(cid,{"revision":1,"matches":[{"id":a,"revision":2}]})
        self.assertEqual([m["id"] for m in self.store.state()["matches"]],[b])
        self.assertEqual(len(self.store.state()["players"]),3)

    def test_rename_identity_and_no_reseeding_deleted_competitions(self):
        competition = self.store.state()["competitions"][0]
        self.store.update_competition(1,{"name":"春季杯","revision":1})
        renamed = self.store.state()["competitions"][0]
        self.assertEqual(renamed["public_key"],competition["public_key"])
        with self.assertRaises(AppError): self.store.update_competition(1,{"name":"夏季杯","revision":1})
        self.store.delete_competition(1,{"revision":2,"matches":[]})
        self.assertEqual(ScoreStore(self.path).state()["competitions"],[])
        with self.assertRaises(AppError): self.store.save_match(manual())

    def test_rank_based_wins_even_when_chip_reverses_final_scores(self):
        self.store.create_player({"name":"未交手"})
        record = points(chips=(-100,100,0))
        self.store.save_match(record)
        self.store.save_match(manual(bases=(0,0,0)))
        state = self.store.state(); stats = technical_stats(state,state["matches"],1)
        a = next(p for p in stats["players"] if p["id"]==1)
        b = next(p for p in stats["players"] if p["id"]==2)
        self.assertEqual(a["average_rank"],1)
        self.assertEqual(b["average_rank"],1.5)
        opponent = next(p for p in stats["opponents"] if p["id"]==2)
        self.assertEqual((opponent["wins"],opponent["draws"],opponent["losses"]),(1,1,0))
        self.assertEqual(opponent["win_rate"],0.5)
        self.assertLess(opponent["difference_units"],0)
        self.assertNotIn(4,{p["id"] for p in stats["opponents"]})
        with self.assertRaises(AppError): technical_stats(state,state["matches"],999)

    def test_averages_use_participation_and_exact_final_scores(self):
        extra = self.store.create_player({"name":"选手 D"})
        self.store.save_match(points(rate="1.25"))
        self.store.save_match(manual("second-game",bases=(10,-4,-6),ids=(1,2,extra)))
        state = self.store.state(); stats = technical_stats(state,state["matches"],extra)
        a = next(p for p in stats["players"] if p["id"]==1)
        c = next(p for p in stats["players"] if p["id"]==3)
        self.assertEqual(a["matches"],2); self.assertEqual(c["matches"],1)
        self.assertEqual(a["average_score_units"],306250)
        self.assertEqual(c["average_score_units"],-475000)
        self.assertNotIn(3,{p["id"] for p in stats["opponents"]})
        with zipfile.ZipFile(io.BytesIO(technical_excel_bytes(state,state["matches"],extra))) as z:
            self.assertEqual(len(ET.fromstring(z.read('xl/workbook.xml')).find(f'{{{tables.NS}}}sheets')),3)
            self.assertIn('0.00%',z.read('xl/styles.xml').decode())

    def test_point_ties_are_draws_even_when_chip_reverses_the_score(self):
        self.store.save_match(points(values=(500,500,50),chips=(-100,100,0)))
        state = self.store.state()
        stats = technical_stats(state,state["matches"],1)
        self.assertEqual({r["id"]:r["average_rank"] for r in stats["players"]},{1:1,2:1,3:3})
        opponent = next(r for r in stats["opponents"] if r["id"]==2)
        self.assertEqual((opponent["wins"],opponent["draws"],opponent["losses"]),(0,1,0))
        self.assertEqual(opponent["win_rate"],0)
        self.assertLess(opponent["difference_units"],0)
        self.assertEqual({r["id"]:r["wins"] for r in leaderboard(state,state["matches"])},{1:1,2:1,3:0})

    def test_bottom_tie_ranks_depend_on_points_not_calculation_order(self):
        record = points(values=(600,400,400),chips=(-100,100,0))
        record["rank_order"] = [1,3,2]
        self.store.save_match(record)
        state = self.store.state()
        stats = technical_stats(state,state["matches"],2)
        self.assertEqual({r["id"]:r["average_rank"] for r in stats["players"]},{1:1,2:2,3:2})
        opponent = next(r for r in stats["opponents"] if r["id"]==3)
        self.assertEqual((opponent["wins"],opponent["draws"],opponent["losses"]),(0,1,0))
        self.assertEqual({r["id"]:r["wins"] for r in leaderboard(state,state["matches"])},{1:1,2:0,3:0})


class ImportTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.other = ScoreStore(Path(self.temp.name)/"other.sqlite3")
        cid = self.store.create_competition({"name":"决赛","note":"比赛备注"})
        self.store.save_match(manual())
        self.store.save_match(points(cid=cid))
        self.store.save_match(points("knockout-game",cid=cid,values=(830,290,0),chips=(0,0,0),awards={"3":2}))
        tied = points("tied-game",cid=cid,values=(500,500,50),chips=(0,0,0),rate="1.25")
        tied["rank_order"] = [2,1,3]; tied["entries"][0]["extra"]="0.15"
        self.store.save_match(tied)

    def payload(self, **options):
        state = self.store.state()
        return upload(excel_bytes(state,state["matches"]),**options)

    def preview_commit(self, payload, store=None):
        store = store or self.other
        preview = store.import_rounds(payload)
        return store.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)

    def test_excel_roundtrip_and_preview_never_writes(self):
        initial = self.other.state(); payload = self.payload()
        preview = self.other.import_rounds(payload)
        self.assertEqual(preview["created"],4)
        self.assertEqual(self.other.state(),initial)
        self.assertEqual(preview["new_players"],[])
        self.preview_commit(payload)
        self.assertEqual(semantics(self.other.state()),semantics(self.store.state()))
        self.assertEqual(export_table(self.other.state(),self.other.state()["matches"]),export_table(self.store.state(),self.store.state()["matches"]))

    def test_tied_rank_export_preserves_the_bottom_tie_calculation(self):
        record = points("bottom-tie-export",values=(600,400,400),chips=(0,0,0))
        record["rank_order"] = [1,3,2]
        mid = self.store.save_match(record)
        state = self.store.state()
        headers,rows = detail_table(state,state["matches"])
        identity = next(m["public_key"] for m in state["matches"] if m["id"]==mid)
        tied = [row for row in rows if row[1]==identity]
        self.assertEqual([row[headers.index("名次")] for row in tied],[1,2,2])
        self.assertEqual([row[headers.index("计分顺序")] for row in tied],[1,3,2])
        self.preview_commit(self.payload())
        self.assertEqual(semantics(self.other.state()),semantics(state))
        destination = ScoreStore(Path(self.temp.name)/"csv-ties.sqlite3")
        self.preview_commit(upload(detail_csv_bytes(state,state["matches"]),"ties.csv"),store=destination)
        self.assertEqual(semantics(destination.state()),semantics(state))

    def test_previous_exports_without_calculation_order_still_import(self):
        state = self.store.state()
        headers,rows = detail_table(state,state["matches"])
        matches = {m["public_key"]:m for m in state["matches"]}
        for row in rows:
            match = matches[row[1]]
            if match["scoring_mode"]=="points":
                row[14] = row[headers.index("计分顺序")]
        kept = [i for i,h in enumerate(headers) if h not in ("计分顺序","对战数量","记录类型")]
        self.preview_commit(upload(tables.csv_table([headers[i] for i in kept],[[row[i] for i in kept] for row in rows]),"old-export.csv"))
        self.assertEqual(semantics(self.other.state()),semantics(state))

    def test_default_skip_copy_and_overwrite(self):
        payload = self.payload(); self.preview_commit(payload)
        old = self.other.state()
        skipped = self.preview_commit(payload)
        self.assertEqual(skipped["skipped"],4); self.assertEqual(self.other.state(),old)
        headers,rows = detail_table(self.store.state(),self.store.state()["matches"])
        rows[0][16] = 7
        edited = upload(tables.csv_table(headers,rows),"edited.csv",mode="overwrite")
        result = self.preview_commit(edited)
        self.assertEqual(result["updated"],4)
        self.assertEqual(next(e for m in self.other.state()["matches"] if m["public_key"]==rows[0][1] for e in m["entries"] if e["seat"]==0)["extra_cents"],700)
        copied = self.preview_commit({**payload,"mode":"copy"})
        self.assertEqual(copied["created"],4); self.assertEqual(len(self.other.state()["matches"]),8)
        self.assertEqual(len({m["public_key"] for m in self.other.state()["matches"]}),8)

    def test_preview_stale_after_player_edit_rejects_everything(self):
        payload = self.payload(); preview = self.other.import_rounds(payload)
        self.other.update_player(1,{"name":"改名"})
        before = self.other.state()
        with self.assertRaisesRegex(AppError,"重新预览"):
            self.other.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)
        self.assertEqual(self.other.state(),before)

    def test_failure_during_commit_rolls_back_players_groups_and_rounds(self):
        self.store.update_player(1,{"name":"新选手"})
        payload = self.payload(); preview = self.other.import_rounds(payload)
        before = self.other.state(); original = self.other._save_match; calls = []
        def fail_late(*args,**kwargs):
            calls.append(1)
            if len(calls)==2: raise AppError("模拟第二局保存失败")
            return original(*args,**kwargs)
        with patch.object(self.other,"_save_match",side_effect=fail_late):
            with self.assertRaises(AppError): self.other.import_rounds({**payload,"fingerprint":preview["fingerprint"]},commit=True)
        self.assertEqual(self.other.state(),before)

    def test_override_target_competition(self):
        target = self.other.create_competition({"name":"统一导入"})
        self.preview_commit(self.payload(competition_id=target))
        self.assertEqual({m["competition_id"] for m in self.other.state()["matches"]},{target})
        self.assertEqual(len(self.other.state()["competitions"]),2)

    def test_csv_preserves_formula_names_and_apostrophes(self):
        for pid,name in ((1,"=SUM(A1:A2)"),(2,"'昵称"),(3,"-选手")):
            self.store.update_player(pid,{"name":name})
        state = self.store.state(); raw = detail_csv_bytes(state,state["matches"])
        self.preview_commit(upload(raw,"scores.csv"))
        self.assertEqual(semantics(self.other.state()),semantics(state))

    def test_renamed_entities_and_foreign_aliases_do_not_duplicate(self):
        payload = self.payload(); self.preview_commit(payload)
        imported = self.other.state()
        cid = next(c["id"] for c in imported["competitions"] if c["name"]=="决赛")
        self.other.update_player(1,{"name":"更名后的选手"})
        self.other.update_competition(1,{"name":"更名后的比赛","revision":1})
        self.other.update_competition(cid,{"name":"更名后的决赛","revision":1})
        self.preview_commit({**payload,"mode":"copy"})
        state = self.other.state()
        self.assertEqual(len(state["players"]),3); self.assertEqual(len(state["competitions"]),2)
        self.assertEqual({m["competition_name"] for m in state["matches"]},{"更名后的比赛","更名后的决赛"})
        self.assertIn("更名后的选手",[e["name"] for m in state["matches"] for e in m["entries"]])

    def test_archive_and_import_keep_existing_roster_state(self):
        self.other.update_player(1,{"active":False})
        self.preview_commit(self.payload())
        self.assertFalse(self.other.state()["players"][0]["active"])

    def test_invalid_group_or_chip_never_changes_data(self):
        state = self.store.state(); headers,rows = detail_table(state,state["matches"])
        for bad in (rows[:-1], [*rows[:4], [*rows[4][:13], 99, *rows[4][14:]], *rows[5:]]):
            before = self.other.state()
            with self.assertRaises(AppError): self.other.import_rounds(upload(tables.csv_table(headers,bad),"bad.csv"))
            self.assertEqual(self.other.state(),before)

    def test_template_and_old_summary_rejection(self):
        template = import_template_bytes(self.other.state())
        result = self.preview_commit(upload(template))
        self.assertEqual(result["created"],1); self.assertEqual(len(result["new_players"]),3)
        self.assertEqual(sorted(e["total_units"] for e in self.other.state()["matches"][0]["entries"]),[-1900000,-150000,2050000])
        state = self.store.state()
        with self.assertRaisesRegex(AppError,"横向表"):
            self.other.import_rounds(upload(excel_bytes(state,state["matches"],include_details=False)))

    def test_excel_numeric_datetime_and_cached_formulas(self):
        raw = import_template_bytes(self.other.state())
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            files = {name:z.read(name) for name in z.namelist()}
        sheet = ET.fromstring(files['xl/worksheets/sheet1.xml'])
        for row in sheet.findall(f'{{{tables.NS}}}sheetData/{{{tables.NS}}}row')[1:]:
            for cell in row:
                if cell.get('r','').startswith('F'):
                    cell.clear(); cell.set('r','F'+row.get('r')); ET.SubElement(cell,f'{{{tables.NS}}}v').text='46000.8125'
        files['xl/worksheets/sheet1.xml']=ET.tostring(sheet)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
            for name,value in files.items(): z.writestr(name,value)
        rows = tables.read_import_table(buffer.getvalue(),'saved.xlsx')
        self.assertEqual(rows[0]['比赛时间'],'2025-12-09T19:30')
        self.preview_commit(upload(buffer.getvalue()))
        self.assertEqual(self.other.state()['matches'][0]['played_at'],'2025-12-09T19:30')

    def test_malformed_workbooks_produce_validation_errors(self):
        for content in (b'not a zip', b'', b'x'*1024):
            with self.assertRaises(AppError): self.other.import_rounds(upload(content))
        with self.assertRaises(AppError): self.other.import_rounds({"filename":"test.xlsx","content":"not base64"})
        with self.assertRaises(AppError): self.other.import_rounds(upload(b'\xff\xfeA\x00','test.xls'))


class NewHttpTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.server = AppServer(('127.0.0.1',0),self.store)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        super().tearDown()

    def request(self,path,method='GET',body=None):
        req = urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,
                                     method=method,headers={'Content-Type':'application/json','X-App-Token':self.server.app_token})
        try: response = urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as error: response = error
        with response: return response.status,response.read()

    def test_complete_http_competition_statistics_export_and_import(self):
        status,body = self.request('/api/competitions','POST',{'name':'本场比赛'})
        self.assertEqual(status,201); cid = json.loads(body)['id']
        self.assertEqual(self.request('/api/matches','POST',points(cid=cid))[0],201)
        status,body = self.request(f'/api/technical?competition={cid}&player=1')
        self.assertEqual(status,200)
        stats = json.loads(body); self.assertEqual(stats['matches'],1)
        self.assertEqual(stats['opponents'][0]['win_rate'],1)
        self.assertEqual(self.request('/api/technical?player=999')[0],400)
        status,excel = self.request(f'/api/export.xlsx?competition={cid}')
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(excel)) as z: self.assertIn('xl/worksheets/sheet2.xml',z.namelist())
        payload = upload(excel,mode='copy')
        status,body = self.request('/api/import/preview','POST',payload)
        self.assertEqual(status,200); preview = json.loads(body)['import']
        self.assertEqual(preview['created'],1); self.assertEqual(len(self.store.state()['matches']),1)
        status,body = self.request('/api/import','POST',{**payload,'fingerprint':preview['fingerprint']})
        self.assertEqual(status,200); self.assertEqual(len(self.store.state()['matches']),2)
        self.assertEqual(self.request('/api/import','POST',{**payload,'fingerprint':preview['fingerprint']})[0],409)
        self.assertEqual(self.request('/api/export-details.csv?competition=1')[0],200)
        self.assertEqual(self.request(f'/api/technical.xlsx?competition={cid}&player=1')[0],200)
        self.assertEqual(self.request('/api/import-template.xlsx')[0],200)
        matches = self.store.state()['matches']
        self.assertEqual(self.request('/api/matches/bulk-move','POST',{'competition_id':1,'matches':[{'id':m['id'],'revision':m['revision']} for m in matches]})[0],200)
        self.assertEqual(self.request(f'/api/competitions/{cid}','DELETE',{'revision':1,'matches':[]})[0],200)

    def test_season_13_shared_games_results_scope_and_excel(self):
        for name in ('皮卡丘','bear','kbg'):
            self.assertEqual(self.request('/api/players','POST',{'name':name})[0],201)
        status,body = self.request('/api/competitions','POST',{'name':'Season 13'})
        self.assertEqual(status,201); cid = json.loads(body)['id']
        fixtures = [((1,2,3),(83,-55,-28),8),((4,5,6),(175,-90,-85),6),((5,1,3),(72,-131,59),2)]
        for index,(ids,bases,count) in enumerate(fixtures):
            record = manual(key=f'season-13-{index}',cid=cid,ids=ids,bases=bases)
            for entry in record['entries']: entry['battle_count'] = count
            self.assertEqual(self.request('/api/matches','POST',record)[0],201)
        other = manual(key='other-season',cid=1,ids=(1,4,6),bases=(100,-50,-50))
        for entry in other['entries']: entry['battle_count'] = 6
        self.assertEqual(self.request('/api/matches','POST',other)[0],201)
        status,body = self.request(f'/api/technical?competition={cid}&player=1')
        self.assertEqual(status,200); stats = json.loads(body)
        self.assertEqual(stats['matches'],3)
        expected = {3:(10,2,1,0,1,0.5,-310000),2:(8,1,1,0,0,1,550000),5:(2,1,0,0,1,0,-720000)}
        self.assertEqual({r['id'] for r in stats['opponents']},set(expected))
        for row in stats['opponents']:
            self.assertEqual(tuple(row[key] for key in ('matches','sessions','wins','draws','losses','win_rate','difference_units')),expected[row['id']])
            self.assertNotIn('average_difference_units',row)
        status,body = self.request('/api/technical?competition=1&player=1')
        self.assertEqual(status,200)
        self.assertEqual({r['id'] for r in json.loads(body)['opponents']},{4,6})
        status,body = self.request(f'/api/technical?competition={cid}&player=1&end=2026-10-08')
        self.assertEqual(status,200); self.assertEqual(json.loads(body)['opponents'],[])
        status,excel = self.request(f'/api/technical.xlsx?competition={cid}&player=1')
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(excel)) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
            rows = [[cell.findtext(f'{{{tables.NS}}}v') if cell.get('t')!='inlineStr' else cell.findtext(f'{{{tables.NS}}}is/{{{tables.NS}}}t')
                     for cell in row] for row in sheet.find(f'{{{tables.NS}}}sheetData')]
            self.assertEqual(rows[0],['查询选手','对手','同桌局数','同桌场数','胜','平','负','胜率','累计分数流向'])
            self.assertEqual(len(rows),4)
            self.assertTrue(all(len(row)==9 for row in rows))
            self.assertEqual(rows[1],['选手 A','选手 C','10','2','1','0','1','0.5','-31'])

    def test_import_request_can_exceed_normal_json_size_limit(self):
        rows = tables.read_import_table(import_template_bytes(self.store.state()),'template.xlsx')
        headers = list(rows[0])[:-1]; records = []
        for i in range(800):
            for row in rows:
                item = {**row,'对局标识':str(uuid.uuid5(uuid.NAMESPACE_URL,f'large-import-{i}')),'备注':'长备注'*60}
                records.append([item[h] for h in headers])
        payload = upload(tables.csv_table(headers,records),'large.csv')
        self.assertGreater(len(json.dumps(payload).encode()),524288)
        status,body = self.request('/api/import/preview','POST',payload)
        self.assertEqual(status,200,body)
        self.assertEqual(json.loads(body)['import']['created'],800)
        self.assertEqual(self.store.state()['matches'],[])


if __name__ == "__main__":
    unittest.main()
