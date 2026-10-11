"use strict";
// Optional: node tests/test_frontend.js. Uses a small DOM stub, not a browser renderer.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");
const root = path.resolve(__dirname,"..");
const elements = new Map();

class Element {
  constructor(id="",tag="div",attrs="") {
    this.id=id; this.tagName=tag.toUpperCase(); this.value=""; this.checked=false; this.open=false;
    this.disabled=false; this.dataset={}; this.style={}; this.listeners={}; this.options=[];
    this.textContent=""; this.validity={valid:true}; this.offsetWidth=130; this.offsetHeight=60;
    this.className=attrs.match(/class="([^"]*)"/)?.[1] || "";
    this.classList={
      add:(...names)=>this.setClasses(names,true), remove:(...names)=>this.setClasses(names,false),
      toggle:(name,force)=>{ const next=force ?? !this.className.split(/\s+/).includes(name); this.setClasses([name],next); return next; },
      contains:(name)=>this.className.split(/\s+/).includes(name)
    };
    this.value=attrs.match(/\bvalue="([^"]*)"/)?.[1] || "";
  }
  setClasses(names,add) {
    const classes=new Set(this.className.split(/\s+/).filter(Boolean));
    for(const name of names) if(add) classes.add(name); else classes.delete(name);
    this.className=[...classes].join(" ");
  }
  set innerHTML(value) {
    this._html=value; parseMarkup(value);
    if(this.tagName==="SELECT") {
      this.options=[...value.matchAll(/<option\b([^>]*)>/g)].map((o)=>({value:o[1].match(/value="([^"]*)"/)?.[1] || "",selected:/\bselected\b/.test(o[1])}));
      this.value=(this.options.find((o)=>o.selected)||this.options[0])?.value || "";
    }
  }
  get innerHTML() { return this._html || ""; }
  addEventListener(name,fn) { (this.listeners[name] ??= []).push(fn); }
  showModal() { this.open=true; }
  close() { const opened=this.open; this.open=false; if(opened) for(const fn of this.listeners.close || []) fn({}); }
  querySelector() { return this.child ??= new Element(); }
  append() {}
  remove() {}
  focus() {}
  getBoundingClientRect() { return {left:0,top:0,width:460,height:225}; }
}
function parseMarkup(html) {
  for(const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*?)\bid="([^"]+)"([^>]*)>/g)) {
    const [,tag,before,id,after]=match, attrs=before+after;
    elements.set(id,new Element(id,tag,attrs));
  }
  for(const match of html.matchAll(/<select\b[^>]*\bid="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    const el=elements.get(match[1]);
    el.options=[...match[2].matchAll(/<option\b([^>]*)>/g)].map((option)=>({
      value:option[1].match(/value="([^"]*)"/)?.[1] || "", selected:/\bselected\b/.test(option[1]), disabled:/\bdisabled\b/.test(option[1])
    }));
    el.value=(el.options.find((o)=>o.selected) || el.options[0])?.value || "";
  }
}
parseMarkup(fs.readFileSync(path.join(root,"static/index.html"),"utf8"));
const document={
  getElementById:(id)=>elements.get(id) || null,
  createElement:(tag)=>new Element("",tag), body:new Element("","body"), activeElement:{tagName:"BODY"}, hidden:false,
  addEventListener(){}, querySelector(selector){return selector.startsWith("[data-view=") ? new Element() : selector==="dialog[open]" ? [...elements.values()].find((el)=>el.tagName==="DIALOG" && el.open) || null : null;},
  querySelectorAll:(selector)=>[...elements.values()].filter((el)=>el.classList.contains(selector.slice(1)))
};
const context=vm.createContext({document,console,requests:[],setTimeout:()=>0,clearTimeout(){},setInterval(){},
  window:{addEventListener(){}},location:{hash:"#overview"},history:{replaceState(){}},
  URLSearchParams,AbortController,btoa:(binary)=>Buffer.from(binary,"binary").toString("base64"),crypto:{randomUUID:()=>"ui-request-1234"}});
const source=fs.readFileSync(path.join(root,"static/app.js"),"utf8");
const bootstrap=source.lastIndexOf("\nswitchView(location.hash.slice(1));");
assert(bootstrap>0);
vm.runInContext(source.slice(0,bootstrap),context);
const run=(code)=>vm.runInContext(code,context);
const plain=(value)=>JSON.parse(JSON.stringify(value));
const cases=JSON.parse(execFileSync(process.env.SCOREBOARD_TEST_PYTHON || "python3",[path.join(__dirname,"frontend_cases.py")],{cwd:root,encoding:"utf8"}));
for(const item of cases) {
  context.payload=item.input;
  const result=run("calculateMatch(payload)");
  assert.equal(result.error,undefined);
  assert.deepEqual(plain(result.bases),item.bases);
  assert.deepEqual(plain(result.totals),item.totals);
  assert.equal(result.bases.reduce((a,b)=>a+b,0),0);
}

const players=Array.from({length:6},(_,i)=>({id:i+1,name:String.fromCharCode(65+i),active:true,color:"#5878f4"}));
function setData(matches=[]) {
  context.fixture={players,competitions:[{id:1,name:"联赛",note:"",revision:1},{id:2,name:"练习赛",note:"",revision:1}],matches,settings:{rate:"5"},token:"test-token"};
  run("data=fixture; selectedMatchIds.clear(); selectedPlayerIds.clear(); chartSelectedIds=null; selectedCompetitionIds=null; page=0;");
  elements.get("chart-mode").value="personal";
}
function fillMatch(points,chips=[0,0,0]) {
  for(let i=0;i<3;i++) { elements.get(`points-${i}`).value=String(points[i]); elements.get(`chip-${i}`).value=String(chips[i]); }
  return run("validateMatch()");
}
function graphMatch(id,ids,scores) {
  return {id,revision:1,played_at:`2026-10-${String(id).padStart(2,"0")}T19:30`,note:"",rate:"1",scoring_mode:"manual",rank_order:[],knockouts:{},
    entries:ids.map((pid,i)=>({player_id:pid,name:players[pid-1].name,base_cents:scores[i]*100,extra_cents:0,base_units:scores[i]*10000,extra_units:0,total_units:scores[i]*10000}))};
}

(async()=>{
  context.multiReadError=null;
  run("api=async(url,options)=>{requests.push({url,method:options.method,body:JSON.parse(options.body)});if(url==='/api/multi-scores/preview'){if(multiReadError)throw Error(multiReadError);return {ok:true,draft:multiDraft};}return {ok:true,id:1};};loadState=async()=>true;");
  setData();
  run("openMatch()");
  assert.equal(elements.get("scoring-mode").value,"points");
  assert.deepEqual([0,1,2].map((i)=>elements.get(`battle-count-${i}`).value),["1","1","1"]);
  [0,1,2].forEach((i)=>{elements.get(`battle-count-${i}`).value="9";});
  assert.equal(fillMatch([500,400,150],[2,-1,-1]),true);
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["+205","-15","-190"]);
  await run("saveMatch({preventDefault(){}})");
  assert.equal(context.requests.at(-1).body.rate,"5");
  assert.deepEqual(plain(context.requests.at(-1).body.entries.map((e)=>e.battle_count)),["9","9","9"]);
  assert.deepEqual(plain(context.requests.at(-1).body.rank_order),[1,2,3]);

  run("openMatch()");
  assert.equal(fillMatch([500,500,50]),true);
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["+112.5","+112.5","-225"]);
  assert(elements.get("rank-label-0").classList.contains("hidden"));
  assert(elements.get("rank-label-1").classList.contains("hidden"));
  assert.equal(fillMatch([500,500,50],[-100,100,0]),true);
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["-1,387.5","+1,612.5","-225"]);
  await run("saveMatch({preventDefault(){}})");
  assert.deepEqual(plain(context.requests.at(-1).body.rank_order),[1,2,3]);

  run("openMatch()");
  assert.equal(fillMatch([600,400,400]),false);
  assert(!elements.get("rank-label-1").classList.contains("hidden"));
  elements.get("rank-1").value="3"; elements.get("rank-2").value="2";
  assert.equal(run("validateMatch()"),true);
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["+50","-50","0"]);
  await run("saveMatch({preventDefault(){}})");
  assert.deepEqual(plain(context.requests.at(-1).body.rank_order),[1,3,2]);

  setData();
  run("openMatch(null,2)");
  elements.get("scoring-mode").value="multi";
  elements.get("match-form").listeners.change[0]();
  assert.equal(elements.get("match-dialog").open,false);
  assert.equal(elements.get("multi-dialog").open,true);
  assert.equal(elements.get("multi-competition").value,"2");
  assert.equal(run("multiRows.length"),4);
  assert.equal(elements.get("multi-count-0").value,"1");
  elements.get("multi-count-0").value="9";
  elements.get("multi-rate").value="1.25";
  [10,-5,8.5,-13.5].forEach((value,i)=>{elements.get(`multi-base-${i}`).value=String(value);});
  assert.equal(run("validateMulti()"),true);
  assert.deepEqual([0,1,2,3].map((i)=>elements.get(`multi-total-${i}`).textContent),["+12.5","-6.25","+10.625","-16.875"]);
  run("addMultiPlayers(true)");
  assert.equal(run("multiRows.length"),6);
  assert.equal(elements.get("multi-base-0").value,"10");
  assert.equal(elements.get("multi-count-0").value,"9");
  elements.get("multi-base-4").value="2"; elements.get("multi-base-5").value="-2";
  assert.equal(run("validateMulti()"),true);
  elements.get("multi-count-0").value="1.5";
  assert.equal(run("validateMulti()"),false);
  assert(elements.get("multi-summary").textContent.includes("对战数量"));
  elements.get("multi-count-0").value="9";
  elements.get("multi-body").listeners.click[0]({target:{closest:()=>({dataset:{removeMulti:"1"}})}});
  assert.equal(run("multiRows.length"),5);
  assert.equal(run("validateMulti()"),false);
  assert(elements.get("multi-summary").textContent.includes("基础分合计必须为 0"));
  const beforeInvalidSave=context.requests.length;
  await run("saveMultiMatch({preventDefault(){}})");
  assert.equal(context.requests.length,beforeInvalidSave);
  elements.get("multi-base-0").value="5";
  assert.equal(run("validateMulti()"),true);
  await run("saveMultiMatch({preventDefault(){}})");
  const multiSaved=context.requests.at(-1);
  assert.equal(multiSaved.url,"/api/matches");
  assert.equal(multiSaved.method,"POST");
  assert.equal(multiSaved.body.scoring_mode,"multi");
  assert.equal(multiSaved.body.competition_id,2);
  assert.equal(multiSaved.body.rate,"1.25");
  assert.deepEqual(plain(multiSaved.body.entries.map((e)=>e.battle_count)),["9","1","1","1","1"]);
  assert.deepEqual(plain(multiSaved.body.entries.map((e)=>e.player_id)),[1,3,4,5,6]);
  assert(multiSaved.body.entries.every((e)=>!Object.hasOwn(e,"chip") && !Object.hasOwn(e,"extra")));

  const multiFixture=JSON.parse(execFileSync(process.env.SCOREBOARD_TEST_PYTHON || "python3",[path.join(__dirname,"frontend_cases.py"),"--multi"],{cwd:root,encoding:"utf8"}));
  context.fixture=multiFixture.state; run("data=fixture; selectedCompetitionIds=null;");
  for(const item of multiFixture.cases) {
    context.payload=item.input;
    const result=run("calculateMatch(payload)");
    assert.equal(result.error,undefined);
    assert.deepEqual(plain(result.bases),item.bases);
    assert.deepEqual(plain(result.totals),item.totals);
    assert.equal(result.bases.reduce((a,b)=>a+b,0),0);
  }
  for(const check of multiFixture.checks) {
    context.chosenPlayer=check.player_id;
    const calculated=plain(run("technicalStats(data.matches,chosenPlayer)"));
    for(const key of ["players","opponents"]) {
      assert.equal(calculated[key].length,check.expected[key].length);
      check.expected[key].forEach((record,i)=>{for(const field of Object.keys(record)) assert.equal(calculated[key][i][field],record[field]);});
    }
  }
  run("openMatch(1)");
  assert.equal(elements.get("multi-dialog").open,true);
  assert.equal(run("multiRows.length"),7);
  assert.equal(elements.get("multi-base-0").value,"35.55");
  assert.equal(elements.get("multi-rate").value,"1.25");
  elements.get("multi-rate").value="2";
  await run("saveMultiMatch({preventDefault(){}})");
  assert.equal(context.requests.at(-1).url,"/api/matches/1");
  assert.equal(context.requests.at(-1).method,"PUT");
  assert.equal(context.requests.at(-1).body.revision,1);
  assert.equal(context.requests.at(-1).body.entries.length,7);

  run("openMultiMatch()");
  elements.get("multi-kind").value="counts";
  elements.get("multi-kind").listeners.change[0]();
  assert.equal(elements.get("multi-base-0").value,"0");
  elements.get("multi-count-0").value="63";
  assert.equal(run("validateMulti()"),true);
  await run("saveMultiMatch({preventDefault(){}})");
  assert.equal(context.requests.at(-1).body.record_kind,"counts");
  assert.equal(context.requests.at(-1).body.entries[0].battle_count,"63");
  assert(context.requests.at(-1).body.entries.every((e)=>e.base==="0"));

  run("openMultiMatch()");
  context.multiDraft={entries:[1,2,3,4].map((pid)=>({player_id:pid,player_name:multiFixture.state.players.find((p)=>p.id===pid).name,base:String(pid)}))
    .concat({player_id:null,player_name:"新 X",base:"-10"}),new_players:["新 X"]};
  const unchanged=JSON.stringify(context.fixture);
  elements.get("multi-paste").value="选手 A\t1\n选手 B\t2\n选手 C\t3\n选手 D\t4\n新 X\t-10";
  await run("readMultiScores(true)");
  assert.equal(context.requests.at(-1).url,"/api/multi-scores/preview");
  assert.equal(run("multiRows.length"),5);
  assert.equal(elements.get("multi-total-4").textContent,"-10");
  assert.equal(elements.get("save-multi").disabled,false);
  assert.equal(JSON.stringify(context.fixture),unchanged); // Reading a table does not write a round or create players.
  context.multiReadError="多人对局的基础分合计必须为 0, 当前为 0.01.";
  elements.get("multi-paste").value="选手 A\t0.1\n选手 B\t0.2\n选手 C\t-0.29";
  await run("readMultiScores(true)");
  assert(elements.get("multi-error").textContent.includes("基础分合计必须为 0"));
  assert.equal(run("multiRows.length"),5); // Failed loading preserves the previous draft.
  assert.equal(elements.get("multi-base-4").value,"-10");
  context.multiReadError=null;
  const scoreFile=Buffer.from("选手,基础分\n选手 A,1\n选手 B,2\n选手 C,3\n选手 D,4\n新 X,-10");
  elements.get("multi-file").files=[{name:"scores.csv",size:scoreFile.length,arrayBuffer:async()=>scoreFile.buffer.slice(scoreFile.byteOffset,scoreFile.byteOffset+scoreFile.byteLength)}];
  await run("readMultiScores()");
  assert.equal(context.requests.at(-1).body.filename,"scores.csv");
  assert.equal(context.requests.at(-1).body.content,scoreFile.toString("base64"));
  await run("saveMultiMatch({preventDefault(){}})");
  assert.equal(context.requests.at(-1).body.entries[4].player_id,null);
  assert.equal(context.requests.at(-1).body.entries[4].player_name,"新 X");

  setData();
  const count=context.requests.length;
  run("openMatch()");
  assert.equal(fillMatch([830,290,0]),true);
  await run("saveMatch({preventDefault(){}})");
  assert.equal(context.requests.length,count); // Award dialog must precede any write.
  assert.equal(elements.get("knockout-dialog").open,true);
  assert(elements.get("knockout-recipient").options.every((o)=>o.value!=="3"));
  elements.get("knockout-recipient").value="2";
  run("finishKnockout({preventDefault(){}})");
  for(let i=0;i<5;i++) await Promise.resolve();
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["+355","-55","-300"]);
  assert.deepEqual(plain(context.requests.at(-1).body.knockouts),{"3":2});

  run("openMatch()");
  assert.equal(fillMatch([1050,0,0]),false);
  elements.get("rank-1").value="2"; elements.get("rank-2").value="3";
  assert.equal(run("validateMatch()"),true);
  run("beginKnockouts(false,true)");
  elements.get("knockout-recipient").value="1";
  run("finishKnockout({preventDefault(){}})");
  assert.equal(elements.get("knockout-dialog").open,true);
  assert(elements.get("knockout-description").textContent.startsWith("C"));
  elements.get("knockout-recipient").value="1";
  run("finishKnockout({preventDefault(){}})");
  assert.deepEqual([0,1,2].map((i)=>elements.get(`total-${i}`).textContent),["+600","-300","-300"]);
  assert.equal(elements.get("knockout-dialog").open,false);

  setData([graphMatch(1,[1,2,3],[30,-10,-20])]);
  run("openMatch(1)");
  assert.equal(elements.get("scoring-mode").value,"manual");
  assert.equal(elements.get("match-rate").value,"1");
  assert.deepEqual([0,1,2].map((i)=>elements.get(`base-${i}`).value),["30","-10","-20"]);
  assert.equal(run("validateMatch()"),true);
  elements.get("match-dialog").close();

  const history=[graphMatch(1,[1,2,3],[100,100,100]),graphMatch(2,[1,2,3],[50,-50,0]),
    graphMatch(3,[1,2,3],[-60,10,10]),graphMatch(4,[4,5,6],[20,30,40])];
  setData(history);
  assert.deepEqual(plain(run("chartRange(participationSeries(data.matches,data.players))")),{lo:200000,hi:1500000});
  const before=JSON.stringify(context.fixture);
  run("renderChart(data.matches,[data.players[3]])");
  const svg=elements.get("chart-container").innerHTML;
  assert(svg.includes('data-y-min="200000" data-y-max="200000"'));
  assert(svg.includes('viewBox="0 0 460 704"'));
  assert(!svg.includes("chart-padding-line"));
  assert(!svg.includes("chart-hover-point"));
  assert(svg.includes('class="chart-endpoint" data-chart-endpoint="4"'));
  assert(!svg.includes("NaN"));
  assert.equal(JSON.stringify(context.fixture),before);
  const series=run("participationSeries(data.matches,[data.players[3]])");
  assert.equal(series[0].actualMatches,1);
  assert.deepEqual(plain(series[0].values),[0,200000]);
  assert.equal(context.fixture.matches.length,4);
  // Curves and markers stop at each player's own record count; the shorter one stops before the longest.
  run("renderChart(data.matches,[data.players[0],data.players[3],data.players[5]])");
  const ownSvg=elements.get("chart-container").innerHTML;
  const ownCurves=[...ownSvg.matchAll(/<polyline class="chart-line"[^>]*points="([^"]*)"/g)].map(m=>m[1].split(/\s+/));
  assert.deepEqual(ownCurves.map(p=>p.length),[4,2,2]);
  const ownMarkers=[...ownSvg.matchAll(/<circle class="chart-endpoint" data-chart-endpoint="(\d+)" cx="([^"]*)" cy="([^"]*)"/g)];
  assert.equal(ownMarkers.length,3);
  ownMarkers.forEach((marker,i)=>{assert.equal(`${marker[2]},${marker[3]}`,ownCurves[i].at(-1));});
  assert(Number(ownMarkers[1][2])<Number(ownMarkers[0][2]));
  const hoverMarker=id=>elements.get("chart-container").onpointermove({target:{closest:selector=>selector==="[data-chart-endpoint]" ? {dataset:{chartEndpoint:String(id)}} : null},clientX:200,clientY:100});
  hoverMarker(4);
  assert.equal(elements.get("chart-tooltip").innerHTML,"<strong>D</strong>对局数: 1 场<br>累计得分: +20");
  assert.equal(elements.get("chart-tooltip").classList.contains("hidden"),false);
  hoverMarker(1);
  assert.equal(elements.get("chart-tooltip").innerHTML,"<strong>A</strong>对局数: 3 场<br>累计得分: +90");
  // Personal mode offers only marker tooltips, never the full column of all selected players.
  elements.get("chart-container").onpointermove({target:{closest:selector=>selector==="[data-chart-index]" ? {dataset:{chartIndex:"1"}} : null},clientX:200,clientY:100});
  assert.equal(elements.get("chart-tooltip").classList.contains("hidden"),true);
  hoverMarker(1);elements.get("chart-container").onpointerleave();
  assert.equal(elements.get("chart-tooltip").classList.contains("hidden"),true);
  run("renderChart(data.matches,[data.players[0],data.players[1]])");
  assert(elements.get("chart-container").innerHTML.includes('data-chart-endpoint="1"'));
  assert(!elements.get("chart-container").innerHTML.includes('data-chart-endpoint="4"'));
  run("renderChart(data.matches,[data.players[0]])");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="900000" data-y-max="1500000"'));
  // Historical peak is 150, latest A=90; opening 0 is excluded even when all real totals are positive.
  context.fixture.matches.push(graphMatch(5,[4,5,6],[-20,0,0]));
  assert.equal(run("chartRange(participationSeries(data.matches,data.players)).lo"),0); // Actual later 0 still counts.
  // Only the currently displayed date/competition subset sets its range.
  run("renderChart([data.matches[1]],[data.players[0]])");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="500000" data-y-max="500000"'));
  run("renderChart([],data.players)");
  assert(elements.get("chart-container").innerHTML.includes("当前范围没有所选选手的录分记录"));

  // The user's ABCDE -> BCD example must rescale through the actual selection form.
  setData([graphMatch(1,[1,2,3,4,5,6],[5000,3200,-800,0,-5000,-2400])]);
  elements.get("period-select").value="all";
  run("openChartSelection(); draftChartIds=new Set([1,2,3,4,5]); generateSelectedChart({preventDefault(){}})");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="-50000000" data-y-max="50000000"'));
  run("openChartSelection(); draftChartIds.delete(1); draftChartIds.delete(5); generateSelectedChart({preventDefault(){}})");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="-8000000" data-y-max="32000000"'));
  assert(elements.get("trend-foot").textContent.includes("当前选手范围 -800 至 3,200"));
  run("openChartSelection(); draftChartIds.add(1); draftChartIds.add(5); generateSelectedChart({preventDefault(){}})");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="-50000000" data-y-max="50000000"'));
  run("renderChart(data.matches,[data.players[3]])");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="0" data-y-max="0"'));
  assert(!elements.get("chart-container").innerHTML.includes("NaN"));

  setData([graphMatch(1,[1,2,3],[10,10,10])]);
  run("renderChart(data.matches,[data.players[0]])");
  assert(elements.get("chart-container").innerHTML.includes('data-y-min="100000" data-y-max="100000"'));
  assert(!elements.get("chart-container").innerHTML.includes("NaN"));
  setData([graphMatch(1,[1,2,3],[-100,-100,-100]),graphMatch(2,[1,2,3],[-20,-50,-40])]);
  assert.deepEqual(plain(run("chartRange(participationSeries(data.matches,data.players))")),{lo:-1500000,hi:-1000000});
  run("renderChart(data.matches,[data.players[5]])");
  assert(elements.get("chart-container").innerHTML.includes("当前范围没有所选选手的录分记录"));
  assert(!elements.get("chart-container").innerHTML.includes("NaN"));

  // Every score record advances the global axis, even when none of the displayed players participated.
  const globalMatches=[graphMatch(1,[1,2,3],[100,-50,-50]),graphMatch(2,[4,5,6],[20,-20,0]),
    graphMatch(3,[1,2,3],[0,0,0]),graphMatch(4,[1,4,2],[-30,10,20]),graphMatch(5,[3,4,5],[0,0,0]),
    graphMatch(6,[1,2,3],[9000,-4500,-4500]),graphMatch(7,[1,2,3],[500,-250,-250])];
  globalMatches.forEach((m,i)=>{m.competition_id=i===5 ? 2 : 1;m.entries.forEach((e)=>{e.battle_count=8;});});
  globalMatches[1].played_at=globalMatches[0].played_at; // Record ID orders equal timestamps.
  globalMatches[2].record_kind="counts";globalMatches[2].entries.forEach((e)=>{e.battle_count=60;});
  globalMatches[3].entries.forEach((e)=>{e.battle_count=0;}); // Historical scores still advance one session.
  setData([...globalMatches].reverse());
  elements.get("period-select").value="custom";
  elements.get("start-date").value="2026-10-01";elements.get("end-date").value="2026-10-05";
  run("selectedCompetitionIds=new Set([1]);chartSelectedIds=new Set([1]);renderChart(filteredMatches(),selectedChartPlayers())");
  assert(elements.get("chart-container").innerHTML.includes("个人录分记录数"));
  const globalState=JSON.stringify(context.fixture);
  elements.get("chart-mode").value="global";elements.get("chart-mode").onchange();
  let globalPoints=plain(run("chartSeries(filteredMatches(),selectedChartPlayers())"));
  assert.deepEqual(globalPoints[0].values,[0,1000000,1000000,700000,700000]);
  assert.deepEqual(globalPoints[0].participated,[false,true,false,true,false]);
  assert.equal(globalPoints[0].actualMatches,2);
  assert.deepEqual(plain(run("chartRange(chartSeries(filteredMatches(),selectedChartPlayers()))")),{lo:700000,hi:1000000});
  assert(elements.get("chart-container").innerHTML.includes("横轴为全局场次"));
  assert(!elements.get("chart-container").innerHTML.includes("chart-padding-line"));
  assert(elements.get("trend-foot").textContent.includes("全局共 4 场"));
  elements.get("chart-container").onpointermove({target:{closest:selector=>selector==="[data-chart-index]" ? {dataset:{chartIndex:"2"}} : null},clientX:50,clientY:100});
  assert(elements.get("chart-tooltip").innerHTML.includes("全局第 2 场后累计得分"));
  assert(elements.get("chart-tooltip").innerHTML.includes("A: +100 (未参赛)"));
  assert(elements.get("chart-container").innerHTML.includes("chart-hover-point"));
  assert(elements.get("chart-container").innerHTML.lastIndexOf('class="chart-endpoint"')>elements.get("chart-container").innerHTML.lastIndexOf('class="chart-hover-point"'));
  hoverMarker(1);
  assert.equal(elements.get("chart-tooltip").innerHTML,"<strong>A</strong>对局数: 2 场<br>累计得分: +70");
  // Selection rescales the y axis while keeping all four global sessions, including A's sessions.
  run("openChartSelection();draftChartIds=new Set([4]);generateSelectedChart({preventDefault(){}})");
  globalPoints=plain(run("chartSeries(filteredMatches(),selectedChartPlayers())"));
  assert.deepEqual(globalPoints[0].values,[0,0,200000,300000,300000]);
  assert.deepEqual(plain(run("chartRange(chartSeries(filteredMatches(),selectedChartPlayers()))")),{lo:200000,hi:300000});
  assert(elements.get("trend-foot").textContent.includes("全局共 4 场"));
  assert.equal(elements.get("chart-mode").value,"global");
  assert.equal(JSON.stringify(context.fixture),globalState);
  // A real cumulative zero counts, unlike zero before the player's first participation.
  context.zeroMatch=graphMatch(8,[1,2,3],[-70,70,0]);
  assert.deepEqual(plain(run("chartRange(globalSeries([...filteredMatches(),zeroMatch],[data.players[0]]))")),{lo:0,hi:1000000});
  run("renderChart([data.matches.find((m)=>m.id===1),data.matches.find((m)=>m.record_kind==='counts')],[data.players[5]])");
  assert(elements.get("chart-container").innerHTML.includes("当前范围没有所选选手的录分记录"));
  assert.equal(elements.get("chart-export-button").disabled,true);
  assert.equal(run("chartImageSvg([],data.players,'联赛','全部时间')"),null);
  elements.get("chart-mode").value="personal";elements.get("chart-mode").onchange();
  assert(elements.get("chart-container").innerHTML.includes("个人录分记录数"));
  assert(elements.get("trend-foot").textContent.includes("个人最多 3 场"));
  assert.deepEqual(plain(run("chartSeries(filteredMatches(),[data.players[0]])[0].values")),[0,1000000,700000]);

  // PNG export must use the applied season, dates and selected players.
  const imageMatches=[graphMatch(1,[1,2,3],[100,-80,-20]),graphMatch(2,[1,2,3],[9000,-4500,-4500]),graphMatch(3,[1,2,3],[0,-10,10])];
  imageMatches.forEach((m,i)=>{m.competition_id=i===1 ? 2 : 1;});
  setData(imageMatches);
  elements.get("period-select").value="custom";
  elements.get("start-date").value="2026-10-01"; elements.get("end-date").value="2026-10-01";
  run("selectedCompetitionIds=new Set([1]); chartSelectedIds=new Set([2,3]); renderChart(filteredMatches(),selectedChartPlayers());");
  const blobUrls=new Map(), downloads=[], revoked=[], canvasCalls=[];
  let failImage=false, failPng=false;
  context.Blob=Blob;
  context.URL={createObjectURL:(blob)=>{const id=`blob:test-${blobUrls.size}`;blobUrls.set(id,blob);return id;},revokeObjectURL:(address)=>revoked.push(address)};
  context.Image=class {set src(address){this.address=address;if(failImage)this.onerror();else this.onload();}};
  const originalCreateElement=document.createElement;
  const imageCanvas={getContext:()=>({fillRect:(...args)=>canvasCalls.push({fill:args}),drawImage:(image)=>canvasCalls.push({address:image.address})}),
    toBlob:(callback,type)=>{canvasCalls.push({width:imageCanvas.width,height:imageCanvas.height,type});callback(failPng ? null : new Blob(["PNG"],{type}));}};
  document.createElement=(tag)=>{
    if(tag==="canvas")return imageCanvas;
    const element=originalCreateElement(tag);
    if(tag==="a")element.click=()=>downloads.push({url:element.href,filename:element.download});
    return element;
  };
  const imageState=JSON.stringify(context.fixture);
  assert.equal(elements.get("chart-export-button").disabled,false);
  await elements.get("chart-export-button").onclick();
  assert.equal(downloads.length,1);
  assert.match(downloads[0].filename,/^scoreboard-chart-\d{4}-\d{2}-\d{2}-1920x1080\.png$/);
  assert.equal(blobUrls.get(downloads[0].url).type,"image/png");
  assert.deepEqual(canvasCalls.at(-1),{width:1920,height:1080,type:"image/png"});
  const svgAddress=canvasCalls.find((item)=>item.address).address, imageSvg=await blobUrls.get(svgAddress).text();
  assert(imageSvg.includes('width="1920" height="1080" viewBox="0 0 1920 1080"'));
  assert(imageSvg.includes('data-y-min="-800000" data-y-max="-200000"'));
  assert(imageSvg.includes("联赛") && imageSvg.includes("2026-10-01 - 2026-10-01"));
  assert.equal((imageSvg.match(/class="chart-line"/g)||[]).length,2);
  assert(imageSvg.includes('aria-label="B, C 的累计得分走势'));
  assert(!imageSvg.includes("chart-hover-point"));
  assert(revoked.includes(svgAddress));
  assert.equal(JSON.stringify(context.fixture),imageState);
  assert.equal(elements.get("chart-export-button").disabled,false);
  // Export follows the selected global mode and keeps sessions played only by hidden players.
  setData([...globalMatches].reverse());
  elements.get("start-date").value="2026-10-01";elements.get("end-date").value="2026-10-05";
  run("selectedCompetitionIds=new Set([1]);chartSelectedIds=new Set([1])");
  elements.get("chart-mode").value="global";elements.get("chart-mode").onchange();
  await elements.get("chart-export-button").onclick();
  assert.equal(downloads.length,2);
  assert.deepEqual(canvasCalls.at(-1),{width:1920,height:1080,type:"image/png"});
  const globalSvgAddress=canvasCalls.filter((item)=>item.address).at(-1).address;
  const globalImageSvg=await blobUrls.get(globalSvgAddress).text();
  assert(globalImageSvg.includes("横轴为全局场次"));
  assert(globalImageSvg.includes("2026-10-01 - 2026-10-05"));
  assert(globalImageSvg.includes('data-y-min="700000" data-y-max="1000000"'));
  assert(!globalImageSvg.includes("chart-padding-line"));
  assert.equal(globalImageSvg.match(/<polyline[^>]*class="chart-line"[^>]*points="([^"]*)"/)[1].trim().split(/\s+/).length,5);
  assert.equal(elements.get("chart-export-button").disabled,false);
  // Personal PNG exports use real curve lengths and markers at each actual endpoint.
  run("chartSelectedIds=new Set([1,4])");
  elements.get("chart-mode").value="personal";elements.get("chart-mode").onchange();
  await elements.get("chart-export-button").onclick();
  assert.equal(downloads.length,3);
  const ownSvgAddress=canvasCalls.filter((item)=>item.address).at(-1).address;
  const ownImageSvg=await blobUrls.get(ownSvgAddress).text();
  const ownImageCurves=[...ownImageSvg.matchAll(/<polyline class="chart-line"[^>]*points="([^"]*)"/g)].map(m=>m[1].split(/\s+/));
  const ownImageMarkers=[...ownImageSvg.matchAll(/<circle class="chart-endpoint" cx="([^"]*)" cy="([^"]*)"/g)];
  assert.deepEqual(ownImageCurves.map(p=>p.length),[3,4]);
  assert.equal(ownImageMarkers.length,2);
  ownImageMarkers.forEach((marker,i)=>{assert.equal(`${marker[1]},${marker[2]}`,ownImageCurves[i].at(-1));});
  assert(Number(ownImageMarkers[0][1])<Number(ownImageMarkers[1][1]));
  assert(!ownImageSvg.includes("chart-padding-line") && !ownImageSvg.includes("chart-hover-point"));
  assert(ownImageSvg.includes('width="1920" height="1080"'));
  failImage=true;
  await elements.get("chart-export-button").onclick();
  assert.equal(downloads.length,3); assert.equal(run("chartExporting"),false);
  assert.equal(elements.get("chart-export-button").disabled,false);
  failImage=false; failPng=true;
  await elements.get("chart-export-button").onclick();
  assert.equal(downloads.length,3); assert.equal(run("chartExporting"),false);
  assert.equal(elements.get("chart-export-button").disabled,false);
  document.createElement=originalCreateElement;
  elements.get("period-select").value="all";
  setData([]); run("renderChart(filteredMatches(),selectedChartPlayers())");
  assert.equal(elements.get("chart-export-button").disabled,true);
  const urlCount=blobUrls.size;
  await run("exportChartImage()");
  assert.equal(blobUrls.size,urlCount); assert.equal(downloads.length,3);

  const batch=Array.from({length:15},(_,i)=>graphMatch(i+1,[1,2,3],[1,-1,0]));
  setData(batch);
  run("renderHistory(data.matches)");
  elements.get("history-container").listeners.change[0]({target:{dataset:{selectMatch:"1"},checked:true}});
  run("page=1;renderHistory(data.matches)");
  elements.get("history-container").listeners.change[0]({target:{id:"select-page-matches",checked:true,dataset:{}}});
  assert.equal(run("selectedMatchIds.size"),4);
  run("deleteSelectedMatches()");
  await elements.get("confirm-action").onclick();
  assert.equal(context.requests.at(-1).url,"/api/matches/bulk-delete");
  assert.deepEqual(plain(context.requests.at(-1).body.matches.map((m)=>m.id)),[1,13,14,15]);
  assert.equal(run("selectedMatchIds.size"),0);
  run("renderPlayers()");
  elements.get("players-grid").listeners.change[0]({target:{dataset:{selectPlayer:"2"},checked:true}});
  run("deleteSelectedPlayers()");
  assert(elements.get("confirm-message").textContent.includes("15 场完整对局"));
  await elements.get("confirm-action").onclick();
  assert.equal(context.requests.at(-1).url,"/api/players/bulk-delete");
  assert.deepEqual(plain(context.requests.at(-1).body.ids),[2]);
  assert.equal(context.requests.at(-1).body.matches.length,15);
  const technical=JSON.parse(execFileSync(process.env.SCOREBOARD_TEST_PYTHON || "python3",[path.join(__dirname,"frontend_cases.py"),"--technical"],{cwd:root,encoding:"utf8"}));
  const flowCases=JSON.parse(execFileSync(process.env.SCOREBOARD_TEST_PYTHON || "python3",[path.join(__dirname,"frontend_cases.py"),"--flows"],{cwd:root,encoding:"utf8",maxBuffer:8*1024*1024}));
  for(const check of flowCases){
    context.flowMatch=check.match;
    const calculated=plain(run("[...scoreFlows(flowMatch)].map(([key,value])=>[...key.split(':').map(Number),value]).sort((a,b)=>a[0]-b[0] || a[1]-b[1])"));
    assert.deepEqual(calculated,check.expected);
  }
  context.fixture=technical.state; run("data=fixture; selectedCompetitionIds=null; selectedMatchIds.clear();");
  assert.deepEqual(plain(run("Object.fromEntries(matchRanks(data.matches.find((m)=>m.played_at==='2026-10-03T20:00')))")),{"1":1,"2":1,"3":3});
  assert.deepEqual(plain(run("Object.fromEntries(matchRanks(data.matches.find((m)=>m.played_at==='2026-10-04T20:00')))")),{"1":1,"2":2,"3":2});
  assert.deepEqual(plain(run("Object.fromEntries(stats(data.matches.filter((m)=>m.played_at==='2026-10-03T20:00')).filter((r)=>r.matches).map((r)=>[r.id,r.wins]))")),{"1":1,"2":1,"3":0});
  for(const check of technical.checks) {
    context.chosenIds=check.competition_ids; context.chosenPlayer=check.player_id;
    const calculated=plain(run("technicalStats(data.matches.filter((m)=>!chosenIds.length || chosenIds.includes(m.competition_id)),chosenPlayer)"));
    for(const key of ["players","opponents"]) {
      assert.equal(calculated[key].length,check.expected[key].length);
      check.expected[key].forEach((record,i)=>{for(const field of Object.keys(record)) assert.equal(calculated[key][i][field],record[field]);});
    }
  }
  run("render(); switchView('technical');");
  assert.equal(elements.get("technical-view").classList.contains("hidden"),false);
  assert(elements.get("technical-body").innerHTML.includes("未交手"));
  elements.get("technical-player").value="1"; run("renderOpponents()");
  assert(!elements.get("opponent-body").innerHTML.includes("未交手"));
  run("openCompetitionSelection(); draftCompetitionIds=new Set([2]); applyCompetitionSelection({preventDefault(){}});");
  assert.equal(run("filteredMatches().length"),3);
  assert.deepEqual(plain(run("scopeQuery().getAll('competition')")),["2"]);
  run("openMatch(null,2)");
  assert.equal(elements.get("match-competition").value,"2");
  elements.get("match-dialog").close();
  run("selectedMatchIds.add(2); openMove()");
  elements.get("move-competition").value="1";
  await run("saveMove({preventDefault(){}})");
  assert.equal(context.requests.at(-1).url,"/api/matches/bulk-move");
  assert.equal(context.requests.at(-1).body.competition_id,1);
  run("openImport()");
  assert.equal(elements.get("import-mode").value,"skip");
  assert.equal(elements.get("commit-import").disabled,true);
  assert.equal(elements.get("import-competition").value,"2");
  context.importFixture={fingerprint:"preview-fingerprint",total:2,created:2,updated:0,skipped:0,new_players:[],new_competitions:[],
    rounds:technical.state.matches.map((m)=>({competition:m.competition_name,played_at:m.played_at,action:"新增",players:m.entries.map((e)=>({name:e.name,total_units:e.total_units}))}))};
  run("fileContent=async()=> 'ZmlsZQ=='; api=async(url,options)=>{requests.push({url,body:JSON.parse(options.body)});return {ok:true,import:importFixture};};");
  elements.get("import-file").files=[{name:"rounds.xlsx",size:300}];
  await run("previewImport({preventDefault(){}})");
  assert.equal(elements.get("commit-import").disabled,false);
  assert(elements.get("import-preview").innerHTML.includes("选手 A"));
  assert.equal(context.requests.at(-1).body.competition_id,"2");
  elements.get("import-mode").value="overwrite";
  elements.get("import-mode").listeners.change[0]();
  assert.equal(elements.get("commit-import").disabled,true);
  await run("previewImport({preventDefault(){}})");
  await run("commitImport()");
  assert.equal(context.requests.at(-1).url,"/api/import");
  assert.equal(context.requests.at(-1).body.mode,"overwrite");
  assert.equal(context.requests.at(-1).body.fingerprint,"preview-fingerprint");
  assert.equal(elements.get("import-dialog").open,false);
  run("selectedCompetitionIds=new Set();");
  assert.equal(run("filteredMatches().length"),0);
  assert.equal(run("scopeQuery().get('none')"),"1");

  // Reproduce the three Season 13 records, then switch to a different season.
  const season13=[graphMatch(1,[1,2,3],[83,-55,-28]),graphMatch(2,[4,5,6],[175,-90,-85]),graphMatch(3,[5,1,3],[72,-131,59])];
  season13.forEach((m,i)=>{m.competition_id=1;m.entries.forEach((e)=>{e.battle_count=[8,6,2][i];});});
  const otherSeason=graphMatch(4,[1,4,6],[100,-50,-50]); otherSeason.competition_id=2;
  otherSeason.entries.forEach((e)=>{e.battle_count=6;});
  setData([...season13,otherSeason]); elements.get("period-select").value="all";
  elements.get("technical-player").value="1";
  run("openCompetitionSelection(); draftCompetitionIds=new Set([1]); applyCompetitionSelection({preventDefault(){}})");
  const season13Opponents=plain(run("technicalStats(filteredMatches(),1).opponents"));
  assert.deepEqual(season13Opponents.map((r)=>[r.id,r.matches,r.sessions,r.wins,r.draws,r.losses,r.win_rate,r.difference_units]),
    [[3,10,2,1,0,1,0.5,-310000],[2,8,1,1,0,0,1,550000],[5,2,1,0,0,1,0,-720000]]);
  assert.equal((elements.get("opponent-body").innerHTML.match(/<tr>/g)||[]).length,3);
  assert(!elements.get("opponent-body").innerHTML.includes("选手 D"));
  assert(!elements.get("opponent-body").innerHTML.includes("选手 F"));
  run("openCompetitionSelection(); draftCompetitionIds=new Set([2]); applyCompetitionSelection({preventDefault(){}})");
  assert.deepEqual(plain(run("technicalStats(filteredMatches(),1).opponents.map((r)=>[r.id,r.matches,r.wins,r.draws,r.losses,r.win_rate])")),
    [[4,6,1,0,0,1],[6,6,1,0,0,1]]);
  assert.deepEqual(plain(run("technicalStats(filteredMatches(),4).opponents.map((r)=>[r.id,r.matches,r.wins,r.draws,r.losses])")),
    [[1,6,0,0,1],[6,6,0,1,0]]);
  run("download=async(url)=>requests.push({url});");
  await run("exportTechnical()");
  const technicalUrl=new URL(context.requests.at(-1).url,"http://scoreboard.test");
  assert.deepEqual(technicalUrl.searchParams.getAll("competition"),["2"]);
  assert.equal(technicalUrl.searchParams.get("player"),"1");
  assert(!fs.readFileSync(path.join(root,"static/index.html"),"utf8").includes("场均分差"));
  elements.get("technical-player").value="2"; run("renderOpponents()");
  assert(elements.get("opponent-body").innerHTML.includes('colspan="6"'));
  assert(elements.get("opponent-body").innerHTML.includes("暂无交手记录"));

  const aggregateHistory=graphMatch(1,[1,2,3],[20,-10,-10]);
  aggregateHistory.entries.forEach((e)=>{e.battle_count=0;});
  const report=graphMatch(2,[1,2,3],[0,0,0]); report.scoring_mode="multi"; report.record_kind="counts";
  report.entries.forEach((e,i)=>{e.battle_count=[63,126,36][i];});
  setData([aggregateHistory,report]);
  let counted=plain(run("technicalStats(data.matches,1)"));
  const a=counted.players.find((r)=>r.id===1);
  assert.equal(a.matches,63);
  assert.equal(a.total_units,200000);
  assert.equal(a.average_score_units,200000/63);
  assert.equal(a.average_rank,null);
  assert.equal(a.ranked_matches,0);
  assert(counted.opponents.every((r)=>r.matches===0));
  assert.equal(counted.opponents.length,2);
  assert(counted.opponents.every((r)=>r.wins===1 && r.draws===0 && r.win_rate===1));
  assert.equal(run("stats(data.matches).find((r)=>r.id===1).wins"),0);
  assert.equal(run("participationSeries(data.matches,[data.players[0]])[0].actualMatches"),1);
  assert.deepEqual(plain(run("chartRange(participationSeries(data.matches,data.players))")),{lo:-100000,hi:200000});
  run("render(); openMatch(2)");
  assert.equal(elements.get("multi-kind").value,"counts");
  assert.equal(elements.get("multi-count-0").value,"63");
  assert(elements.get("technical-body").innerHTML.includes("63"));
  context.fixture.matches.push(graphMatch(3,[1,2,3],[30,-10,-20]));
  counted=plain(run("technicalStats(data.matches,1)"));
  const later=counted.players.find((r)=>r.id===1);
  assert.equal(later.matches,64);
  assert.equal(later.total_units,500000);
  assert.equal(later.average_rank,1);
  assert.equal(counted.opponents.find((r)=>r.id===2).matches,1);
  assert.equal(counted.opponents.find((r)=>r.id===2).wins,2);
  if(process.env.SCOREBOARD_VERIFY_STATE) {
    const actual=JSON.parse(fs.readFileSync(process.env.SCOREBOARD_VERIFY_STATE,"utf8"));
    context.fixture=actual.state;run("data=fixture; selectedCompetitionIds=null;");
    for(const check of actual.checks) {
      context.chosenIds=check.competition_ids;context.chosenPlayer=check.player_id;
      const calculated=plain(run("technicalStats(data.matches.filter((m)=>!chosenIds.length || chosenIds.includes(m.competition_id)),chosenPlayer)"));
      for(const key of ["players","opponents"]) {
        assert.equal(calculated[key].length,check.expected[key].length);
        check.expected[key].forEach((record,i)=>{for(const field of Object.keys(record))assert.equal(calculated[key][i][field],record[field]);});
      }
    }
    run("render(); renderCompetitions(); renderHistory(data.matches)");
    console.log(`Verified ${actual.state.matches.length} real records across all imported seasons and ${actual.checks.length} statistic scopes.`);
  }
  console.log(`Passed ${cases.length+multiFixture.cases.length} scoring comparisons, ${technical.checks.length+multiFixture.checks.length} technical-statistic comparisons, ${flowCases.length} balanced-flow comparisons, multi-player entry and zero-sum checks, dialogs, competition scope, moves, import setup, charts, PNG export and bulk selection.`);
})().catch((error)=>{console.error(error);process.exitCode=1;});
