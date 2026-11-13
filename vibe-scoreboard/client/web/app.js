"use strict";
const $=(id)=>document.getElementById(id);
let token="", catalog=null, busy=false, pending=null, submitted=false, nextKey=crypto.randomUUID(), rows=[];
let configuredURL="";
const esc=(value)=>String(value??"").replace(/[&<>"']/g,(ch)=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
function now() {
  const date=new Date(), pad=(n)=>String(n).padStart(2,"0");
  return date.getFullYear()+"-"+pad(date.getMonth()+1)+"-"+pad(date.getDate())+"T"+pad(date.getHours())+":"+pad(date.getMinutes());
}
async function request(path,payload) {
  const response=await fetch(path,{method:payload===undefined?"GET":"POST",
    headers:payload===undefined?{}:{"Content-Type":"application/json","X-Client-Token":token},
    ...(payload===undefined?{}:{body:JSON.stringify(payload)})});
  const body=await response.json();
  if(!response.ok) {
    const error=new Error(body.error||"请求失败, 请重试");
    error.pending=body.pending; error.status=response.status; throw error;
  }
  return body;
}
function errorMessage(message) {
  $("entry-error").textContent=message||"";
  $("entry-error").classList.toggle("hidden",!message);
}
function readRows() {
  return rows.map((row,i)=>({player_id:Number($("player-"+i).value),base:$("base-"+i).value,battle_count:$("count-"+i).value}));
}
function draft() {
  return {server_id:catalog?.server_id,competition_id:Number($("season").value),played_at:$("played-at").value,
    rate:pending?.payload?.rate??"1",note:$("note").value,request_key:nextKey,entries:readRows()};
}
function renderRows() {
  const players=catalog?.players||[];
  $("players").innerHTML=rows.map((row,i)=>{
    let options='<option value="">请选择选手</option>';
    options+=players.map((player)=>'<option value="'+player.id+'"'+(player.id===Number(row.player_id)?' selected':'')+'>'+esc(player.name)+'</option>').join("");
    if(row.player_id && !players.some((player)=>player.id===Number(row.player_id))) options+='<option value="'+Number(row.player_id)+'" selected>选手 #'+Number(row.player_id)+' (已停用或不存在)</option>';
    return '<tr><td class="seat">'+(i+1)+'</td><td><select id="player-'+i+'" aria-label="第 '+(i+1)+' 位选手">'+options+'</select></td>'+
      '<td><input id="count-'+i+'" type="text" inputmode="numeric" value="'+esc(row.battle_count??1)+'" aria-label="第 '+(i+1)+' 位选手的半庄数"></td>'+
      '<td><input id="base-'+i+'" type="text" inputmode="decimal" value="'+esc(row.base??"")+'" placeholder="例如: +100" aria-label="第 '+(i+1)+' 位选手的分数"></td>'+
      '<td class="player-total" id="total-'+i+'">—</td><td><button class="remove-player" type="button" data-remove="'+i+'" aria-label="移除第 '+(i+1)+' 位选手">×</button></td></tr>';
  }).join("");
  update();
}
function update() {
  const connected=Boolean(catalog), locked=busy||Boolean(pending)||submitted;
  $("entry-fields").disabled=!connected||locked;
  $("connect").disabled=busy;
  $("server-url").disabled=busy||Boolean(pending);
  $("access-code").disabled=busy;
  $("quit").disabled=busy;
  $("new-round").disabled=busy;
  $("new-round").classList.toggle("hidden",!submitted);
  $("connect").textContent=busy?"处理中...":connected?"刷新名单":"连接";
  const validation=EntryForm.validate(draft(),new Set((catalog?.players||[]).map((player)=>player.id)));
  $("submit").disabled=busy||!connected||submitted||(!pending&&!validation.valid);
  $("submit").textContent=busy?"正在处理...":pending?"重试上次提交":"提交本场成绩";
  $("roster-count").textContent=connected?rows.length+" 位选手":"等待连接";
  const season=catalog?.competitions.find((competition)=>competition.id===Number($("season").value));
  $("season-note").textContent=season?.note||"";
  $("balance").className="balance"+(connected?(validation.valid?" valid":" invalid"):"");
  $("balance-title").textContent=!connected?"等待连接服务器":validation.valid?"分数已平衡, 可以提交":validation.error;
  $("balance-detail").textContent=pending?"上次提交结果未确认. 原内容已保留, 可安全重试.":"倍率固定为 1, 半庄数单独计入统计.";
  $("balance-value").textContent=validation.balance===null?"—":EntryForm.format(validation.balance);
  const payload=draft(), ids=payload.entries.map((entry)=>entry.player_id), rate=EntryForm.cents(payload.rate);
  payload.entries.forEach((entry,i)=>{
    const base=EntryForm.cents(entry.base), value=base!==null&&rate!==null?base*rate:null;
    $("total-"+i).textContent=value===null?"—":EntryForm.format(value,10000);
    $("total-"+i).className="player-total"+(value>0?" positive":value<0?" negative":"");
    for(const option of $("player-"+i).options) option.disabled=Number(option.value)>0&&Number(option.value)!==entry.player_id&&ids.includes(Number(option.value));
  });
  $("add-player").disabled=locked||rows.length>=200||rows.length>=(catalog?.players.length||0);
}
function applyCatalog(value, preserve=true) {
  const oldSeason=$("season").value, hadCatalog=Boolean(catalog);
  if(rows.length) rows=readRows();
  catalog=value;
  $("season").innerHTML='<option value="">请选择赛季</option>'+value.competitions.map((season)=>'<option value="'+season.id+'">'+esc(season.name)+'</option>').join("");
  if(preserve && oldSeason && value.competitions.some((season)=>String(season.id)===oldSeason)) $("season").value=oldSeason;
  else if(!pending && value.competitions.length===1) $("season").value=String(value.competitions[0].id);
  if(!preserve || (!rows.length&&!hadCatalog)) rows=value.players.slice(0,3).map((player)=>({player_id:player.id,base:"",battle_count:"1"}));
  if(!$("played-at").value) $("played-at").value=value.server_time||now();
  $("connection-badge").textContent="已连接"; $("connection-badge").className="badge online";
  $("connection-status").textContent="已连接 "+value.server_name+". 可用赛季 "+value.competitions.length+" 个, 选手 "+value.players.length+" 位.";
  $("connection-status").className="helper";
  renderRows();
  if(pending) restorePending();
}
function restorePending() {
  const value=pending.payload;
  if(!value) return;
  nextKey=value.request_key;
  $("season").value=String(value.competition_id);
  if($("season").value!==String(value.competition_id)) {
    const option=document.createElement("option"); option.value=String(value.competition_id); option.textContent="原赛季 #"+value.competition_id+" (当前名单中不存在)";
    $("season").append(option); $("season").value=option.value;
  }
  $("played-at").value=value.played_at; $("note").value=value.note||"";
  rows=value.entries; renderRows();
  errorMessage("上次提交结果尚未确认. 连接原服务器后点击重试, 不会重复录入.");
}
async function loadConfig() {
  const config=await request("/api/config");
  token=config.token; configuredURL=config.server_url||""; $("server-url").value=configuredURL; pending=config.pending;
  if(config.connected && config.catalog) applyCatalog(config.catalog);
  else if(pending) {
    $("server-url").value=pending.server_url;
    errorMessage("发现上次尚未确认的提交. 请填写录分码, 连接原服务器后重试.");
  }
  update();
  return config;
}
$("connection-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if(busy) return;
  busy=true; update();
  const requested=$("server-url").value.trim();
  try {
    const previousServer=catalog?.server_id;
    const value=await request("/api/connect",{server_url:requested,access_code:$("access-code").value});
    if(previousServer && previousServer!==value.server_id && !pending) { rows=[]; submitted=false; $("receipt").classList.add("hidden"); }
    configuredURL=requested; applyCatalog(value,!previousServer||previousServer===value.server_id);
    if(!pending) errorMessage("");
  } catch(error) {
    catalog=null;
    $("connection-status").textContent=error.message||"连接失败, 请确认服务器正在运行.";
    $("connection-status").className="helper problem";
    $("connection-badge").textContent="连接失败"; $("connection-badge").className="badge";
  } finally { busy=false; update(); }
});
$("entry-fields").addEventListener("input",()=>{if(!pending){nextKey=crypto.randomUUID();errorMessage("");update();}});
$("entry-fields").addEventListener("change",()=>{if(!pending){nextKey=crypto.randomUUID();errorMessage("");update();}});
$("add-player").addEventListener("click",()=>{
  if(busy||pending||submitted||!catalog) return;
  rows=readRows(); const used=new Set(rows.map((row)=>row.player_id)), available=catalog.players.find((player)=>!used.has(player.id));
  if(available && rows.length<200) { rows.push({player_id:available.id,base:"",battle_count:"1"}); nextKey=crypto.randomUUID();renderRows(); }
});
$("players").addEventListener("click",(event)=>{
  const button=event.target.closest("[data-remove]"); if(!button||busy||pending||submitted) return;
  rows=readRows(); rows.splice(Number(button.dataset.remove),1); nextKey=crypto.randomUUID();renderRows();
});
$("entry-form").addEventListener("submit",async(event)=>{
  event.preventDefault(); if(busy||submitted||!catalog) return;
  const payload=pending?pending.payload:draft();
  if(!pending) {
    const validation=EntryForm.validate(payload,new Set(catalog.players.map((player)=>player.id)));
    if(!validation.valid) {errorMessage(validation.error); return;}
    if(!$("played-at").validity.valid) {errorMessage("请填写有效的对局时间"); return;}
  }
  busy=true;errorMessage("");update();
  try {
    const result=await request("/api/submit",payload);
    pending=null;submitted=true;
    const season=catalog.competitions.find((item)=>item.id===payload.competition_id);
    $("receipt").textContent="提交成功 · "+(season?.name||"赛季 #"+payload.competition_id)+"\n记录 #"+result.id+" 已写入服务器数据库, 共 "+result.participants+" 位选手.";
    $("receipt").classList.remove("hidden");
    $("submission-note").textContent="这场已提交. 点击新建下一场继续录分.";
  } catch(error) {
    if(error.pending) pending={server_url:configuredURL,payload};
    else if(error.pending===false) pending=null;
    else {
      try {const config=await request("/api/config");pending=config.pending;}
      catch {pending={server_url:configuredURL,payload};}
    }
    errorMessage((error.message||"客户端连接中断, 请重新打开 exe.")+(pending?"\n本次内容已保留, 请重试原提交, 不会重复录入.":""));
  } finally {busy=false;update();}
});
$("new-round").addEventListener("click",()=>{
  if(busy||pending) return;
  rows=readRows().map((row)=>({...row,base:"",battle_count:"1"}));
  submitted=false;nextKey=crypto.randomUUID();$("played-at").value=now();$("note").value="";
  $("receipt").classList.add("hidden");$("submission-note").textContent="提交成功后, 成绩会直接计入所选赛季.";errorMessage("");renderRows();
});
$("quit").addEventListener("click",async()=>{
  if(busy) return;
  if(!submitted&&!pending&&rows.length&&readRows().some((row)=>row.base.trim())&&!confirm("当前成绩尚未提交, 确定退出客户端?")) return;
  try {await request("/api/quit",{});document.body.textContent="客户端已退出. 可以关闭此页面, 下次双击 exe 即可重新打开.";}
  catch(error){errorMessage(error.message||"无法退出, 请关闭客户端进程.");}
});
loadConfig().catch((error)=>errorMessage(error.message||"客户端连接失败, 请重新打开 exe."));
