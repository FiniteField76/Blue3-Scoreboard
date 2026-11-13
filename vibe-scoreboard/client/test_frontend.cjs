"use strict";
// npm install, then npm test. jsdom is a development dependency only.
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const {JSDOM}=require("jsdom");
const Form=require("./web/form.js");
const catalog={ok:true,api_version:1,server_id:"server-001",server_name:"Blue3 Scoreboard",server_time:"2026-11-13T20:00",rate:"1",
  capabilities:{create_players:true,create_competitions:true},
  competitions:[{id:13,name:"Season 13",note:"2026 4th quarter"},{id:14,name:"Season 14",note:""}],
  players:[{id:1,name:"蓝白碗"},{id:2,name:"admin"},{id:3,name:"fox"},{id:4,name:"8g"}]};
const plain=(value)=>JSON.parse(JSON.stringify(value));
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
const baseDraft={competition_id:13,played_at:"2026-11-13T20:00",rate:"1",
  entries:[{player_id:1,base:"0.1",battle_count:"8"},{player_id:2,base:"0.2",battle_count:"8"},{player_id:3,base:"-0.3",battle_count:"6"}]};
assert.equal(Form.validate(baseDraft).valid,true);
for(const value of ["1.5","-1","","1000001","NaN"]) assert.equal(Form.count(value),null);
for(const value of ["1.001","NaN","Infinity","1e2","1000000.01",""]) assert.equal(Form.cents(value),null);
assert.equal(Form.validate({...baseDraft,rate:"1.25"}).totals[0],1250);
assert.equal(Form.validate({...baseDraft,entries:[...baseDraft.entries.slice(0,2),{player_id:3,base:"-0.29",battle_count:"1"}]}).balance,1);
assert.equal(Form.validate({...baseDraft,entries:[baseDraft.entries[0],baseDraft.entries[0]]}).valid,false);

async function setup(initial={}) {
  const dom=new JSDOM(fs.readFileSync(path.join(__dirname,"web/index.html"),"utf8"),{url:"http://127.0.0.1:18976",runScripts:"outside-only"});
  const {window}=dom, calls=[];
  const state={pending:null,connected:false,failConnect:false,loseReply:false,catalog:plain(catalog),...initial};
  window.confirm=()=>true;
  window.fetch=async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;
    calls.push({url,body});
    let status=200,result;
    if(url==="/api/config") result={token:"local-token",server_url:"http://server:8766",connected:state.connected,catalog:state.connected?state.catalog:null,pending:state.pending};
    else if(url==="/api/connect") {
      if(state.failConnect){status=502;result={error:"连接失败",pending:Boolean(state.pending)};}
      else {state.connected=true;result=state.catalog;}
    } else if(url==="/api/submit") {
      if(state.loseReply){status=502;state.pending={server_url:"http://server:8766",payload:body};result={error:"响应中断",pending:true};}
      else {status=201;state.pending=null;result={ok:true,id:42,participants:body.entries.length};}
    } else result={ok:true};
    return {ok:status>=200&&status<300,status,json:async()=>plain(result)};
  };
  for(const file of ["form.js","app.js"])vm.runInContext(fs.readFileSync(path.join(__dirname,"web",file),"utf8"),dom.getInternalVMContext(),{filename:file});
  const get=(id)=>window.document.getElementById(id);
  const fill=(id,value)=>{get(id).value=value;get(id).dispatchEvent(new window.Event("input",{bubbles:true}));};
  const submit=(id)=>get(id).dispatchEvent(new window.Event("submit",{bubbles:true,cancelable:true}));
  await flush();
  return {dom,get,fill,submit,calls,state};
}
async function connect(ui) {ui.fill("access-code","entry-code");ui.submit("connection-form");await flush();}
function scores(ui) {
  ui.fill("season","13");
  if(!ui.get("base-3"))ui.get("add-player").click();
  ["100","-50","-30","-20"].forEach((value,i)=>ui.fill("base-"+i,value));
  ["8","8","6","6"].forEach((value,i)=>ui.fill("count-"+i,value));
}
(async()=>{
  const ui=await setup();
  assert.equal(ui.get("entry-fields").disabled,true);
  await connect(ui);
  assert.equal(ui.get("entry-fields").disabled,false);
  assert.equal(ui.get("rate"),null);
  scores(ui);
  assert.equal(ui.get("submit").disabled,false);
  ui.fill("base-0","99.99");
  assert.equal(ui.get("submit").disabled,true);
  assert.equal(ui.get("balance-value").textContent,"-0.01");
  ui.fill("base-0","100");
  ui.submit("entry-form");await flush();
  const payload=ui.calls.find((call)=>call.url==="/api/submit").body;
  assert.equal(payload.competition_id,13);assert.equal(payload.rate,"1");
  assert.deepEqual(payload.entries.map((entry)=>entry.battle_count),["8","8","6","6"]);
  assert(ui.get("receipt").textContent.includes("#42"));
  assert.equal(ui.get("entry-fields").disabled,true);
  ui.submit("entry-form");await flush();
  assert.equal(ui.calls.filter((call)=>call.url==="/api/submit").length,1);
  ui.get("new-round").click();
  assert.equal(ui.get("count-0").value,"1");assert.equal(ui.get("base-0").value,"");assert.equal(ui.get("rate"),null);
  scores(ui);ui.state.loseReply=true;ui.submit("entry-form");await flush();
  assert.equal(ui.get("entry-fields").disabled,true);
  assert.equal(ui.get("submit").textContent,"重试上次提交");
  assert.equal(ui.get("submit").disabled,false);
  const pending=plain(ui.state.pending);
  ui.state.loseReply=false;ui.submit("entry-form");await flush();
  const attempts=ui.calls.filter((call)=>call.url==="/api/submit");
  assert.deepEqual(attempts.at(-1).body,attempts.at(-2).body);
  const restored=await setup({pending});
  assert.equal(restored.get("server-url").disabled,true);
  await connect(restored);
  assert.equal(restored.get("base-0").value,"100");assert.equal(restored.get("count-0").value,"8");
  assert.equal(restored.get("season").value,"13");assert.equal(restored.get("submit").textContent,"重试上次提交");
  restored.submit("entry-form");await flush();
  assert.deepEqual(restored.calls.find((call)=>call.url==="/api/submit").body,pending.payload);
  ui.get("new-round").click();scores(ui);
  ui.state.failConnect=true;ui.fill("server-url","http://other-server:8766");ui.submit("connection-form");await flush();
  assert.equal(ui.get("entry-fields").disabled,true);assert.equal(ui.get("submit").disabled,true);
  assert.equal(ui.get("connection-badge").textContent,"连接失败");
  for(const id of ["catalog-title","create-season-form","create-player-form","new-season-name","new-player-name"])assert.equal(ui.get(id),null);
  assert(ui.get("client-version").textContent.includes("1.3.0"));
  const oldPending=plain(pending);oldPending.payload.rate="2";
  const legacy=await setup({pending:oldPending});await connect(legacy);
  assert.equal(legacy.get("rate"),null);assert.equal(legacy.get("total-0").textContent,"+200");
  legacy.submit("entry-form");await flush();
  assert.deepEqual(legacy.calls.find((call)=>call.url==="/api/submit").body,oldPending.payload);
  legacy.get("new-round").click();scores(legacy);legacy.submit("entry-form");await flush();
  assert.equal(legacy.calls.filter((call)=>call.url==="/api/submit").at(-1).body.rate,"1");
  const changedDefault=await setup({catalog:{...plain(catalog),rate:"10"}});await connect(changedDefault);scores(changedDefault);
  changedDefault.submit("entry-form");await flush();assert.equal(changedDefault.calls.find((call)=>call.url==="/api/submit").body.rate,"1");
  for(const item of [ui,restored,legacy,changedDefault]) {
    assert(!item.calls.some((call)=>call.url.startsWith("/api/catalog/")));item.dom.window.close();
  }
  console.log("Passed fixed-rate submission, removed creation controls, legacy retry preservation, real-DOM entry, restart recovery and connection checks.");
})().catch((error)=>{console.error(error);process.exitCode=1;});
