"use strict";

const $ = (id) => document.getElementById(id);
const icon = (name, extra = "") => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let data = { players: [], competitions: [], matches: [], settings: { rate: "1" }, token: "" };
let view = "overview", page = 0, editingMatch = null, editingPlayer = null;
let requestKey = "", saving = false, loadingPromise = null, lastSignature = "";
const exportExcludedIds = new Set();
const selectedMatchIds = new Set(), selectedPlayerIds = new Set();
let knockoutAwards = {}, knockoutPending = null, knockoutSavePending = false, confirmBusy = false;
let chartSelectedIds = null, chartExporting = false;
let draftChartIds = new Set();
let selectedCompetitionIds = null, draftCompetitionIds = new Set(), editingCompetition = null;
let moveSelection = [], importUpload = null, importPreview = null, importBusy = false, competitionBusy = false, moveBusy = false;
let multiRows = [], multiEditing = null, multiRequestKey = "", multiSaving = false, multiReading = false;
const PAGE_SIZE = 12;
const SCORE_SCALE = 10000;

function score(units, signed = true) {
  const value = (Math.abs(units) / SCORE_SCALE).toLocaleString("en-US", { maximumFractionDigits: 4 });
  return (units < 0 ? "-" : units > 0 && signed ? "+" : "") + value;
}
function parseCents(value) {
  const text = String(value).trim();
  if (text.length > 32 || !/^[+-]?(?:\d+(?:\.\d{1,2})?|\.\d{1,2})$/.test(text)) return null;
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace(/^[+-]/, "").split(".");
  const result = Number(whole || "0") * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(result) || result > 100000000) return null;
  return negative ? -result : result;
}
function plainScore(cents) {
  const abs = Math.abs(cents);
  const remainder = abs % 100;
  return `${cents < 0 ? "-" : ""}${Math.floor(abs / 100)}${remainder ? "." + String(remainder).padStart(2, "0").replace(/0$/, "") : ""}`;
}
function parseInteger(value) {
  const text = String(value).trim();
  if (!/^[+-]?\d{1,7}$/.test(text)) return null;
  const number = Number(text);
  return Number.isSafeInteger(number) && Math.abs(number) <= 1000000 ? number : null;
}
function calculateMatch(payload) {
  const entries = payload.entries, ids = entries.map((e) => Number(e.player_id));
  const fail = (error) => ({ error });
  const multiMode = payload.scoring_mode === "multi";
  if ((multiMode ? ids.length < 1 || ids.length > 200 : ids.length !== 3) || new Set(ids).size !== ids.length || ids.some((id) => !Number.isInteger(id) || id <= 0)) return fail(multiMode ? "请选择 1 到 200 位不同的选手." : "请选择三位不同的选手.");
  const counts = entries.map((e) => parseInteger(e.battle_count ?? 1));
  if (counts.some((n) => n === null || n < 0)) return fail("对战数量须为 0 到 1,000,000 的整数. 默认 1, 历史成绩已汇总数量时填 0.");
  const kind = payload.record_kind ?? "score";
  if (!["score","counts"].includes(kind) || (kind === "counts" && !multiMode)) return fail("数量汇总记录须使用多人模式.");
  const rate = parseCents(payload.rate);
  if (rate === null || rate <= 0 || rate > 1000000) return fail("rate 必须大于 0 且不超过 10,000, 最多两位小数.");
  const extras = multiMode ? entries.map(() => 0) : entries.map((e) => parseCents(e.extra));
  if (extras.some((n) => n === null)) return fail("额外分须为数字, 最多两位小数; 没有额外分请填 0.");
  let bases, order = [], placements = [0,0,0], chipScores = [0,0,0], knockoutScores = [0,0,0], missingAwards = [];
  if (payload.scoring_mode === "manual" || multiMode) {
    bases = entries.map((e) => parseCents(e.base));
    if (bases.some((n) => n === null)) return fail(multiMode ? "请填写每位选手的基础分, 最多两位小数." : "请填写三人的基础分, 最多两位小数.");
    const balance = bases.reduce((a,b) => a+b, 0);
    if (balance !== 0) return fail(multiMode ? `所有选手的基础分合计必须为 0, 当前为 ${plainScore(balance)}.` : "三人的基础分之和必须为 0, 额外分不参与校验.");
  } else {
    const points = entries.map((e) => parseInteger(e.points)), chips = entries.map((e) => parseInteger(e.chip));
    if (points.some((n) => n === null)) return fail("请填写三人的整数点数, 范围为 -1,000,000 到 1,000,000.");
    if (chips.some((n) => n === null)) return fail("chip 必须是整数, 范围为 -1,000,000 到 1,000,000.");
    if (chips.reduce((a,b) => a+b,0) !== 0) return fail("三人的 chip 之和必须为 0.");
    if (payload.rank_order) {
      order = payload.rank_order.map(Number);
      if (order.length !== 3 || new Set(order).size !== 3 || order.some((id) => !ids.includes(id))) return fail("请指定同分选手的先后名次, 每个名次只能选择一次.");
    } else {
      order = [...ids].sort((a,b) => points[ids.indexOf(b)]-points[ids.indexOf(a)]);
      const orderedPoints = order.map((id) => points[ids.indexOf(id)]);
      const topTie = orderedPoints[0] === orderedPoints[1] && orderedPoints[1] > orderedPoints[2];
      if (new Set(points).size !== 3 && !topTie) return fail("点数相同, 请指定第二、三名或三人同分时的计分先后.");
    }
    const [first, second, third] = order.map((id) => ids.indexOf(id));
    if (points[first] < points[second] || points[second] < points[third]) return fail("名次须按点数从高到低排列, 只可调整同分者的先后.");
    placements[third] = (points[third]-500)*10;
    if (points[first] === points[second] && points[second] > points[third]) {
      placements[first] = placements[second] = -placements[third]/2;
    } else {
      placements[second] = (points[second]-(points[second]<400 ? 500 : 400))*10;
      placements[first] = -placements[second]-placements[third];
    }
    chipScores = chips.map((n) => n*300);
    entries.forEach((entry,i) => {
      if (points[i] !== 0) return;
      const recipient = Number(payload.knockouts?.[entry.player_id]);
      if (!ids.includes(recipient) || recipient === ids[i]) { missingAwards.push(ids[i]); return; }
      knockoutScores[i] -= 1000; knockoutScores[ids.indexOf(recipient)] += 1000;
    });
    bases = placements.map((n,i) => n+chipScores[i]+knockoutScores[i]);
  }
  const baseUnits = bases.map((n) => n*rate), extraUnits = extras.map((n) => n*rate);
  const totals = baseUnits.map((n,i) => n+extraUnits[i]);
  if (kind === "counts" && bases.some((n) => n !== 0)) return fail("对战数量汇总只记录场数, 基础分必须为 0.");
  if ([...baseUnits,...extraUnits,...totals].some((n) => !Number.isSafeInteger(n) || Math.abs(n)>1000000*SCORE_SCALE)) return fail("乘以 rate 后的分数超过范围, 每项及当场总分不得超过 1,000,000.");
  return { bases, extras, baseUnits, extraUnits, totals, order, placements, chipScores, knockoutScores, missingAwards, rate };
}
function scoreClass(cents) { return cents > 0 ? "score-positive" : cents < 0 ? "score-negative" : "score-zero"; }
function avatar(player, extra = "") {
  const color = player?.color || "#5878f4";
  const letter = Array.from(player?.name || "?")[0];
  return `<span class="avatar ${extra}" style="--avatar-color:${color};--avatar-bg:${color}16" aria-hidden="true">${esc(letter)}</span>`;
}
function playerById(id) { return data.players.find((p) => p.id === Number(id)); }
function localDate(date = new Date()) {
  const z = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${z(date.getMonth() + 1)}-${z(date.getDate())}`;
}
function localDateTime() {
  const date = new Date();
  return `${localDate(date)}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
function dateRange() {
  const period = $("period-select").value;
  if (period === "all") return { start: "", end: "", label: "全部时间" };
  if (period === "custom") return { start: $("start-date").value, end: $("end-date").value, label: "自定义日期" };
  const now = new Date(), start = new Date(now);
  start.setDate(now.getDate() - (period === "today" ? 0 : Number(period) - 1));
  return { start: localDate(start), end: localDate(now), label: period === "today" ? "今天" : `最近 ${period} 天` };
}
function filteredMatches() {
  const { start, end } = dateRange();
  if (start && end && start > end) return [];
  return data.matches.filter((m) => (selectedCompetitionIds === null || selectedCompetitionIds.has(m.competition_id)) && (!start || m.played_at.slice(0, 10) >= start) && (!end || m.played_at.slice(0, 10) <= end));
}
function competitionById(id) { return (data.competitions || []).find((c) => c.id === Number(id)); }
function scopeLabel() {
  if (selectedCompetitionIds === null) return "全部比赛";
  const selected = (data.competitions || []).filter((c) => selectedCompetitionIds.has(c.id));
  return selected.length === 1 ? selected[0].name : `已选 ${selected.length} 场比赛`;
}
function competitionOptions(selected = null, allOption = "") {
  return (allOption ? `<option value="">${esc(allOption)}</option>` : "") + (data.competitions || []).map((c) => `<option value="${c.id}"${c.id === Number(selected) ? " selected" : ""}>${esc(c.name)}</option>`).join("");
}
function matchRanks(match) {
  if (match.record_kind === "counts") return new Map(match.entries.map((e) => [e.player_id,null]));
  if (match.scoring_mode === "points") return new Map(match.entries.map((e) => [e.player_id, 1+match.entries.filter((other) => other.points > e.points).length]));
  return new Map(match.entries.map((e) => [e.player_id, 1+match.entries.filter((other) => other.total_units > e.total_units).length]));
}
function singleResult(match) {
  return match.record_kind !== "counts" && match.entries.every((e) => (e.battle_count ?? 1) === 1);
}
function proportionalIntegers(amount, entries) {
  const total = entries.reduce((sum,e)=>sum+BigInt(e.weight),0n);
  const parts = entries.map((e)=>{const product=BigInt(amount)*BigInt(e.weight);return {id:e.id,amount:Number(product/total),remainder:Number(product%total)};});
  const missing = amount-parts.reduce((sum,e)=>sum+e.amount,0);
  parts.sort((a,b)=>b.remainder-a.remainder || a.id-b.id);
  parts.slice(0,missing).forEach((e)=>e.amount++);
  return new Map(parts.map((e)=>[e.id,e.amount]));
}
function scoreFlows(match) {
  const result = new Map();
  if (match.record_kind === "counts") return result;
  const positive = match.entries.filter((e)=>e.total_units>0).map((e)=>({id:e.player_id,weight:e.total_units})).sort((a,b)=>a.id-b.id);
  const negative = match.entries.filter((e)=>e.total_units<0).map((e)=>({id:e.player_id,weight:-e.total_units})).sort((a,b)=>a.id-b.id);
  if (!positive.length || !negative.length) return result;
  const pos = positive.reduce((sum,e)=>sum+e.weight,0), neg = negative.reduce((sum,e)=>sum+e.weight,0);
  const budget = Number((BigInt(Math.min(pos,neg))+5000n)/10000n);
  if (!budget) return result;
  const incoming = proportionalIntegers(budget,positive), outgoing = proportionalIntegers(budget,negative);
  const source = positive.length+negative.length, sink = source+1, graph = Array.from({length:sink+1},()=>[]);
  const edge = (a,b,capacity) => {
    const forward={target:b,capacity,reverse:graph[b].length};graph[a].push(forward);
    graph[b].push({target:a,capacity:0,reverse:graph[a].length-1});return forward;
  };
  const cells=[], rowUsed=new Map(positive.map((e)=>[e.id,0])), colUsed=new Map(negative.map((e)=>[e.id,0]));
  positive.forEach((winner,i)=>{
    const portions=[];
    negative.forEach((loser,j)=>{
      const product=BigInt(incoming.get(winner.id))*BigInt(outgoing.get(loser.id)), whole=Number(product/BigInt(budget)), remainder=Number(product%BigInt(budget));
      const cell={winner:winner.id,loser:loser.id,whole,rounding:null};cells.push(cell);
      rowUsed.set(winner.id,rowUsed.get(winner.id)+whole);colUsed.set(loser.id,colUsed.get(loser.id)+whole);
      if(remainder)portions.push({remainder,loser:loser.id,j,cell});
    });
    portions.sort((a,b)=>b.remainder-a.remainder || a.loser-b.loser).forEach((p)=>{p.cell.rounding=edge(i,positive.length+p.j,1);});
  });
  positive.forEach((e,i)=>edge(source,i,incoming.get(e.id)-rowUsed.get(e.id)));
  negative.forEach((e,j)=>edge(positive.length+j,sink,outgoing.get(e.id)-colUsed.get(e.id)));
  while (true) {
    const levels=Array(graph.length).fill(-1), queue=[source];levels[source]=0;
    for(let i=0;i<queue.length;i++)for(const link of graph[queue[i]])if(link.capacity && levels[link.target]<0){levels[link.target]=levels[queue[i]]+1;queue.push(link.target);}
    if(levels[sink]<0)break;
    const cursor=Array(graph.length).fill(0);
    const send=(node,amount)=>{
      if(node===sink)return amount;
      while(cursor[node]<graph[node].length){
        const link=graph[node][cursor[node]];
        if(link.capacity && levels[link.target]===levels[node]+1){const sent=send(link.target,Math.min(amount,link.capacity));if(sent){link.capacity-=sent;graph[link.target][link.reverse].capacity+=sent;return sent;}}
        cursor[node]++;
      }
      return 0;
    };
    while(send(source,budget)){}
  }
  for(const cell of cells){
    const units=(cell.whole+(cell.rounding ? 1-cell.rounding.capacity : 0))*10000;
    if(units){result.set(`${cell.winner}:${cell.loser}`,units);result.set(`${cell.loser}:${cell.winner}`,-units);}
  }
  return result;
}
function technicalStats(matches, playerId = null) {
  const players = new Map(data.players.map((p) => [p.id, { ...p, matches:0, ranked_matches:0, rank_sum:0, total_units:0 }]));
  const opponents = new Map(data.players.filter((p) => p.id !== playerId).map((p) => [p.id, { ...p, matches:0, sessions:0, wins:0, draws:0, losses:0, difference_units:0 }]));
  for (const match of matches) {
    const ranks = matchRanks(match), chosen = match.entries.find((e) => e.player_id === playerId);
    const flows = chosen ? scoreFlows(match) : new Map();
    for (const entry of match.entries) {
      const r = players.get(entry.player_id);
      r.matches += entry.battle_count ?? 1; r.total_units += entry.total_units;
      if (singleResult(match)) { r.ranked_matches++; r.rank_sum += ranks.get(entry.player_id); }
      if (chosen && match.record_kind !== "counts" && entry.player_id !== playerId) {
        const other = opponents.get(entry.player_id), difference = ranks.get(entry.player_id) - ranks.get(playerId);
        other.matches += Math.min(chosen.battle_count ?? 1,entry.battle_count ?? 1);
        other.sessions++;
        other[difference > 0 ? "wins" : difference < 0 ? "losses" : "draws"]++;
        other.difference_units += flows.get(`${playerId}:${entry.player_id}`) || 0;
      }
    }
  }
  for (const r of players.values()) { r.average_rank = r.ranked_matches ? r.rank_sum/r.ranked_matches : null; r.average_score_units = r.matches ? r.total_units/r.matches : null; }
  for (const r of opponents.values()) {
    const count = r.wins+r.draws+r.losses;
    r.win_rate = count ? r.wins/count : null;
  }
  return {players:[...players.values()].sort((a,b) => Number(!a.matches)-Number(!b.matches) || (a.average_rank || 0)-(b.average_rank || 0) || b.total_units-a.total_units || a.id-b.id),
    opponents:playerId ? [...opponents.values()].filter((r) => r.wins+r.draws+r.losses).sort((a,b) => b.matches-a.matches || a.id-b.id) : []};
}
function renderTechnical(matches) {
  const prior = Number($("technical-player").value);
  const players = technicalStats(matches).players;
  const selected = players.some((p) => p.id === prior) ? prior : players[0]?.id;
  $("technical-player").innerHTML = players.map((p) => `<option value="${p.id}"${p.id === selected ? ' selected' : ''}>${esc(p.name)}</option>`).join("");
  // Assign explicitly so the selection is retained when option markup changes.
  $("technical-player").value = selected ? String(selected) : "";
  $("technical-scope").textContent = `${scopeLabel()} · ${dateRange().label} · ${matches.filter((m) => m.record_kind !== "counts").length} 条成绩记录`;
  $("technical-body").innerHTML = players.map((r) => `<tr><td><div class="player-cell">${avatar(r)}${esc(r.name)}</div></td><td class="number">${r.matches}</td><td class="number ${scoreClass(r.average_score_units)}">${r.matches ? score(r.average_score_units) : '-'}</td><td class="number ${scoreClass(r.total_units)}">${score(r.total_units)}</td></tr>`).join("") || '<tr><td colspan="4"><div class="empty-state">暂无选手</div></td></tr>';
  renderOpponents(matches);
}
function renderOpponents(matches = filteredMatches()) {
  const id = Number($("technical-player").value), selected = playerById(id);
  const opponents = technicalStats(matches, id).opponents;
  $("head-to-head-caption").textContent = selected ? `${scopeLabel()}内 ${selected.name} 的对手统计. 同桌局数累计录入场数, 同桌场数及胜平负每条成绩记录计一次. 分数流向正数表示从对手获得, 负数表示付给对手.` : "请先登记选手.";
  $("opponent-body").innerHTML = opponents.map((r) => `<tr><td><div class="player-cell">${avatar(r)}${esc(r.name)}</div></td><td class="number">${r.matches}</td><td class="number">${r.sessions}</td><td class="number">${r.wins} / ${r.draws} / ${r.losses}</td><td class="number">${(r.win_rate*100).toFixed(2)}%</td><td class="number ${scoreClass(r.difference_units)}">${score(r.difference_units)}</td></tr>`).join("") || '<tr><td colspan="6"><div class="empty-state">所选赛季和日期范围内暂无交手记录</div></td></tr>';
}
function stats(matches) {
  const records = new Map(data.players.map((p) => [p.id, { ...p, matches: 0, wins: 0, base: 0, extra: 0, total: 0 }]));
  for (const match of matches) {
    const ranks = matchRanks(match);
    for (const entry of match.entries) {
      const r = records.get(entry.player_id);
      r.matches += entry.battle_count ?? 1;
      if (singleResult(match) && ranks.get(entry.player_id) === 1) r.wins++;
      r.base += entry.base_units; r.extra += entry.extra_units; r.total += entry.total_units;
    }
  }
  const rows = [...records.values()].filter((r) => r.active || r.matches).sort((a, b) => Number(!a.matches)-Number(!b.matches) || b.total - a.total || b.wins - a.wins || a.id - b.id);
  rows.forEach((r, i) => { r.rank = !r.matches ? "-" : i && r.total === rows[i - 1].total ? rows[i - 1].rank : i + 1; });
  return rows;
}

function toast(message, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = icon(error ? "help" : "check") + `<span>${esc(message)}</span>`;
  $("toasts").append(el);
  setTimeout(() => el.remove(), 4300);
}
function showError(id, message = "") {
  $(id).textContent = message;
  $(id).classList.toggle("hidden", !message);
}
function connection(online) {
  $("connection-label").classList.toggle("offline", !online);
  $("connection-label").innerHTML = `<span class="status-dot"></span>${online ? "本地模式" : "连接中断"}`;
  $("connection-banner").classList.toggle("hidden", online);
}
async function api(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), url.startsWith("/api/import") ? 60000 : 12000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal, headers: {
      "Content-Type": "application/json", "X-App-Token": data.token, ...(options.headers || {})
    }});
    const json = await response.json();
    if (!response.ok) throw new Error(json.error || "操作失败, 请重试.");
    return json;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      connection(false);
      throw new Error("无法连接服务. 输入已保留, 请确认服务运行后重试.");
    }
    throw error;
  } finally { clearTimeout(timeout); }
}
function loadState(force = false) {
  if (loadingPromise) return loadingPromise;
  loadingPromise = (async () => {
    try {
      const next = await api("/api/state");
      const signature = JSON.stringify(next);
      data = next;
      if (force || signature !== lastSignature) { lastSignature = signature; render(); }
      connection(true);
      $("last-sync").textContent = `已同步 ${new Date().toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })}`;
      $("demo-banner").classList.toggle("hidden", !data.demo);
      return true;
    } catch {
      connection(false);
      $("last-sync").textContent = "等待重新连接";
      return false;
    } finally {
      $("loading").classList.add("hidden");
      loadingPromise = null;
    }
  })();
  return loadingPromise;
}

const viewCopy = {
  overview: { label: "总览", title: "比赛概览", subtitle: "记录每一场比赛, 让成绩一目了然.", eyebrow: "MATCH DAY, EVERY DAY" },
  competitions: { label: "比赛管理", title: "每场比赛, 独立记录", subtitle: "建立比赛, 添加对局, 汇总本场成绩.", eyebrow: "A PLACE FOR EVERY COMPETITION" },
  history: { label: "对局记录", title: "对局记录", subtitle: "按比赛查看对局, 随时核对、移动与修改比分.", eyebrow: "EVERY MATCH HAS A STORY" },
  technical: { label: "技术统计", title: "看见每一次进步", subtitle: "批量选择比赛, 查询平均表现与对手交锋记录.", eyebrow: "MORE THAN A FINAL SCORE" },
  players: { label: "选手管理", title: "上场的伙伴", subtitle: "管理姓名与参赛状态, 一起把比赛继续下去.", eyebrow: "THE PEOPLE BEHIND THE SCORES" }
};
function switchView(next) {
  view = Object.hasOwn(viewCopy, next) ? next : "overview";
  const copy = viewCopy[view];
  for (const name of Object.keys(viewCopy)) {
    $(`${name}-view`).classList.toggle("hidden", name !== view);
    document.querySelector(`[data-view="${name}"]`).classList.toggle("active", name === view);
  }
  $("breadcrumb-view").textContent = copy.label;
  $("page-title").innerHTML = esc(copy.title) + '<span class="heading-period">.</span>';
  $("page-subtitle").textContent = copy.subtitle;
  $("page-eyebrow").textContent = copy.eyebrow;
  $("new-match-button").innerHTML = icon("plus") + (view === "players" ? "添加选手" : view === "competitions" ? "建立比赛" : "记一局");
  $("global-filters").classList.toggle("hidden", view === "players" || view === "competitions");
  $("export-button").classList.toggle("hidden", view === "players");
  $("import-button").classList.toggle("hidden", view === "players");
  $("sidebar").classList.remove("open"); $("sidebar-scrim").classList.remove("open");
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
}
function render() {
  const knownCompetitions = new Set((data.competitions || []).map((c) => c.id));
  if (selectedCompetitionIds !== null) for (const id of selectedCompetitionIds) if (!knownCompetitions.has(id)) selectedCompetitionIds.delete(id);
  const matches = filteredMatches(), rows = stats(matches);
  const range = dateRange();
  $("range-summary").textContent = range.start && range.end && range.start > range.end ? "开始日期不能晚于结束日期" : range.start || range.end ? `${range.start || "最早"} 至 ${range.end || "最新"}` : "全部时间";
  $("competition-filter-label").textContent = scopeLabel();
  $("leaderboard-period").textContent = scopeLabel();
  $("nav-match-count").textContent = data.matches.length;
  $("global-rate-label").textContent = data.settings.rate;
  renderOverview(matches, rows);
  renderHistory(matches);
  renderPlayers();
  renderCompetitions();
  renderTechnical(matches);
  if ($("export-dialog").open) renderRankingChoices();
  if ($("chart-selection-dialog").open) renderChartPlayerChoices();
  if ($("competition-selection-dialog").open) renderCompetitionChoices();
}
function renderOverview(matches, rows) {
  const participants = rows.filter((r) => r.matches);
  const leaders = participants.filter((r) => r.total === participants[0]?.total);
  $("metric-matches").innerHTML = `${matches.filter((m) => m.record_kind !== "counts").length}<span>条</span>`;
  $("metric-players").innerHTML = `${participants.length}<span>人</span>`;
  $("metric-match-caption").textContent = `选手累计对战数量: ${rows.reduce((sum,r) => sum+r.matches,0)}`;
  $("metric-player-caption").textContent = `${data.players.filter((p) => p.active).length} 位选手可参加新比赛`;
  $("metric-leader").textContent = leaders.length > 1 ? `${leaders.length} 人并列` : leaders[0]?.name || "暂无";
  $("metric-leader-caption").innerHTML = participants.length ? `<strong class="${scoreClass(participants[0].total)}">${score(participants[0].total)}</strong>累计总分` : "等待第一场比赛";
  $("metric-extra").innerHTML = `${score(rows.reduce((sum, r) => sum + r.extra, 0))}<span>分</span>`;
  $("leaderboard-body").innerHTML = rows.map((r) => `<tr>
    <td><span class="rank rank-${r.rank}">${r.rank}</span></td>
    <td><div class="player-cell">${avatar(r)}<span title="${esc(r.name)}">${esc(r.name)}</span></div></td>
    <td class="number">${r.matches}</td><td class="number">${score(r.base)}</td>
    <td class="number">${score(r.extra)}</td><td class="number total-cell">${score(r.total)}</td></tr>`).join("") || '<tr><td colspan="6"><div class="empty-state"><h3>还没有选手</h3><p>添加三位选手后就能开始录分.</p></div></td></tr>';
  $("recent-count").textContent = matches.length;
  $("recent-container").innerHTML = matches.length ? matchTable(matches.slice(0, 5), false) : emptyState("还没有对局记录", "点击记一局, 从第一场开始.", "match");
  renderChart(matches, selectedChartPlayers());
}
function emptyState(title, text, action = "") {
  return `<div class="empty-state"><div class="empty-icon">${icon("trophy")}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${action === "match" ? `<button class="button button-primary" data-action="new-match">${icon("plus")}记一局</button>` : ""}</div>`;
}
function matchTable(matches, detailed) {
  const headers = ["对局", "所属比赛", "对局时间", "选手与当局总分", "额外分合计", ...(detailed ? ["备注"] : []), "操作"];
  return `<table class="match-table"><thead><tr>${detailed ? '<th class="selection-cell"><input type="checkbox" id="select-page-matches" aria-label="选择本页对局"></th>' : ''}${headers.map((h, i) => `<th${i === headers.length - 1 ? ' class="number"' : ""}>${h}</th>`).join("")}</tr></thead><tbody>${matches.map((m) => {
    const date = m.played_at.slice(5, 10).replace("-", ".");
    return `<tr${selectedMatchIds.has(m.id) && detailed ? ' class="selected-row"' : ''}>${detailed ? `<td class="selection-cell"><input type="checkbox" data-select-match="${m.id}" aria-label="选择比赛 ${m.id}"${selectedMatchIds.has(m.id) ? ' checked' : ''}></td>` : ''}<td><span class="match-id">#${String(m.id).padStart(3, "0")}</span>${m.record_kind === "counts" ? '<small class="count-record-label">数量汇总</small>' : ""}</td>
    <td><span class="competition-tag" title="${esc(m.competition_name || competitionById(m.competition_id)?.name)}">${esc(m.competition_name || competitionById(m.competition_id)?.name || '-')}</span></td><td><span class="match-date" title="${esc(m.played_at.replace("T", " "))}">${m.played_at.slice(0,4)}.${date}<small>${m.played_at.slice(11)}</small></span></td>
    <td><div class="match-players${m.scoring_mode === "multi" ? " multi-match-players" : ""}">${m.entries.map((e) => `<div class="match-person" title="${esc(e.name)}: 基础分 ${score(e.base_units)}, 额外分 ${score(e.extra_units)}, rate ${esc(m.rate)}, 对战数量 ${e.battle_count === 0 && m.record_kind !== "counts" ? "已单独汇总" : e.battle_count ?? 1}">${avatar(playerById(e.player_id))}<span class="match-person-name">${esc(e.name)}</span><span class="match-person-score ${scoreClass(e.total_units)}">${m.record_kind === "counts" ? `${e.battle_count ?? 1} 场` : score(e.total_units)}</span><small class="match-person-count">${m.record_kind === "counts" ? "" : e.battle_count === 0 ? "场数另记" : `${e.battle_count ?? 1} 场`}</small></div>`).join("")}</div></td>
    <td><span class="extra-badge">${score(m.entries.reduce((sum, e) => sum + e.extra_units, 0))}</span></td>
    ${detailed ? `<td class="match-note-cell" title="${esc(m.note)}">${esc(m.note || "-")}</td>` : ""}
    <td><div class="match-actions"><button class="icon-button" data-action="edit-match" data-id="${m.id}" aria-label="编辑比赛 ${m.id}" title="编辑">${icon("edit")}</button><button class="icon-button danger" data-action="delete-match" data-id="${m.id}" aria-label="删除比赛 ${m.id}" title="删除">${icon("trash")}</button></div></td></tr>`;
  }).join("")}</tbody></table>`;
}
function historyMatches(matches = filteredMatches()) {
  const search = $("history-search").value.trim().toLocaleLowerCase();
  return matches.filter((m) => !search || `${m.id} #${String(m.id).padStart(3,"0")} ${m.competition_name || ''} ${m.note} ${m.entries.map((e) => e.name).join(" ")}`.toLocaleLowerCase().includes(search));
}
function renderHistory(matches) {
  const search = $("history-search").value.trim(), visible = historyMatches(matches);
  const visibleIds = new Set(visible.map((m) => m.id));
  for (const id of selectedMatchIds) if (!visibleIds.has(id)) selectedMatchIds.delete(id);
  page = Math.max(0, Math.min(page, Math.ceil(visible.length / PAGE_SIZE) - 1));
  $("history-count").textContent = visible.length;
  $("history-container").innerHTML = visible.length ? matchTable(visible.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), true) : emptyState(search ? "没有匹配的比赛" : "这个时间范围没有比赛", search ? "试试其他姓名、备注或比赛编号." : "更换日期范围, 或记录一场新比赛.", search ? "" : "match");
  $("pagination-info").textContent = visible.length ? `第 ${page * PAGE_SIZE + 1}-${Math.min((page + 1) * PAGE_SIZE, visible.length)} 条, 共 ${visible.length} 条` : "共 0 条记录";
  $("prev-page").disabled = page === 0;
  $("next-page").disabled = (page + 1) * PAGE_SIZE >= visible.length;
  $("match-selection-count").textContent = `已选 ${selectedMatchIds.size} 场`;
  $("bulk-delete-matches").disabled = selectedMatchIds.size === 0;
  $("bulk-move-matches").disabled = selectedMatchIds.size === 0 || !(data.competitions || []).length;
  $("select-all-matches").disabled = !visible.length;
  const pageCheckbox = $("select-page-matches"), pageMatches = visible.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);
  if (pageCheckbox) {
    const count = pageMatches.filter((m) => selectedMatchIds.has(m.id)).length;
    pageCheckbox.checked = count === pageMatches.length;
    pageCheckbox.indeterminate = count > 0 && count < pageMatches.length;
  }
}
function visiblePlayers() { return data.players.filter((p) => p.active || $("show-archived").checked); }
function renderPlayers() {
  const allStats = stats(data.matches);
  const active = data.players.filter((p) => p.active);
  $("players-summary").textContent = `${active.length} 位参赛中, ${data.players.length - active.length} 位已停用`;
  const players = visiblePlayers(), visibleIds = new Set(players.map((p) => p.id));
  for (const id of selectedPlayerIds) if (!visibleIds.has(id)) selectedPlayerIds.delete(id);
  $("player-selection-count").textContent = `已选 ${selectedPlayerIds.size} 人`;
  $("bulk-delete-players").disabled = selectedPlayerIds.size === 0;
  $("select-all-players").disabled = !players.length;
  $("players-grid").innerHTML = players.map((p) => {
    const r = allStats.find((s) => s.id === p.id) || { matches: 0, wins: 0, total: 0 };
    return `<article class="player-card ${p.active ? "" : "archived"} ${selectedPlayerIds.has(p.id) ? 'selected-player' : ''}"><div class="player-card-top"><label class="player-selection"><input type="checkbox" data-select-player="${p.id}"${selectedPlayerIds.has(p.id) ? ' checked' : ''} aria-label="选择 ${esc(p.name)}">${avatar(p)}</label><button class="icon-button" data-action="edit-player" data-id="${p.id}" aria-label="修改 ${esc(p.name)} 的姓名">${icon("edit")}</button></div><div class="player-card-name">${esc(p.name)}</div><div class="player-card-status">${p.active ? "参赛中" : "已停用, 历史记录保留"}</div><div class="player-card-stats"><div><strong>${r.matches}</strong><span>参赛场次</span></div><div><strong>${r.wins}</strong><span>单场第一名次数</span></div><div><strong class="${scoreClass(r.total)}">${score(r.total)}</strong><span>累计总分</span></div></div><div class="player-card-bottom"><span class="small-tag">${r.matches ? `总排名 #${r.rank}` : "等待第一场"}</span><button class="${p.active ? "archive-button" : "text-button"}" data-action="toggle-player" data-id="${p.id}">${p.active ? "停用选手" : "恢复参赛"}</button></div></article>`;
  }).join("") + `<button class="add-player-card" data-action="new-player"><span>${icon("plus")}</span>添加一位选手</button>`;
}

function selectedChartPlayers() {
  return data.players.filter((player) => chartSelectedIds === null || chartSelectedIds.has(player.id))
    .map((player) => {
      if (player.id <= 6) return {...player,color:player.color || "#5878f4"};
      const hue = Math.round(player.id*137.508%360)/30;
      const channel = (n) => {const k=(n+hue)%12;return Math.round(255*(.48-.288*Math.max(-1,Math.min(k-3,9-k,1)))).toString(16).padStart(2,"0");};
      return {...player,color:`#${channel(0)}${channel(8)}${channel(4)}`};
    });
}
function openChartSelection() {
  draftChartIds = new Set(selectedChartPlayers().map((player) => player.id));
  renderChartPlayerChoices();
  $("chart-selection-dialog").showModal();
}
function renderChartPlayerChoices() {
  $("chart-player-list").innerHTML = data.players.map((player) =>
    `<label class="export-ranking-player"><input type="checkbox" data-chart-player="${player.id}"${draftChartIds.has(player.id) ? " checked" : ""}>${avatar(player)}<span title="${esc(player.name)}">${esc(player.name)}</span></label>`
  ).join("") || '<span class="export-ranking-empty">请先添加选手</span>';
  updateChartPlayerCount();
}
function updateChartPlayerCount() {
  const count = data.players.filter((player) => draftChartIds.has(player.id)).length;
  $("chart-player-count").textContent = `已选择 ${count} / ${data.players.length} 位选手`;
  $("generate-chart").disabled = count === 0;
}
function selectChartPlayers(all) {
  draftChartIds = new Set(all ? data.players.map((player) => player.id) : []);
  renderChartPlayerChoices();
}
function generateSelectedChart(event) {
  event.preventDefault();
  const selected = data.players.filter((player) => draftChartIds.has(player.id));
  if (!selected.length) return;
  chartSelectedIds = selected.length === data.players.length ? null : new Set(selected.map((player) => player.id));
  renderChart(filteredMatches(), selectedChartPlayers());
  $("chart-selection-dialog").close();
}
function orderedScoreMatches(matches) {
  return matches.filter((match) => match.record_kind !== "counts")
    .sort((a, b) => a.played_at.localeCompare(b.played_at) || a.id - b.id);
}
function participationSeries(matches, series) {
  const ordered = orderedScoreMatches(matches);
  const entries = new Map();
  for (const match of ordered) {
    for (const entry of match.entries) {
      if (!entries.has(entry.player_id)) entries.set(entry.player_id, []);
      entries.get(entry.player_id).push(entry.total_units);
    }
  }
  return series.map((player) => {
    let total = 0;
    const values = [0], scores = entries.get(player.id) || [];
    for (const score of scores) { total += score; values.push(total); }
    return { player, values, actualMatches: scores.length };
  });
}
function globalSeries(matches, series) {
  const points = series.map((player) => ({player, values:[0], actualValues:[], participated:[false], actualMatches:0}));
  for (const match of orderedScoreMatches(matches)) {
    const entries = new Map(match.entries.map((entry) => [entry.player_id,entry]));
    for (const point of points) {
      const entry = entries.get(point.player.id), total = point.values.at(-1)+(entry?.total_units ?? 0);
      point.values.push(total); point.participated.push(!!entry);
      if (entry) { point.actualValues.push(total); point.actualMatches++; }
    }
  }
  return points;
}
function chartMode() { return $("chart-mode").value === "global" ? "global" : "personal"; }
function chartAxisLabel(mode) { return mode === "global" ? "全局场次" : "个人录分记录数"; }
function chartSeries(matches, series, mode = chartMode()) {
  return mode === "global" ? globalSeries(matches,series) : participationSeries(matches,series);
}
function chartRange(points) {
  // Scale only actual cumulative results; opening 0 and zeros before participation do not count.
  let lo = Infinity, hi = -Infinity;
  for (const point of points) {
    for (const value of point.actualValues ?? point.values.slice(1,point.actualMatches+1)) {
      lo = Math.min(lo,value); hi = Math.max(hi,value);
    }
  }
  return Number.isFinite(lo) ? { lo, hi } : null;
}
function chartSvg(points, range, options) {
  const matchCount = points.reduce((max, point) => Math.max(max, point.values.length - 1), 0);
  const {lo,hi} = range;
  const {width:W,height:H,top=19,bottom=45,fontSize=11,hover=true,background=false,mode="personal"} = options;
  const axisLabel = chartAxisLabel(mode);
  const left = Math.max(background ? 80 : 52,Math.max(score(lo,false).length,score(hi,false).length)*fontSize*.65+14), right = background ? 80 : 18;
  const labelColor = background ? "#58667d" : "#a4aec1", lineWidth = background ? 4 : 2.5;
  const x = (i) => left + (W - left - right) * i / matchCount;
  const y = (v) => hi === lo ? top+(H-top-bottom)/2+(lo-v) : top+(hi-v)/(hi-lo)*(H-top-bottom);
  let svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="${fontSize}" data-y-min="${lo}" data-y-max="${hi}" role="img" aria-label="${esc(points.map((p) => p.player.name).join(', '))} 的累计得分走势, 横轴为${axisLabel}, 当前最低 ${score(lo,false)} 分, 最高 ${score(hi,false)} 分"><title>累计得分走势, 按${axisLabel}</title>${background ? `<rect width="${W}" height="${H}" fill="#ffffff"/>` : ''}<defs><clipPath id="chart-plot-clip"><rect x="${left-4}" y="${top-4}" width="${W-left-right+8}" height="${H-top-bottom+8}"/></clipPath></defs>`;
  const yTicks = hi === lo ? [lo] : [...new Set(Array.from({length:5},(_,i) => Math.round(lo+(hi-lo)*i/4)))];
  for (const val of yTicks) {
    const yy = y(val);
    svg += `<path class="chart-grid" d="M${left} ${yy}H${W-right}" fill="none" stroke="#eef1f6" stroke-width="1"/><text class="chart-label" x="${left-9}" y="${yy+fontSize*.3}" fill="${labelColor}" text-anchor="end">${score(val, false)}</text>`;
  }
  if (lo<=0 && hi>=0) svg += `<path d="M${left} ${y(0)}H${W-right}" stroke="#dbe2ee" stroke-dasharray="3 4" fill="none"/>`;
  const ticks = matchCount <= 6 ? Array.from({ length: matchCount + 1 }, (_, i) => i) :
    [...new Set([0, Math.round(matchCount / 4), Math.round(matchCount / 2), Math.round(matchCount * 3 / 4), matchCount])];
  ticks.forEach((i) => {
    svg += `<text class="chart-label" x="${x(i)}" y="${H-bottom+fontSize+10}" fill="${labelColor}" text-anchor="middle">${i}</text>`;
  });
  svg += `<text class="chart-label" x="${(left+W-right)/2}" y="${H-bottom+fontSize*2+16}" fill="${labelColor}" text-anchor="middle">${axisLabel}</text>`;
  svg += '<g clip-path="url(#chart-plot-clip)">';
  points.forEach((p) => {
    if (mode !== "global" && !p.actualMatches) return;
    svg += `<polyline class="chart-line" stroke="${p.player.color}" fill="none" stroke-width="${lineWidth}" stroke-linejoin="round" stroke-linecap="round" points="${p.values.map((v,i)=>`${x(i)},${y(v)}`).join(' ')}"/>`;
  });
  svg += '</g>';
  for (let i = 0; hover && mode === "global" && i <= matchCount; i++) {
    const start = i === 0 ? left : x(i-.5), end = i === matchCount ? W-right : x(i+.5);
    svg += `<rect class="chart-hover-point" data-chart-index="${i}" x="${start}" y="${top}" width="${end-start}" height="${H-top-bottom}" fill="transparent"/>`;
  }
  // Keep endpoint markers above the global hover areas, so the markers receive pointer events.
  for (const point of points) {
    if (!point.actualMatches) continue;
    const end = point.values.length-1;
    const detail = `${point.player.name}, 对局数: ${point.actualMatches} 场, 累计得分: ${score(point.values[end])}`;
    svg += `<circle class="chart-endpoint"${hover ? ` data-chart-endpoint="${point.player.id}"` : ''} cx="${x(end)}" cy="${y(point.values[end])}" r="${background ? 8 : 5}" fill="${point.player.color}" stroke="#fff" stroke-width="2" aria-label="${esc(detail)}"><title>${esc(detail)}</title></circle>`;
  }
  return svg+'</svg>';
}
function renderChart(matches, series) {
  $("chart-legend").innerHTML = series.map((p) => `<span><i class="legend-dot" style="--series-color:${p.color}"></i>${esc(p.name)}</span>`).join("");
  const mode = chartMode(), axisLabel = chartAxisLabel(mode);
  $("chart-description").textContent = mode === "global" ? "每条成绩记录推进 1 场, 未参赛时累计分不变" : "按个人录分顺序累计, 各人画到自己的最后一场";
  const container = $("chart-container"), points = chartSeries(matches, series, mode), range = chartRange(points);
  $("chart-export-button").disabled = chartExporting || !range;
  if (!range) {
    container.onpointermove = null; container.onpointerleave = null;
    container.innerHTML = `<div class="empty-chart">${emptyState(series.length ? "当前范围没有所选选手的录分记录" : "选择选手生成折线图", series.length ? `更换选手、比赛或日期范围, 查看按${axisLabel}累计的得分.` : "点击选择选手, 可以同时查看一位或多位选手.")}</div>`;
    $("trend-foot").textContent = `已选择 ${series.length} 位选手, 按${axisLabel}累计`;
    return;
  }
  const {lo,hi} = range, matchCount = points.reduce((max, point) => Math.max(max, point.values.length - 1), 0);
  const width = Math.max(240,Math.round(container.getBoundingClientRect().width || 460)), plotHeight = 640;
  container.innerHTML = chartSvg(points,range,{width,height:plotHeight+19+45,mode})+'<div class="chart-tooltip hidden" id="chart-tooltip"></div>';
  const progress = mode === "global" ? `全局共 ${matchCount} 场, 每条成绩算 1 场, 未参赛时分数不变` : `个人最多 ${matchCount} 场, 曲线停在各自最后一场`;
  $("trend-foot").textContent = `已选 ${series.length} 位, ${progress}. 当前选手范围 ${score(lo,false)} 至 ${score(hi,false)}, 不含开局0.`;
  const tooltip = $("chart-tooltip");
  container.onpointermove = (event) => {
    const marker = event.target.closest("[data-chart-endpoint]");
    if (marker) {
      const point = points.find((p) => p.player.id === Number(marker.dataset.chartEndpoint));
      if (!point) { tooltip.classList.add("hidden"); return; }
      tooltip.innerHTML = `<strong>${esc(point.player.name)}</strong>对局数: ${point.actualMatches} 场<br>累计得分: ${score(point.values.at(-1))}`;
    } else {
      const node = mode === "global" && event.target.closest("[data-chart-index]");
      if (!node) { tooltip.classList.add("hidden"); return; }
      const i = Number(node.dataset.chartIndex);
      tooltip.innerHTML = `<strong>${i ? `全局第 ${i} 场后累计得分` : '参赛前'}</strong>${points.map((p) =>
        `${esc(p.player.name)}: ${score(p.values[i])}${i && !p.participated[i] ? ' (未参赛)' : ''}`
      ).join('<br>')}`;
    }
    const bounds = container.getBoundingClientRect();
    tooltip.classList.remove("hidden");
    tooltip.style.left = `${Math.max(0, Math.min(event.clientX-bounds.left+10, bounds.width-tooltip.offsetWidth))}px`;
    tooltip.style.top = `${Math.max(0,event.clientY-bounds.top-tooltip.offsetHeight-10)}px`;
  };
  container.onpointerleave = () => tooltip.classList.add("hidden");
}

function chartImageSvg(matches, series, competitionLabel, periodLabel, mode = chartMode()) {
  const points = chartSeries(matches,series,mode), range = chartRange(points);
  if (!range) return null;
  const width = 1920, height = 1080, fontSize = series.length > 48 ? 14 : 18;
  const columns = Math.min(series.length,Math.max(6,Math.ceil(series.length/10))), cellWidth = (width-160)/columns;
  const rowHeight = fontSize+16, legendHeight = Math.ceil(series.length/columns)*rowHeight;
  let svg = chartSvg(points,range,{width,height,top:125,bottom:legendHeight+72,fontSize:20,hover:false,background:true,mode});
  let notes = `<text x="80" y="48" font-size="32" font-weight="600" fill="#26354e">累计得分走势</text><text x="80" y="83" font-size="18" fill="#65738a">${esc(competitionLabel)} · ${esc(periodLabel)} · ${chartAxisLabel(mode)} · ${series.length} 位选手</text>`;
  series.forEach((player,i) => {
    const x = 80+(i%columns)*cellWidth, y = height-legendHeight+18+Math.floor(i/columns)*rowHeight;
    const textWidth = [...player.name].reduce((sum,c) => sum+fontSize*(/[^\x00-\x7f]/.test(c) ? 1 : .6),0);
    const fit = textWidth > cellWidth-36 ? ` textLength="${cellWidth-36}" lengthAdjust="spacingAndGlyphs"` : '';
    notes += `<circle cx="${x+6}" cy="${y-6}" r="6" fill="${player.color}"/><text x="${x+23}" y="${y}" font-size="${fontSize}" fill="#58667d"${fit}>${esc(player.name)}</text>`;
  });
  return svg.replace('</svg>',notes+'</svg>');
}
async function exportChartImage() {
  if (chartExporting) return;
  const period = dateRange();
  const periodLabel = period.start || period.end ? `${period.label} (${period.start || '不限'} - ${period.end || '不限'})` : period.label;
  const svg = chartImageSvg(filteredMatches(),selectedChartPlayers(),scopeLabel(),periodLabel);
  if (!svg) { toast("当前范围没有可导出的折线图.",true); return; }
  chartExporting = true; $("chart-export-button").disabled = true;
  let address;
  try {
    if (document.fonts) await document.fonts.ready;
    address = URL.createObjectURL(new Blob([svg],{type:"image/svg+xml;charset=utf-8"}));
    const image = new Image();
    await new Promise((resolve,reject) => { image.onload=resolve; image.onerror=()=>reject(new Error("折线图图片生成失败, 请重试.")); image.src=address; });
    const canvas = document.createElement("canvas"); canvas.width=1920; canvas.height=1080;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("当前浏览器无法生成图片.");
    ctx.fillStyle="#ffffff"; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.drawImage(image,0,0);
    const png = await new Promise((resolve,reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("折线图图片生成失败, 请重试.")),"image/png"));
    downloadBlob(png,`scoreboard-chart-${localDate(new Date())}-1920x1080.png`);
    toast("已导出 1920 x 1080 PNG 图片.");
  } catch (error) { toast(error.message || "折线图导出失败, 请重试.",true); }
  finally {
    if (address) URL.revokeObjectURL(address);
    chartExporting = false;
    $("chart-export-button").disabled = !chartRange(chartSeries(filteredMatches(),selectedChartPlayers()));
  }
}

function newKey() { return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`; }
function openMatch(id = null, competitionId = null) {
  const match = id ? data.matches.find((m) => m.id === Number(id)) : null;
  if (id && !match) { toast("比赛已不存在, 请刷新后重试.", true); return; }
  if (match?.scoring_mode === "multi") { openMultiMatch(id, competitionId); return; }
  if (!(data.competitions || []).length) { toast("请先建立一场比赛.", true); switchView("competitions"); openCompetition(); return; }
  if (!match && data.players.filter((p) => p.active).length < 3) { toast("请先添加或恢复至少三位选手.", true); switchView("players"); return; }
  editingMatch = match; requestKey = newKey(); knockoutAwards = { ...(match?.knockouts || {}) }; knockoutSavePending = false;
  $("match-dialog-title").textContent = match ? `编辑对局 #${String(match.id).padStart(3,'0')}` : "记一局";
  const chosenCompetition = match?.competition_id || competitionId || (selectedCompetitionIds?.size === 1 ? [...selectedCompetitionIds][0] : data.competitions[0].id);
  $("match-competition").innerHTML = competitionOptions(chosenCompetition);
  $("match-competition").value = String(chosenCompetition);
  $("match-date").value = match?.played_at || localDateTime();
  $("match-note").value = match?.note || "";
  $("scoring-mode").value = match?.scoring_mode || "points";
  for (const option of $("scoring-mode").options) if (option.value === "multi") option.disabled = Boolean(match);
  $("match-rate").value = match?.rate || data.settings.rate;
  const eligible = data.players.filter((p) => p.active || match?.entries.some((e) => e.player_id === p.id));
  $("participant-grid").innerHTML = Array.from({length:3}, (_, i) => {
    const entry = match?.entries[i], selected = entry?.player_id ?? eligible[i].id;
    const rank = match?.rank_order.indexOf(selected) + 1 || '';
    return `<div class="participant-card"><div class="participant-card-top"><span id="seat-avatar-${i}">${avatar(playerById(selected))}</span><span class="seat-label">PLAYER 0${i+1}</span></div><select id="player-${i}" aria-label="第 ${i+1} 位选手" required>${eligible.map((p)=>`<option value="${p.id}"${p.id===selected?' selected':''}>${esc(p.name)}${p.active?'':' (已停用)'}</option>`).join('')}</select><label class="score-field base point-field" for="points-${i}">点数 <span class="optional">整数</span><input id="points-${i}" type="text" inputmode="numeric" autocomplete="off" placeholder="例如: 500" value="${entry?.points ?? ''}" aria-label="第 ${i+1} 位选手的点数"></label><label class="score-field chip point-field" for="chip-${i}">chip <span class="optional">1 chip = 3 分</span><input id="chip-${i}" type="text" inputmode="text" autocomplete="off" value="${entry?.chip ?? '0'}" aria-label="第 ${i+1} 位选手的 chip"></label><label class="score-field tie-field hidden" id="rank-label-${i}" for="rank-${i}">同分时计分顺序<select id="rank-${i}" aria-label="第 ${i+1} 位选手的同分计分顺序"><option value="">请选择</option>${[1,2,3].map((r)=>`<option value="${r}"${r===rank?' selected':''}>按第 ${r} 名计算</option>`).join('')}</select></label><label class="score-field base manual-field hidden" for="base-${i}">基础分<input id="base-${i}" type="text" inputmode="decimal" autocomplete="off" placeholder="0" value="${entry ? plainScore(entry.base_cents) : ''}" aria-label="第 ${i+1} 位选手的基础分"></label><label class="score-field extra" for="extra-${i}">额外分 <span class="optional">乘 rate 前</span><input id="extra-${i}" type="text" inputmode="decimal" autocomplete="off" value="${entry ? plainScore(entry.extra_cents) : '0'}" required aria-label="第 ${i+1} 位选手的额外分"></label><div class="score-breakdown point-field" id="breakdown-${i}">基础分会自动计算</div><label class="score-field" for="battle-count-${i}">对战数量 <span class="optional">默认 1</span><input id="battle-count-${i}" type="text" inputmode="numeric" autocomplete="off" value="${entry?.battle_count ?? 1}" aria-label="第 ${i+1} 位选手的对战数量"></label><div class="participant-total"><span>当场总分 <small>× rate</small></span><strong id="total-${i}">-</strong></div></div>`;
  }).join('');
  showError("match-error"); saving = false; validateMatch();
  $("match-dialog").showModal();
  setTimeout(() => $($("scoring-mode").value === "points" ? "points-0" : "base-0").focus(), 50);
}
function readMatchForm() {
  const mode = $("scoring-mode").value;
  const payload = { scoring_mode: mode, rate: $("match-rate").value,
    competition_id: Number($("match-competition").value),
    played_at: $("match-date").value, note: $("match-note").value, request_key: requestKey,
    entries: [0,1,2].map((i) => ({ player_id: Number($(`player-${i}`).value),
      base: $(`base-${i}`).value, points: $(`points-${i}`).value, chip: $(`chip-${i}`).value, extra: $(`extra-${i}`).value,
      battle_count:$(`battle-count-${i}`).value })) };
  if (mode === "points") {
    const pts = payload.entries.map((e) => parseInteger(e.points)), ids = payload.entries.map((e) => e.player_id);
    for (const [loser, recipient] of Object.entries(knockoutAwards)) {
      if (!ids.includes(Number(loser)) || pts[ids.indexOf(Number(loser))] !== 0 || !ids.includes(recipient) || Number(loser) === recipient) delete knockoutAwards[loser];
    }
    payload.knockouts = { ...knockoutAwards };
    const sorted = [...pts].sort((a,b) => b-a), topTie = sorted[0] === sorted[1] && sorted[1] > sorted[2];
    if (pts.every((n) => n !== null) && new Set(pts).size !== 3 && !topTie) {
      payload.rank_order = [1,2,3].map((rank) => ids.find((_,i) => Number($(`rank-${i}`).value) === rank));
    }
  }
  if (editingMatch) payload.revision = editingMatch.revision;
  return payload;
}
function syncScoringFields() {
  const pointsMode = $("scoring-mode").value === "points";
  document.querySelectorAll(".point-field").forEach((el) => el.classList.toggle("hidden",!pointsMode));
  document.querySelectorAll(".manual-field").forEach((el) => el.classList.toggle("hidden",pointsMode));
  $("autofill-button").classList.toggle("hidden",pointsMode);
  $("scoring-hint").textContent = pointsMode ? "名次只看点数, chip 不影响名次. 一、二名同点数自动平分第三名的名次分, 二、三名同点数沿用原算法. chip 合计须为 0, 不检查点数总和." : "基础分合计为 0, 分数最多两位小数. 历史手动记录按原分数编辑.";
  const pts = [0,1,2].map((i) => parseInteger($(`points-${i}`).value)), sorted = [...pts].sort((a,b) => b-a);
  const topTie = pts.every((n) => n !== null) && sorted[0] === sorted[1] && sorted[1] > sorted[2];
  [0,1,2].forEach((i) => {
    const allowed = pts.every((n) => n !== null) ? [1,2,3].filter((rank) => sorted[rank-1] === pts[i]) : [];
    const tied = pointsMode && allowed.length > 1 && !topTie;
    $(`rank-label-${i}`).classList.toggle("hidden",!tied);
    const select = $(`rank-${i}`);
    for (const option of select.options) if (option.value) option.disabled = !allowed.includes(Number(option.value));
    if (allowed.length === 1) select.value = String(allowed[0]);
    else if (allowed.length && !allowed.includes(Number(select.value))) select.value = "";
  });
}
function validateMatch() {
  syncScoringFields();
  const payload = readMatchForm(), result = calculateMatch(payload), ids = payload.entries.map((e) => String(e.player_id));
  const pointMode = payload.scoring_mode === "points", pending = !result.error && result.missingAwards.length > 0;
  [0,1,2].forEach((i) => {
    const total = result.error || pending ? null : result.totals[i];
    $(`total-${i}`).textContent = pending ? "待选赏分" : total === null ? "-" : score(total);
    $(`total-${i}`).className = total === null ? "" : scoreClass(total);
    $(`seat-avatar-${i}`).innerHTML = avatar(playerById(ids[i]));
    [...$(`player-${i}`).options].forEach((option) => { option.disabled = option.value !== ids[i] && ids.includes(option.value); });
    if (pointMode) $(`breakdown-${i}`).innerHTML = result.error ? "名次分 + chip 分 + 击飞赏 = 基础分" : `<span>名次 ${score(result.placements[i]*100)} / chip ${score(result.chipScores[i]*100)} / 击飞 ${pending ? '待选' : score(result.knockoutScores[i]*100)}</span><strong>基础分 ${pending ? '待定' : score(result.bases[i]*100)}</strong>`;
  });
  let title = result.error || (pending ? "请选择击飞赏归属" : "基础分合计为 0, 校验通过");
  let detail = result.error ? (pointMode ? "基础分由点数、chip 和击飞赏自动计算." : "额外分不参与基础分零和校验.") : pending ? "0 点玩家扣 10 分, 请指定获得这 10 分的另一位本局玩家." : `本场 rate = ${payload.rate}, 总分合计 ${score(result.totals.reduce((a,b)=>a+b,0))}.`;
  let mode = result.error ? "invalid" : pending ? "" : "valid";
  const dateValid = Boolean($("match-date").value) && $("match-date").validity.valid;
  const competitionValid = Boolean(competitionById(payload.competition_id));
  if (!result.error && !dateValid) { title = "请选择有效的比赛时间"; mode = "invalid"; }
  if (!competitionValid) { title = "请选择所属比赛"; mode = "invalid"; }
  $("balance-box").className = "balance-box" + (mode ? ` ${mode}` : "");
  $("balance-title").textContent = title; $("balance-description").textContent = detail;
  $("balance-value").textContent = result.error || pending ? "-" : "0";
  $("balance-box").querySelector(".balance-icon").innerHTML = icon(mode === "valid" ? "check" : "help");
  const manualBases = payload.entries.map((e) => parseCents(e.base));
  $("autofill-button").disabled = saving || manualBases[0] === null || manualBases[1] === null || Math.abs((manualBases[0] || 0)+(manualBases[1] || 0)) > 100000000;
  const zeros = pointMode ? payload.entries.filter((e) => parseInteger(e.points) === 0) : [];
  $("knockout-button").classList.toggle("hidden", !zeros.length);
  $("knockout-button").disabled = Boolean(result.error) || saving;
  $("knockout-button").textContent = pending ? "选择击飞赏归属" : "修改击飞赏归属";
  $("knockout-summary").classList.toggle("hidden",!zeros.length);
  $("knockout-summary").textContent = zeros.map((e) => `${playerById(e.player_id)?.name}: -10 分 → ${playerById(knockoutAwards[e.player_id])?.name || '待选择'}: +10 分`).join("; ");
  $("save-match").disabled = saving || Boolean(result.error) || !dateValid || !competitionValid;
  if (!saving) $("save-match").innerHTML = icon("check") + (pending ? "选择击飞赏并保存" : "保存对局");
  return !result.error && dateValid && competitionValid;
}
function beginKnockouts(saveAfter = false, force = false) {
  if (!validateMatch()) return;
  const payload = readMatchForm(), result = calculateMatch(payload);
  const zeroIds = payload.entries.filter((e) => parseInteger(e.points) === 0).map((e) => e.player_id);
  const queue = force ? zeroIds : result.missingAwards;
  if (!queue.length) return;
  knockoutPending = { queue: [...queue], ids: payload.entries.map((e) => e.player_id) };
  knockoutSavePending = saveAfter;
  showNextKnockout();
}
function showNextKnockout() {
  const loser = knockoutPending.queue[0], ids = knockoutPending.ids;
  $("knockout-description").textContent = `${playerById(loser)?.name} 本局为 0 点, 将扣除 10 分. 请选择这份击飞赏的获得者.`;
  $("knockout-recipient").innerHTML = '<option value="">请选择本局玩家</option>' + ids.filter((id) => id !== loser).map((id) => `<option value="${id}"${knockoutAwards[loser] === id ? ' selected' : ''}>${esc(playerById(id)?.name)}</option>`).join('');
  if (!$("knockout-dialog").open) $("knockout-dialog").showModal();
}
function finishKnockout(event) {
  event.preventDefault();
  const recipient = Number($("knockout-recipient").value), loser = knockoutPending?.queue[0];
  if (!loser || recipient === loser || !knockoutPending.ids.includes(recipient)) return;
  knockoutAwards[loser] = recipient; requestKey = newKey(); knockoutPending.queue.shift();
  validateMatch();
  if (knockoutPending.queue.length) { showNextKnockout(); return; }
  const shouldSave = knockoutSavePending;
  knockoutPending = null; knockoutSavePending = false; $("knockout-dialog").close();
  if (shouldSave) saveMatch({ preventDefault() {} });
}
async function saveMatch(event) {
  event.preventDefault();
  if (saving || !validateMatch()) return;
  const payload = readMatchForm(), result = calculateMatch(payload);
  if (result.missingAwards.length) { beginKnockouts(true); return; }
  if (payload.scoring_mode === "points") payload.rank_order = result.order;
  const wasEditing = Boolean(editingMatch), url = editingMatch ? `/api/matches/${editingMatch.id}` : "/api/matches";
  saving = true; $("save-match").disabled = true; $("save-match").innerHTML = "正在保存...";
  showError("match-error");
  try {
    await api(url, { method: wasEditing ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("match-dialog").close();
    const synced = await loadState(true);
    toast(synced ? (wasEditing ? "对局已更新, 统计已同步." : "对局已保存, 统计已同步.") : "对局已保存, 统计将在连接恢复后更新.");
  } catch (error) { showError("match-error", error.message); }
  finally { saving = false; $("save-match").innerHTML = icon("check") + "保存对局"; validateMatch(); }
}

function openMultiMatch(id = null, competitionId = null) {
  const match = id ? data.matches.find((m) => m.id === Number(id)) : null;
  if (id && !match) { toast("对局已不存在, 请刷新后重试.", true); return; }
  if (!(data.competitions || []).length) { toast("请先建立一场比赛.", true); switchView("competitions"); openCompetition(); return; }
  multiEditing = match; multiRequestKey = newKey(); multiSaving = false; multiReading = false;
  const cid = match?.competition_id || competitionId || (selectedCompetitionIds?.size === 1 ? [...selectedCompetitionIds][0] : data.competitions[0].id);
  $("multi-title").textContent = match ? `编辑多人对局 #${String(match.id).padStart(3,'0')}` : "多人录分";
  $("multi-competition").innerHTML = competitionOptions(cid); $("multi-competition").value = String(cid);
  $("multi-date").value = match?.played_at || localDateTime(); $("multi-note").value = match?.note || "";
  $("multi-rate").value = match?.rate || data.settings.rate;
  $("multi-kind").value = match?.record_kind || "score";
  $("multi-file").value = ""; $("multi-paste").value = "";
  multiRows = match ? match.entries.map((e) => ({player_id:e.player_id, player_name:e.name, base:plainScore(e.base_cents), battle_count:e.battle_count ?? 1}))
    : data.players.filter((p) => p.active).slice(0,4).map((p) => ({player_id:p.id, player_name:p.name, base:"", battle_count:1}));
  showError("multi-error"); renderMultiRows(); $("multi-dialog").showModal();
}
function readMultiRows() {
  return multiRows.map((row,i) => {
    const selected = $(`multi-player-${i}`).value, pid = Number(selected);
    return {player_id:pid > 0 ? pid : null, player_name:pid > 0 ? playerById(pid)?.name || "" : row.player_name,
      base:$(`multi-base-${i}`).value, battle_count:$(`multi-count-${i}`).value};
  });
}
function readMultiForm() {
  const payload = {scoring_mode:"multi", competition_id:Number($("multi-competition").value),
    played_at:$("multi-date").value, note:$("multi-note").value, rate:$("multi-rate").value,
    request_key:multiRequestKey, record_kind:$("multi-kind").value, entries:readMultiRows()};
  if (multiEditing) payload.revision = multiEditing.revision;
  return payload;
}
function renderMultiRows() {
  $("multi-body").innerHTML = multiRows.map((row,i) => {
    const eligible = data.players.filter((p) => p.active || p.id === row.player_id);
    const newOption = row.player_id ? "" : `<option value="new-${i}" selected>${esc(row.player_name)} (新选手)</option>`;
    return `<tr><td class="multi-seat">${i+1}</td><td><select id="multi-player-${i}" aria-label="第 ${i+1} 位选手">${newOption}${eligible.map((p) => `<option value="${p.id}"${p.id === row.player_id ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select></td><td><input id="multi-base-${i}" type="text" inputmode="decimal" autocomplete="off" value="${esc(row.base)}" placeholder="基础分" aria-label="第 ${i+1} 位选手的基础分"></td><td><input id="multi-count-${i}" type="text" inputmode="numeric" autocomplete="off" value="${esc(row.battle_count ?? 1)}" aria-label="第 ${i+1} 位选手的对战数量"></td><td class="number"><strong id="multi-total-${i}">-</strong></td><td><button type="button" class="icon-button danger" data-remove-multi="${i}" aria-label="移除第 ${i+1} 位选手">${icon("trash")}</button></td></tr>`;
  }).join("") || '<tr><td colspan="6"><div class="empty-state">添加选手或读入成绩表, 即可开始录分.</div></td></tr>';
  validateMulti();
}
function addMultiPlayers(all = false) {
  if (multiSaving || multiReading) return;
  multiRows = readMultiRows();
  const used = new Set(multiRows.map((r) => r.player_id));
  const available = data.players.filter((p) => p.active && !used.has(p.id));
  if (!available.length) { toast("已添加全部可参赛选手. 新姓名也可通过表格或粘贴数据读入.", true); return; }
  for (const p of all ? available : available.slice(0,1)) multiRows.push({player_id:p.id,player_name:p.name,base:$("multi-kind").value === "counts" ? "0" : "",battle_count:1});
  multiRequestKey = newKey(); renderMultiRows();
}
function validateMulti() {
  const payload = readMultiForm(), names = payload.entries.map((e) => e.player_name.trim().toLocaleLowerCase());
  const result = calculateMatch({...payload, entries:payload.entries.map((e,i) => ({...e,player_id:e.player_id || 1000000000+i}))});
  if (new Set(names).size !== names.length || names.some((name) => !name)) result.error = "每位选手在本局只能录入一次, 姓名不能为空.";
  const dateValid = Boolean(payload.played_at) && $("multi-date").validity.valid;
  const competitionValid = Boolean(competitionById(payload.competition_id));
  payload.entries.forEach((entry,i) => {
    $(`multi-total-${i}`).textContent = result.error ? "-" : score(result.totals[i]);
    $(`multi-total-${i}`).className = result.error ? "" : scoreClass(result.totals[i]);
    for (const option of $(`multi-player-${i}`).options) option.disabled = Number(option.value) > 0 && Number(option.value) !== entry.player_id && payload.entries.some((other) => other.player_id === Number(option.value));
    $(`multi-player-${i}`).disabled = multiSaving || multiReading;
    $(`multi-base-${i}`).disabled = multiSaving || multiReading;
    $(`multi-count-${i}`).disabled = multiSaving || multiReading;
  });
  $("multi-summary").textContent = result.error || !dateValid || !competitionValid ? result.error || "请选择有效的比赛和对局时间."
    : payload.record_kind === "counts" ? `只汇总对战数量, 不增加得分、名次或胜负. 请勿重复登记已计入对局的场数.`
      : `${payload.entries.length} 位选手, rate = ${payload.rate}, 总分合计 ${score(result.totals.reduce((a,b) => a+b,0))}.`;
  $("save-multi").disabled = multiSaving || multiReading || Boolean(result.error) || !dateValid || !competitionValid;
  for (const id of ["multi-add","multi-add-all","multi-read-file","multi-read-paste","multi-rate","multi-kind","multi-date","multi-note","multi-competition","multi-file","multi-paste"]) $(id).disabled = multiSaving || multiReading;
  return !result.error && dateValid && competitionValid;
}
async function readMultiScores(pasted = false) {
  if (multiSaving || multiReading) return;
  const file = $("multi-file").files?.[0];
  if (!pasted && (!file || file.size > 5*1024*1024)) { showError("multi-error","请选择不超过 5 MB 的 XLSX 或 CSV."); return; }
  if (pasted && !$("multi-paste").value.trim()) { showError("multi-error","请先粘贴选手和基础分两列数据."); return; }
  multiReading = true; showError("multi-error"); validateMulti();
  try {
    let payload;
    if (pasted) payload = {text:$("multi-paste").value};
    else {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = ""; for (let i=0;i<bytes.length;i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192));
      payload = {filename:file.name,content:btoa(binary)};
    }
    const result = await api("/api/multi-scores/preview",{method:"POST",body:JSON.stringify(payload)});
    multiRows = result.draft.entries; multiRequestKey = newKey(); renderMultiRows();
    toast(`已读入 ${multiRows.length} 位选手${result.draft.new_players.length ? `, 保存时建立 ${result.draft.new_players.length} 位新选手` : ''}. 点击保存后计入比赛.`);
  } catch (error) { showError("multi-error",error.message || "成绩表读取失败."); }
  finally { multiReading = false; validateMulti(); }
}
async function saveMultiMatch(event) {
  event.preventDefault();
  if (multiSaving || multiReading || !validateMulti()) return;
  const payload = readMultiForm(), wasEditing = Boolean(multiEditing);
  multiSaving = true; validateMulti(); $("save-multi").textContent = "正在保存..."; showError("multi-error");
  try {
    await api(wasEditing ? `/api/matches/${multiEditing.id}` : "/api/matches",{method:wasEditing ? "PUT" : "POST",body:JSON.stringify(payload)});
    $("multi-dialog").close(); const synced = await loadState(true);
    toast(synced ? "多人对局已保存, 已计入对应比赛." : "多人对局已保存, 统计将在连接恢复后更新.");
  } catch (error) { showError("multi-error",error.message || "保存失败."); }
  finally { multiSaving = false; $("save-multi").textContent = "保存对局"; validateMulti(); }
}
function renderCompetitions() {
  const competitions = data.competitions || [];
  $("competitions-summary").textContent = `共 ${competitions.length} 场比赛, ${data.matches.filter((m) => m.record_kind !== "counts").length} 条成绩记录`;
  $("competitions-grid").innerHTML = competitions.map((c) => {
    const matches = data.matches.filter((m) => m.competition_id === c.id), rows = stats(matches).filter((r) => r.matches);
    const leaders = rows.filter((r) => r.total === rows[0]?.total);
    const recent = matches[0]?.played_at.slice(0,10) || "尚未开始";
    return `<article class="competition-card"><div class="competition-card-top"><span class="competition-mark">${icon("trophy")}</span><div class="match-actions"><button class="icon-button" data-action="edit-competition" data-id="${c.id}" aria-label="编辑 ${esc(c.name)}">${icon("edit")}</button><button class="icon-button danger" data-action="delete-competition" data-id="${c.id}" aria-label="删除 ${esc(c.name)}">${icon("trash")}</button></div></div><h2 title="${esc(c.name)}">${esc(c.name)}</h2><p class="competition-description">${esc(c.note || '为这场比赛留下每一局成绩.')}</p><div class="competition-card-stats"><div><strong>${matches.filter((m) => m.record_kind !== "counts").length}</strong><span>成绩记录</span></div><div><strong>${rows.length}</strong><span>选手</span></div><div><strong class="competition-leader">${esc(leaders.length > 1 ? '并列领跑' : leaders[0]?.name || '-')}</strong><span>本场领跑</span></div></div><div class="competition-recent">最近对局: ${esc(recent)}</div><div class="competition-card-actions"><button class="button button-small button-secondary" data-action="competition-results" data-id="${c.id}">查看本场统计</button><button class="button button-small button-primary" data-action="competition-round" data-id="${c.id}">${icon("plus")}新建对局</button><button class="button button-small button-secondary" data-action="competition-multi" data-id="${c.id}">多人录分</button></div></article>`;
  }).join("") || `<div class="empty-competition">${emptyState('还没有比赛', '建立比赛后即可在比赛下记录对局.')}<button class="button button-primary" data-action="new-competition">${icon("plus")}建立比赛</button></div>`;
}
function openCompetition(id = null) {
  editingCompetition = id ? competitionById(id) : null;
  if (id && !editingCompetition) return;
  $("competition-title").textContent = editingCompetition ? "编辑比赛" : "建立比赛";
  $("competition-name").value = editingCompetition?.name || "";
  $("competition-note").value = editingCompetition?.note || "";
  showError("competition-error"); $("competition-dialog").showModal();
  setTimeout(() => $("competition-name").focus(), 40);
}
async function saveCompetition(event) {
  event.preventDefault();
  if (competitionBusy) return;
  const name = $("competition-name").value.trim(), note = $("competition-note").value.trim();
  if (!name) { showError("competition-error", "请填写比赛名称."); return; }
  const prior = editingCompetition, payload = {name,note,revision:prior?.revision};
  competitionBusy = true; $("save-competition").disabled = true; showError("competition-error");
  try {
    await api(prior ? `/api/competitions/${prior.id}` : "/api/competitions", {method:prior ? "PATCH" : "POST",body:JSON.stringify(payload)});
    $("competition-dialog").close(); await loadState(true); toast(prior ? "比赛已更新." : "比赛已建立, 可以添加对局.");
  } catch (error) { showError("competition-error",error.message); }
  finally { competitionBusy = false; $("save-competition").disabled = false; }
}
function viewCompetition(id = null) {
  selectedCompetitionIds = id ? new Set([Number(id)]) : null;
  page = 0; selectedMatchIds.clear(); render(); switchView("overview");
}
function deleteCompetition(id) {
  const c = competitionById(id); if (!c) return;
  const matches = data.matches.filter((m) => m.competition_id === c.id);
  const payload = {revision:c.revision,matches:matches.map((m) => ({id:m.id,revision:m.revision}))};
  confirmAction(`删除比赛 ${c.name}?`, `将删除比赛以及其中的 ${matches.length} 局完整对局. 选手名单保留, 对应成绩从统计中移除.\n此操作无法撤销.`, "删除比赛及对局", async () => {
    await api(`/api/competitions/${c.id}`, {method:"DELETE",body:JSON.stringify(payload)}); toast("比赛及其对局已删除.");
  });
}
function renderCompetitionChoices() {
  const competitions = data.competitions || [], known = new Set(competitions.map((c) => c.id));
  for (const id of draftCompetitionIds) if (!known.has(id)) draftCompetitionIds.delete(id);
  $("competition-choice-list").innerHTML = competitions.map((c) => `<label class="competition-choice"><input type="checkbox" data-scope-competition="${c.id}"${draftCompetitionIds.has(c.id) ? ' checked' : ''}><span><strong>${esc(c.name)}</strong><small>${data.matches.filter((m) => m.competition_id === c.id).length} 局</small></span></label>`).join("") || '<span class="export-ranking-empty">请先建立比赛</span>';
  updateCompetitionCount();
}
function updateCompetitionCount() {
  $("competition-choice-count").textContent = `已选 ${draftCompetitionIds.size} / ${(data.competitions || []).length} 场比赛`;
  $("apply-competitions").disabled = !draftCompetitionIds.size;
}
function openCompetitionSelection() {
  draftCompetitionIds = new Set(selectedCompetitionIds === null ? (data.competitions || []).map((c) => c.id) : selectedCompetitionIds);
  renderCompetitionChoices(); $("competition-selection-dialog").showModal();
}
function applyCompetitionSelection(event) {
  event.preventDefault(); if (!draftCompetitionIds.size) return;
  selectedCompetitionIds = draftCompetitionIds.size === data.competitions.length ? null : new Set(draftCompetitionIds);
  page = 0; selectedMatchIds.clear(); $("competition-selection-dialog").close(); render();
}
function openMove() {
  moveSelection = data.matches.filter((m) => selectedMatchIds.has(m.id)).map((m) => ({id:m.id,revision:m.revision}));
  if (!moveSelection.length || !data.competitions.length) return;
  $("move-description").textContent = `将 ${moveSelection.length} 局对局归入选定的比赛.`;
  $("move-competition").innerHTML = competitionOptions();
  showError("move-error"); $("move-dialog").showModal();
}
async function saveMove(event) {
  event.preventDefault(); if (moveBusy) return;
  moveBusy = true; $("save-move").disabled = true; showError("move-error");
  try {
    await api("/api/matches/bulk-move", {method:"POST",body:JSON.stringify({competition_id:Number($("move-competition").value),matches:moveSelection})});
    $("move-dialog").close(); selectedMatchIds.clear(); await loadState(true); toast(`${moveSelection.length} 局对局已移动.`);
  } catch (error) { showError("move-error",error.message); }
  finally { moveBusy = false; $("save-move").disabled = false; }
}
function resetImportPreview() {
  importPreview = null; importUpload = null;
  $("import-preview").innerHTML = ""; $("import-preview").classList.add("hidden"); $("commit-import").disabled = true;
  showError("import-error");
}
function openImport() {
  resetImportPreview(); $("import-file").value = ""; $("import-mode").value = "skip";
  const chosen = selectedCompetitionIds?.size === 1 ? [...selectedCompetitionIds][0] : null;
  $("import-competition").innerHTML = competitionOptions(chosen, "按表中的比赛归属");
  $("import-competition").value = chosen ? String(chosen) : "";
  $("import-dialog").showModal();
}
function setImportBusy(busy) {
  importBusy = busy;
  for (const id of ["import-file","import-mode","import-competition","preview-import"]) $(id).disabled = busy;
  $("commit-import").disabled = busy || !importPreview || !importPreview.created && !importPreview.updated;
}
function fileContent(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("文件读取失败, 请重新选择."));
    reader.readAsDataURL(file);
  });
}
async function previewImport(event) {
  event.preventDefault(); if (importBusy) return;
  resetImportPreview();
  const file = $("import-file").files[0];
  if (!file || !/\.(xlsx|csv)$/i.test(file.name) || file.size > 5*1024*1024) { showError("import-error","请选择不超过 5 MB 的 XLSX 或 CSV 文件."); return; }
  setImportBusy(true); $("preview-import").textContent = "正在检查...";
  try {
    const payload = {filename:file.name,content:await fileContent(file),mode:$("import-mode").value,competition_id:$("import-competition").value || null};
    const result = (await api("/api/import/preview", {method:"POST",body:JSON.stringify(payload)})).import;
    importUpload = payload; importPreview = result;
    $("import-preview").innerHTML = `<div class="preview-counts"><span>读取 <strong>${result.total}</strong> 局</span><span>新增 <strong>${result.created}</strong></span><span>覆盖 <strong>${result.updated}</strong></span><span>跳过 <strong>${result.skipped}</strong></span></div><p class="preview-entities">${result.new_competitions.length ? '新比赛: '+esc(result.new_competitions.join(', '))+'<br>' : ''}${result.new_players.length ? '新选手: '+esc(result.new_players.join(', ')) : '使用现有选手.'}</p><div class="table-wrap"><table class="import-preview-table"><thead><tr><th>处理</th><th>比赛 / 时间</th><th>选手与最终得分</th></tr></thead><tbody>${result.rounds.map((r) => `<tr><td>${esc(r.action)}</td><td>${esc(r.competition)}<small>${esc(r.played_at.replace('T',' '))}</small></td><td>${r.players.map((p) => `<div>${esc(p.name)} <strong class="${scoreClass(p.total_units)}">${score(p.total_units)}</strong></div>`).join('')}</td></tr>`).join('') || '<tr><td colspan="3">全部对局已存在, 无需重复导入.</td></tr>'}</tbody></table></div><p class="stats-note">${result.rounds.length < result.created+result.updated ? '仅展示前 50 局; 确认将处理全部已检查记录.' : '确认后保存以上记录.'}${result.updated ? ' 覆盖将替换已有对局的时间、归属和分数.' : ''}</p>`;
    $("import-preview").classList.remove("hidden");
    $("commit-import").textContent = result.updated ? `确认导入 (覆盖 ${result.updated} 局)` : "确认导入";
  } catch (error) { showError("import-error",error.message); }
  finally { setImportBusy(false); $("preview-import").textContent = "检查并预览"; }
}
async function commitImport() {
  if (importBusy || !importPreview || !importUpload) return;
  setImportBusy(true); showError("import-error");
  try {
    const result = (await api("/api/import", {method:"POST",body:JSON.stringify({...importUpload,fingerprint:importPreview.fingerprint})})).import;
    $("import-dialog").close(); await loadState(true); toast(`导入完成: 新增 ${result.created} 局, 覆盖 ${result.updated} 局, 跳过 ${result.skipped} 局.`);
  } catch (error) { resetImportPreview(); showError("import-error",error.message); }
  finally { setImportBusy(false); }
}
async function exportTechnical() {
  $("download-technical").disabled = true;
  const query = scopeQuery(), player = $("technical-player").value;
  if (player) query.set("player",player);
  try { await download(`/api/technical.xlsx?${query}`,"scoreboard-technical.xlsx"); toast("技术统计与对手记录已导出."); }
  catch (error) { toast(error.message || "导出失败.",true); }
  finally { $("download-technical").disabled = false; }
}

function openPlayer(id = null) {
  editingPlayer = id ? playerById(id) : null;
  $("player-dialog-title").textContent = editingPlayer ? "修改选手姓名" : "添加选手";
  $("player-name").value = editingPlayer?.name || "";
  showError("player-error"); $("player-dialog").showModal();
  setTimeout(() => $("player-name").focus(), 40);
}
async function savePlayer(event) {
  event.preventDefault();
  const name = $("player-name").value.trim();
  if (!name) { showError("player-error", "请填写选手姓名."); return; }
  $("save-player").disabled = true; showError("player-error");
  try {
    await api(editingPlayer ? `/api/players/${editingPlayer.id}` : "/api/players", {
      method: editingPlayer ? "PATCH" : "POST", body: JSON.stringify({name})
    });
    $("player-dialog").close(); await loadState(true); toast(editingPlayer ? "选手姓名已更新." : "选手已添加.");
  } catch (error) { showError("player-error", error.message); }
  finally { $("save-player").disabled = false; }
}
function confirmAction(title, message, label, action, danger = true) {
  $("confirm-title").textContent = title; $("confirm-message").textContent = message;
  $("confirm-action").textContent = label;
  $("confirm-action").className = `button ${danger ? 'button-danger' : 'button-primary'}`;
  $("confirm-action").disabled = false; showError("confirm-error");
  $("confirm-action").onclick = async () => {
    if (confirmBusy) return;
    confirmBusy = true; $("confirm-action").disabled = true;
    try { await action(); $("confirm-dialog").close(); await loadState(true); }
    catch (error) { showError("confirm-error", error.message); }
    finally { confirmBusy = false; $("confirm-action").disabled = false; }
  };
  $("confirm-dialog").showModal();
}
function deleteSelectedMatches() {
  const matches = data.matches.filter((m) => selectedMatchIds.has(m.id));
  if (!matches.length) return;
  const payload = { matches: matches.map((m) => ({ id:m.id, revision:m.revision })) };
  confirmAction(`删除 ${matches.length} 场对局?`, `所选对局的三位玩家成绩都会被移除, 排行榜和走势图会重新计算.\n此操作无法撤销.`, "删除所选对局", async () => {
    await api("/api/matches/bulk-delete", {method:"POST", body:JSON.stringify(payload)});
    selectedMatchIds.clear(); toast(`已删除 ${matches.length} 场对局.`);
  });
}
function deleteSelectedPlayers() {
  const players = data.players.filter((p) => selectedPlayerIds.has(p.id));
  if (!players.length) return;
  const ids = players.map((p) => p.id), matches = data.matches.filter((m) => m.entries.some((e) => ids.includes(e.player_id)));
  const payload = { ids, matches: matches.map((m) => ({ id:m.id, revision:m.revision })) };
  confirmAction(`删除 ${players.length} 位选手及其对局?`, `${players.map((p) => p.name).join(', ')}\n\n将删除这 ${players.length} 位选手和他们参与的 ${matches.length} 场完整对局, 包含全部日期和停用选手的记录.\n同场其他选手的该局成绩也会移除. 此操作无法撤销.`, "删除选手及对局", async () => {
    await api("/api/players/bulk-delete", {method:"POST", body:JSON.stringify(payload)});
    selectedPlayerIds.clear(); toast(`已删除 ${players.length} 位选手及 ${matches.length} 场对局.`);
  });
}
function openRateSettings() {
  $("global-rate-input").value = data.settings.rate;
  showError("rate-error"); $("rate-dialog").showModal();
}
async function saveRate(event) {
  event.preventDefault();
  const rate = $("global-rate-input").value, parsed = parseCents(rate);
  if (parsed === null || parsed <= 0 || parsed > 1000000) { showError("rate-error", "rate 必须大于 0 且不超过 10,000, 最多两位小数."); return; }
  $("save-rate").disabled = true; showError("rate-error");
  try {
    await api("/api/settings",{method:"PUT",body:JSON.stringify({rate})});
    $("rate-dialog").close(); await loadState(true); toast("全局 rate 已保存, 新比赛使用此倍率.");
  } catch (error) { showError("rate-error",error.message); }
  finally { $("save-rate").disabled = false; }
}
function deleteMatch(id) {
  const match = data.matches.find((m) => m.id === Number(id));
  if (!match) return;
  confirmAction("删除这局?", `比赛 #${String(id).padStart(3,'0')} (${match.entries.map((e)=>e.name).join(', ')}) 将被删除.\n排行榜会扣除本场所有分数, 此操作无法撤销.`, "删除对局", async () => {
    await api(`/api/matches/${id}`, {method:"DELETE", body:JSON.stringify({revision:match.revision})});
    toast("对局已删除, 累计统计已更新.");
  });
}
function togglePlayer(id) {
  const p = playerById(id); if (!p) return;
  confirmAction(p.active ? `停用 ${p.name}?` : `恢复 ${p.name}?`, p.active ? "停用后无法选择该选手参加新比赛. 已有记录和累计分数会保留, 之后可以恢复." : "恢复后, 新比赛的姓名选择框将重新显示这位选手.", p.active ? "停用选手" : "恢复参赛", async () => {
    await api(`/api/players/${p.id}`, {method:"PATCH", body:JSON.stringify({active:!p.active})});
    toast(p.active ? "选手已停用, 历史记录已保留." : "选手已恢复参赛.");
  }, p.active);
}
async function download(url, fallbackName) {
  const response = await fetch(url);
  if (!response.ok) { const json = await response.json(); throw new Error(json.error || "导出失败, 请重试."); }
  downloadBlob(await response.blob(),response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || fallbackName);
}
function downloadBlob(blob, filename) {
  const address = URL.createObjectURL(blob), a = document.createElement("a");
  a.href = address;
  a.download = filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(address), 60000);
}
function renderRankingChoices() {
  const players = stats(filteredMatches());
  $("export-ranking-list").innerHTML = players.map((player) =>
    `<label class="export-ranking-player"><input type="checkbox" data-rank-player="${player.id}"${exportExcludedIds.has(player.id) ? "" : " checked"}>${avatar(player)}<span title="${esc(player.name)}">${esc(player.name)}</span></label>`
  ).join("") || '<span class="export-ranking-empty">暂无选手</span>';
  updateRankingSummary();
}
function updateRankingSummary() {
  const players = stats(filteredMatches());
  const selected = players.filter((player) => !exportExcludedIds.has(player.id)).length;
  $("export-ranking-summary").textContent = `${selected} / ${players.length} 位参与排名`;
}
function selectRankingPlayers(selected) {
  for (const player of stats(filteredMatches())) {
    if (selected) exportExcludedIds.delete(player.id);
    else exportExcludedIds.add(player.id);
  }
  renderRankingChoices();
}
function scopeQuery() {
  const { start, end } = dateRange();
  const query = new URLSearchParams(); if (start) query.set("start",start); if (end) query.set("end",end);
  if (selectedCompetitionIds !== null) {
    if (!selectedCompetitionIds.size) query.set("none","1");
    for (const id of selectedCompetitionIds) query.append("competition",String(id));
  }
  return query;
}
function exportQuery() {
  const query = scopeQuery();
  for (const player of stats(filteredMatches())) {
    if (exportExcludedIds.has(player.id)) query.append("exclude", String(player.id));
  }
  return query;
}
async function exportFile(format) {
  const query = exportQuery();
  $("download-excel").disabled = true; $("download-csv").disabled = true; $("download-details").disabled = true; showError("export-error");
  const path = format === "details" ? "/api/export-details.csv" : `/api/export.${format}`;
  try { await download(`${path}?${query}`, format === "details" ? "scoreboard-round-details.csv" : `scoreboard-match-scores.${format}`); $("export-dialog").close(); toast("统计表已生成."); }
  catch(error) { showError("export-error", error instanceof TypeError ? "无法连接服务, 请重试." : error.message); }
  finally { $("download-excel").disabled=false; $("download-csv").disabled=false; $("download-details").disabled=false; }
}

document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]");
  if (close) {
    if ((saving && close.dataset.close === "match-dialog") || (confirmBusy && close.dataset.close === "confirm-dialog")) return;
    if (((multiSaving || multiReading) && close.dataset.close === "multi-dialog") || (importBusy && close.dataset.close === "import-dialog") || (competitionBusy && close.dataset.close === "competition-dialog") || (moveBusy && close.dataset.close === "move-dialog")) return;
    if (close.dataset.close === "knockout-dialog") { knockoutSavePending = false; knockoutPending = null; }
    $(close.dataset.close).close(); return;
  }
  const go = event.target.closest("[data-go]"); if (go) { switchView(go.dataset.go); return; }
  const nav = event.target.closest("[data-view]"); if (nav) { event.preventDefault(); switchView(nav.dataset.view); return; }
  const action = event.target.closest("[data-action]"); if (!action) return;
  const handlers = {"new-match":()=>openMatch(), "edit-match":()=>openMatch(action.dataset.id), "delete-match":()=>deleteMatch(action.dataset.id), "new-player":()=>openPlayer(), "edit-player":()=>openPlayer(action.dataset.id), "toggle-player":()=>togglePlayer(action.dataset.id),
    "new-competition":()=>openCompetition(), "edit-competition":()=>openCompetition(action.dataset.id), "delete-competition":()=>deleteCompetition(action.dataset.id),
    "competition-round":()=>openMatch(null,Number(action.dataset.id)), "competition-multi":()=>openMultiMatch(null,Number(action.dataset.id)), "competition-results":()=>viewCompetition(action.dataset.id), "all-competitions":()=>viewCompetition()};
  handlers[action.dataset.action]?.();
});
$("new-match-button").onclick = () => view === "players" ? openPlayer() : view === "competitions" ? openCompetition() : openMatch();
$("competition-form").addEventListener("submit",saveCompetition);
$("competition-filter-button").onclick = openCompetitionSelection;
$("competition-selection-form").addEventListener("submit",applyCompetitionSelection);
$("competition-choice-list").addEventListener("change",(event) => {
  const box = event.target.closest("[data-scope-competition]"); if (!box) return;
  const id = Number(box.dataset.scopeCompetition);
  if (box.checked) draftCompetitionIds.add(id); else draftCompetitionIds.delete(id);
  updateCompetitionCount();
});
$("competition-select-all").onclick = () => { draftCompetitionIds = new Set(data.competitions.map((c) => c.id)); renderCompetitionChoices(); };
$("competition-select-none").onclick = () => { draftCompetitionIds.clear(); renderCompetitionChoices(); };
$("bulk-move-matches").onclick = openMove;
$("move-form").addEventListener("submit",saveMove);
$("technical-player").onchange = () => renderOpponents();
$("download-technical").onclick = exportTechnical;
$("import-button").onclick = openImport;
$("import-form").addEventListener("submit",previewImport);
$("commit-import").onclick = commitImport;
for (const id of ["import-file","import-competition","import-mode"]) $(id).addEventListener("change",resetImportPreview);
for (const [id,busy] of [["import-dialog",()=>importBusy],["competition-dialog",()=>competitionBusy],["move-dialog",()=>moveBusy]]) $(id).addEventListener("cancel", (event) => { if (busy()) event.preventDefault(); });
$("download-template").onclick = async () => {
  try { await download("/api/import-template.xlsx","scoreboard-import-template.xlsx"); }
  catch (error) { showError("import-error",error.message || "模板下载失败."); }
};
$("match-form").addEventListener("submit", saveMatch);
$("match-form").addEventListener("input", () => { showError("match-error"); requestKey = newKey(); validateMatch(); });
$("match-form").addEventListener("change", () => {
  if ($("scoring-mode").value === "multi") {
    const cid = Number($("match-competition").value), rate = $("match-rate").value;
    $("match-dialog").close(); openMultiMatch(null,cid); $("multi-rate").value = rate; validateMulti();
  } else validateMatch();
});
$("multi-form").addEventListener("submit",saveMultiMatch);
for (const name of ["input","change"]) $("multi-form").addEventListener(name, () => { showError("multi-error"); multiRequestKey = newKey(); validateMulti(); });
$("multi-kind").addEventListener("change", () => {
  if ($("multi-kind").value === "counts") for (let i=0;i<multiRows.length;i++) if (!$(`multi-base-${i}`).value.trim()) $(`multi-base-${i}`).value = "0";
  validateMulti();
});
$("multi-dialog").addEventListener("cancel",(event) => { if (multiSaving || multiReading) event.preventDefault(); });
$("multi-add").onclick = () => addMultiPlayers();
$("multi-add-all").onclick = () => addMultiPlayers(true);
$("multi-read-file").onclick = () => readMultiScores();
$("multi-read-paste").onclick = () => readMultiScores(true);
$("multi-body").addEventListener("click",(event) => {
  const button = event.target.closest("[data-remove-multi]");
  if (!button || multiSaving || multiReading) return;
  multiRows = readMultiRows(); multiRows.splice(Number(button.dataset.removeMulti),1); multiRequestKey = newKey(); renderMultiRows();
});
$("match-dialog").addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
$("match-dialog").addEventListener("close", () => { knockoutPending = null; knockoutSavePending = false; if ($("knockout-dialog").open) $("knockout-dialog").close(); });
$("confirm-dialog").addEventListener("cancel", (event) => { if (confirmBusy) event.preventDefault(); });
$("knockout-dialog").addEventListener("cancel", () => { knockoutPending = null; knockoutSavePending = false; });
$("knockout-form").addEventListener("submit", finishKnockout);
$("knockout-button").onclick = () => beginKnockouts(false, true);
$("rate-settings-button").onclick = openRateSettings;
$("rate-form").addEventListener("submit",saveRate);
$("autofill-button").onclick = () => {
  const a=parseCents($("base-0").value), b=parseCents($("base-1").value);
  if (a===null || b===null || Math.abs(a+b)>100000000) return;
  $("base-2").value=plainScore(-a-b); requestKey=newKey(); validateMatch(); $("extra-2").focus();
};
$("player-form").addEventListener("submit",savePlayer);
$("period-select").onchange=()=> { $("custom-dates").classList.toggle("hidden",$("period-select").value!=="custom"); page=0; render(); };
$("start-date").onchange=$("end-date").onchange=()=>{ page=0; render(); };
$("history-search").oninput=()=>{ page=0; renderHistory(filteredMatches()); };
$("prev-page").onclick=()=>{ page--; renderHistory(filteredMatches()); };
$("next-page").onclick=()=>{ page++; renderHistory(filteredMatches()); };
$("show-archived").onchange=renderPlayers;
$("history-container").addEventListener("change", (event) => {
  if (event.target.id === "select-page-matches") {
    for (const match of historyMatches().slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE)) {
      if (event.target.checked) selectedMatchIds.add(match.id); else selectedMatchIds.delete(match.id);
    }
  } else if (event.target.dataset.selectMatch) {
    const id = Number(event.target.dataset.selectMatch);
    if (event.target.checked) selectedMatchIds.add(id); else selectedMatchIds.delete(id);
  } else return;
  renderHistory(filteredMatches());
});
$("select-all-matches").onclick = () => { for (const match of historyMatches()) selectedMatchIds.add(match.id); renderHistory(filteredMatches()); };
$("clear-match-selection").onclick = () => { selectedMatchIds.clear(); renderHistory(filteredMatches()); };
$("bulk-delete-matches").onclick = deleteSelectedMatches;
$("players-grid").addEventListener("change", (event) => {
  if (!event.target.dataset.selectPlayer) return;
  const id = Number(event.target.dataset.selectPlayer);
  if (event.target.checked) selectedPlayerIds.add(id); else selectedPlayerIds.delete(id);
  renderPlayers();
});
$("select-all-players").onclick = () => { for (const player of visiblePlayers()) selectedPlayerIds.add(player.id); renderPlayers(); };
$("clear-player-selection").onclick = () => { selectedPlayerIds.clear(); renderPlayers(); };
$("bulk-delete-players").onclick = deleteSelectedPlayers;
$("help-button").onclick=$("sidebar-rules").onclick=()=>$("rules-dialog").showModal();
$("chart-select-button").onclick = openChartSelection;
$("chart-export-button").onclick = exportChartImage;
$("chart-mode").onchange = () => renderChart(filteredMatches(), selectedChartPlayers());
$("chart-selection-form").addEventListener("submit", generateSelectedChart);
$("chart-player-list").addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-chart-player]");
  if (!checkbox) return;
  const id = Number(checkbox.dataset.chartPlayer);
  if (checkbox.checked) draftChartIds.add(id);
  else draftChartIds.delete(id);
  updateChartPlayerCount();
});
$("chart-select-all").onclick = () => selectChartPlayers(true);
$("chart-clear-selection").onclick = () => selectChartPlayers(false);
window.addEventListener("resize", () => {
  if (view === "overview") renderChart(filteredMatches(), selectedChartPlayers());
});
$("export-button").onclick=()=>{
  $("export-description").textContent=`${scopeLabel()}, ${dateRange().label}, 共 ${filteredMatches().length} 局. Excel 和明细 CSV 可用于批量导入.`;
  renderRankingChoices(); showError("export-error"); $("export-dialog").showModal();
};
$("export-ranking-list").addEventListener("change", (event) => {
  const checkbox = event.target.closest("[data-rank-player]");
  if (!checkbox) return;
  const id = Number(checkbox.dataset.rankPlayer);
  if (checkbox.checked) exportExcludedIds.delete(id);
  else exportExcludedIds.add(id);
  updateRankingSummary();
});
$("ranking-select-all").onclick = () => selectRankingPlayers(true);
$("ranking-select-none").onclick = () => selectRankingPlayers(false);
$("download-excel").onclick=()=>exportFile("xlsx");
$("download-csv").onclick=()=>exportFile("csv");
$("download-details").onclick=()=>exportFile("details");
$("backup-button").onclick=async()=>{
  $("backup-button").disabled=true;
  try { await download("/api/backup","scoreboard-backup.sqlite3"); toast("完整数据备份已下载."); }
  catch { toast("备份失败, 请确认服务运行后重试.",true); }
  finally { $("backup-button").disabled=false; }
};
$("retry-button").onclick=()=>loadState(true);
$("menu-button").onclick=()=>{ $("sidebar").classList.add("open"); $("sidebar-scrim").classList.add("open"); };
$("sidebar-scrim").onclick=()=>{ $("sidebar").classList.remove("open"); $("sidebar-scrim").classList.remove("open"); };
window.addEventListener("hashchange",()=>switchView(location.hash.slice(1)));
document.addEventListener("keydown",(event)=>{
  if (event.key.toLowerCase()==="n" && !event.ctrlKey && !event.metaKey && !event.altKey && !document.querySelector("dialog[open]") && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { event.preventDefault(); openMatch(); }
});
switchView(location.hash.slice(1));
loadState(true);
setInterval(()=>{ if (!document.hidden) loadState(); },5000);
