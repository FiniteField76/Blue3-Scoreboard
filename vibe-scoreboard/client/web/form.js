"use strict";
(function(root) {
  function cents(value) {
    const text=String(value ?? "").trim();
    if(text.length>32 || !/^[+-]?(?:\d+(?:\.\d{1,2})?|\.\d{1,2})$/.test(text)) return null;
    const negative=text.startsWith("-"), parts=text.replace(/^[+-]/,"").split(".");
    const result=Number(parts[0]||"0")*100+Number((parts[1]||"").padEnd(2,"0"));
    return Number.isSafeInteger(result) && result<=100000000 ? (negative?-result:result) : null;
  }
  function count(value) {
    const text=String(value ?? "").trim();
    if(!/^\+?\d{1,7}$/.test(text)) return null;
    const result=Number(text);
    return result<=1000000 ? result : null;
  }
  function format(units,scale=100,signed=true) {
    return (units>0&&signed?"+":"")+(units/scale).toLocaleString("en-US",{maximumFractionDigits:4});
  }
  function validate(draft, allowedIds) {
    let balance=0;
    const entries=draft.entries||[], ids=entries.map((entry)=>Number(entry.player_id));
    const bases=entries.map((entry)=>cents(entry.base)), rate=cents(draft.rate ?? "1");
    const fail=(error)=>({valid:false,error,balance:bases.every((n)=>n!==null)?bases.reduce((a,b)=>a+b,0):null,totals:[]});
    if(!Number.isInteger(Number(draft.competition_id)) || Number(draft.competition_id)<=0) return fail("请选择所属赛季");
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(draft.played_at||"") || Number.isNaN(Date.parse(draft.played_at))) return fail("请填写有效的对局时间");
    if(!entries.length || entries.length>200) return fail("请添加 1 到 200 位选手");
    if(ids.some((id)=>!Number.isInteger(id)||id<=0|| (allowedIds && !allowedIds.has(id)))) return fail("请选择有效的选手");
    if(new Set(ids).size!==ids.length) return fail("同一场不能重复选择选手");
    if(entries.some((entry)=>count(entry.battle_count)===null)) return fail("半庄数须为 0 到 1,000,000 的整数");
    if(bases.some((base)=>base===null)) return fail("请填写每位选手的分数, 最多两位小数");
    if(rate===null || rate<=0 || rate>1000000) return fail("rate 须大于 0 且不超过 10,000, 最多两位小数");
    balance=bases.reduce((a,b)=>a+b,0);
    if(balance!==0) return fail("分数合计须为 0, 当前差额 "+format(balance));
    const totals=bases.map((base)=>base*rate);
    if(totals.some((value)=>Math.abs(value)>10000000000)) return fail("乘 rate 后, 每位选手的得分不能超过 1,000,000");
    return {valid:true,error:"",balance,totals};
  }
  const api={cents,count,format,validate};
  if(typeof module!=="undefined" && module.exports) module.exports=api;
  else root.EntryForm=api;
})(globalThis);
