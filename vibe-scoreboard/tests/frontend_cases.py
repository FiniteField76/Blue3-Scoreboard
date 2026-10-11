"""Emit deterministic Python-calculated inputs for the optional Node UI checks."""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import point_scores, score_cents, score_text

rng = random.Random(31050)
cases = []
for i in range(300):
    points = [rng.randint(-50, 1200) for _ in range(3)]
    if i % 5 == 0:
        points[2] = 0
    if i % 9 == 0:
        points[1] = points[2]
    chips = [rng.randint(-10, 10) for _ in range(2)]
    chips.append(-sum(chips))
    entries = [{"player_id": pid, "points": p, "chip": c, "extra": rng.choice(["0", "-2.05", "1.25", "0.01"])}
               for pid, p, c in zip((1,2,3), points, chips)]
    order = sorted((1,2,3), key=lambda pid: (-points[pid-1], -pid))
    awards = {str(pid): rng.choice([other for other in (1,2,3) if other != pid])
              for pid in (1,2,3) if points[pid-1] == 0}
    rate = rng.choice(["0.01", "0.25", "1", "2.5", "5", "12.34"])
    bases, _, _ = point_scores(entries, order, awards)
    payload = {"scoring_mode": "points", "rate": rate, "rank_order": order, "knockouts": awards, "entries": entries}
    cases.append({"input": payload, "bases": [bases[pid] for pid in (1,2,3)],
                  "totals": [(bases[e["player_id"]]+score_cents(e["extra"]))*score_cents(rate) for e in entries]})

if __name__ == "__main__":
    if "--multi" in sys.argv:
        import tempfile
        from app import ScoreStore, technical_stats
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreStore(Path(directory)/"scores.sqlite3")
            for name in ("选手 D","选手 E","选手 F","选手 G"):
                store.create_player({"name":name})
            sample = {"request_key":"multi-front-end","played_at":"2026-10-11T14:30","competition_id":1,
                      "scoring_mode":"multi","rate":"1.25","entries":[
                {"player_id":pid,"base":base} for pid,base in zip(range(1,8),("35.55","-10.25","0","8","-2","8","-39.3"))]}
            store.save_match(sample)
            state = store.state()
            multi_cases = []
            for count in (1,4,7,200):
                for rate in ("0.01","0.25","1.25","5"):
                    entries = [{"player_id":pid,"base":rng.choice(["35.55","-10.25","0","8","-2","0.01"])} for pid in range(1,count+1)]
                    entries[-1]["base"] = score_text(-sum(score_cents(e["base"]) for e in entries[:-1]))
                    multi_cases.append({"input":{"scoring_mode":"multi","rate":rate,"entries":entries},
                        "bases":[score_cents(e["base"]) for e in entries],
                        "totals":[score_cents(e["base"])*score_cents(rate) for e in entries]})
            print(json.dumps({"state":state,"cases":multi_cases,
                              "checks":[{"player_id":pid,"expected":technical_stats(state,state["matches"],pid)} for pid in range(1,8)]}))
    elif "--technical" in sys.argv:
        import tempfile
        from app import ScoreStore, technical_stats
        with tempfile.TemporaryDirectory() as directory:
            store = ScoreStore(Path(directory)/"scores.sqlite3")
            store.create_player({"name":"选手 D"}); store.create_player({"name":"未交手"})
            competition = store.create_competition({"name":"决赛"})
            store.save_match({"request_key":"technical-1","played_at":"2026-10-01T20:00","entries":[
                {"player_id":pid,"base":base,"extra":"0"} for pid,base in zip((1,2,4),("0","0","0"))]})
            store.save_match({"competition_id":competition,"request_key":"technical-2","played_at":"2026-10-02T20:00",
                              "scoring_mode":"points","rate":"1.25","entries":[
                {"player_id":pid,"points":p,"chip":c,"extra":"0.15"} for pid,p,c in zip((1,2,3),(500,400,150),(-100,100,0))]})
            store.save_match({"competition_id":competition,"request_key":"technical-top-tie","played_at":"2026-10-03T20:00",
                              "scoring_mode":"points","rate":"5","entries":[
                {"player_id":pid,"points":p,"chip":c,"extra":"0"} for pid,p,c in zip((1,2,3),(500,500,50),(-100,100,0))]})
            store.save_match({"competition_id":competition,"request_key":"technical-bottom-tie","played_at":"2026-10-04T20:00",
                              "scoring_mode":"points","rate":"5","rank_order":[1,3,2],"entries":[
                {"player_id":pid,"points":p,"chip":c,"extra":"0"} for pid,p,c in zip((1,2,3),(600,400,400),(-100,100,0))]})
            state = store.state()
            checks = [{"competition_ids":ids,"player_id":pid,"expected":technical_stats(state,[m for m in state["matches"] if not ids or m["competition_id"] in ids],pid)}
                      for ids in ([],[1],[competition]) for pid in range(1,6)]
            print(json.dumps({"state":state,"checks":checks}))
    elif "--flows" in sys.argv:
        from app import score_flows
        samples = [[1000000,-500000,-500000],[1005000,-502500,-502500],[20000,10000,-10000,-10000,-10000],
                   [1000000,-600000,-300000],[0,0,0],[1000000,500000,0],
                   [i*10000 for i in range(1,101)]+[-i*10000 for i in range(1,101)],
                   [999999000000]*100+[-999999000000]*100]
        for _ in range(60):
            values = [rng.randint(-1000000,1000000) for _ in range(rng.randint(2,20))]
            values[-1] = -sum(values[:-1]); samples.append(values)
        flow_cases = []
        for scores in samples:
            match = {"entries":[{"player_id":i+1,"total_units":value} for i,value in enumerate(scores)]}
            flow_cases.append({"match":match,"expected":[[a,b,v] for (a,b),v in sorted(score_flows(match).items())]})
        flow_cases.append({"match":{**flow_cases[0]["match"],"record_kind":"counts"},"expected":[]})
        print(json.dumps(flow_cases))
    else:
        print(json.dumps(cases))
