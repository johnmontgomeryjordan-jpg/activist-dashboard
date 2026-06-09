const fmtCap = n => { if(n==null) return "—";
  if(n>=1e12) return "$"+(n/1e12).toFixed(2)+"T"; if(n>=1e9) return "$"+(n/1e9).toFixed(1)+"B";
  if(n>=1e6) return "$"+(n/1e6).toFixed(0)+"M"; return "$"+n; };
const fmtPct = n => n==null? "—" : (n*100).toFixed(1)+"%";
const fmtNum = n => n==null? "—" : (typeof n==="number"? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n);
const fmtDate = s => { if(!s) return ""; try{return new Date(s).toLocaleDateString(undefined,{month:"short",day:"numeric"});}catch(e){return s;} };
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const secUrl = cik => `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(cik)}&type=&dateb=&owner=include&count=40`;

function showTab(name){
  document.getElementById("tab-dashboard").style.display = name==="dashboard"?"block":"none";
  document.getElementById("tab-about").style.display = name==="about"?"block":"none";
  document.getElementById("tabbtn-dashboard").classList.toggle("active", name==="dashboard");
  document.getElementById("tabbtn-about").classList.toggle("active", name==="about");
}
async function loadStatus(){
  try{ const s=await (await fetch("/api/status")).json();
    document.getElementById("status").innerHTML =
      `Universe <b>${s.universe_size}</b> · Threshold <b>${s.threshold}+</b> · Window <b>${s.window_days}d</b> · `+
      `Refresh <b>${s.refresh_minutes}m</b> · Subscribers <b>${s.subscribers}</b> · `+
      `News <b>${s.news_enabled?"on":"off"}</b> · Email <b>${s.email_enabled?"on":"off"}</b>`;
    const at=document.getElementById("aboutThreshold"); if(at) at.textContent=s.threshold;
  }catch(e){}
}

/* ---- News categorization (client-side, from the headline) ---- */
const MOVE_RE = /\b(slid|slide|slides|slip|slips|slipped|fall|falls|fell|drop|drops|dropped|dip|dips|dipped|sink|sinks|sank|slump|slumps|slumped|decline|declines|declined|retreat|retreats|retreated|plunge|plunges|tumble|tumbles|plummets|sell-?off)\b/;
const CAT_ACTIVIST = ["activist","proxy fight","proxy battle","proxy contest","13d","schedule 13d",
  "board seat","board seats","director nominee","nominates","builds stake","raises stake",
  "takes stake","boosts stake","elliott management","starboard","trian","jana partners",
  "third point","carl icahn","icahn","nelson peltz","valueact","value act","engine no",
  "ancora","politan","sachem head","legion partners","short seller","short-seller"];
const CAT_PROXY = ["glass lewis","proxy advisor","proxy adviser","institutional shareholder services",
  "iss recommends","iss advises","iss backs","recommends against","withhold vote","withhold votes"];
const CAT_EXEC = ["steps down","stepping down","steps aside","to resign","resigns","resigned",
  "ousted","ousts","departs","departure","interim ceo","interim cfo","names ceo","new ceo",
  "appoints ceo","names new chief","leadership change","management shake","shake-up","shakeup",
  "reshuffle","exits as ceo","exits as cfo"];
const CAT_MARKET = ["nasdaq","s&p","dow jones"," dow ","wall street","stock market","stocks ",
  "futures","treasury","global markets","indexes","indices"];
// Display order + labels. High-value buckets first; the last three default collapsed.
const CATS = [
  ["activist","Activist activity"],
  ["proxy","Proxy advisors"],
  ["exec","Executive changes"],
  ["movers","Price movers"],
  ["distress","Earnings & distress"],
  ["market","Market"],
];
const OPEN_BY_DEFAULT = new Set(["activist","proxy","exec"]);

function newsCategory(h){
  const t = " " + (h||"").toLowerCase() + " ";
  const has = arr => arr.some(k => t.includes(k));
  if(has(CAT_PROXY)) return "proxy";
  if(has(CAT_ACTIVIST)) return "activist";
  if(has(CAT_EXEC)) return "exec";
  const moved = MOVE_RE.test(t);
  if(moved && has(CAT_MARKET)) return "market";
  if(moved) return "movers";
  return "distress";
}
function newsItemHtml(n){
  return `<div class="item">
    <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a>
    <div class="meta"><span class="tag">${esc(n.source)||"news"}</span><span>${fmtDate(n.published_at)}</span></div></div>`;
}
function toggleAcc(el){ el.parentElement.classList.toggle("open"); }

async function loadFeed(){
  try{ const d=await (await fetch("/api/feed")).json();
    // ---- News, grouped into expandable categories ----
    const nf=document.getElementById("newsFeed");
    const news = d.news||[];
    if(!news.length){ nf.innerHTML = `<div class="empty">No headlines yet.</div>`; }
    else{
      const groups = {}; CATS.forEach(([k])=>groups[k]=[]);
      news.forEach(n=>{ (groups[newsCategory(n.headline)] ||= []).push(n); });
      let html = "";
      CATS.forEach(([key,label])=>{
        const items = groups[key]||[];
        if(!items.length) return;
        const open = OPEN_BY_DEFAULT.has(key) ? " open" : "";
        html += `<div class="acc${open}">
          <div class="acc-head" onclick="toggleAcc(this)">
            <span class="acc-title"><span class="caret">▸</span>${esc(label)}</span>
            <span class="acc-count">${items.length}</span>
          </div>
          <div class="acc-body">${items.map(newsItemHtml).join("")}</div>
        </div>`;
      });
      nf.innerHTML = html || `<div class="empty">No headlines yet.</div>`;
    }
    // ---- Filings (unchanged for now) ----
    const ff=document.getElementById("filingFeed");
    ff.innerHTML = d.filings.length ? d.filings.map(f=>{
      const sigs=(f.signals||"").split(",").filter(Boolean).map(s=>`<span class="tag sig">${esc(s.replace(/_/g," "))}</span>`).join(" ");
      return `<div class="item"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.company)} — ${esc(f.title)}</a>
        <div class="meta"><span class="tag">${esc(f.ticker)||esc(f.form)}</span><span>${fmtDate(f.filed_at)}</span>${sigs}</div></div>`;
    }).join("") : `<div class="empty">No filings yet.</div>`;
  }catch(e){}
}
async function loadShortlist(){
  try{ const d=await (await fetch("/api/shortlist")).json();
    const tb=document.getElementById("shortlist");
    if(!d.companies.length){ tb.innerHTML=`<tr><td colspan="7" class="empty">No companies flagged yet. Scores build as data loads.</td></tr>`; return; }
    tb.innerHTML = d.companies.map((c,i)=>{
      const cls=c.score>=7?"hot":c.score>=5?"warn":"mid";
      const chg = weekChip(c.week_change);
      const link=c.top_item_url?`<span class="pill-link"><a href="${esc(c.top_item_url)}" target="_blank" rel="noopener">${esc(c.top_item_title)||"view"}</a></span>`:"—";
      const co=`<span class="co-link" onclick="openCompany('${esc(c.cik)}')">${esc(c.company)}</span>`;
      return `<tr><td>${i+1}</td>
        <td>${co}<div class="meta">${esc(c.ticker)}</div></td>
        <td class="mcap">${fmtCap(c.market_cap)}</td>
        <td class="score ${cls}">${c.score}${chg}</td>
        <td class="signals">${esc(c.signals)}</td>
        <td>${link}</td>
        <td class="mcap">${esc(c.first_flagged)}</td></tr>`;
    }).join("");
  }catch(e){}
}
async function openCompany(cik){
  const ov=document.getElementById("overlay"); ov.classList.add("open");
  document.getElementById("mTitle").textContent="Loading…";
  document.getElementById("mSub").textContent=""; document.getElementById("mScore").textContent="";
  document.getElementById("mBody").innerHTML=`<div class="empty">Loading company profile…</div>`;
  try{
    const d=await (await fetch("/api/company?cik="+encodeURIComponent(cik))).json();
    if(!d.ok){ document.getElementById("mBody").innerHTML=`<div class="empty">Could not load this company.</div>`; return; }
    document.getElementById("mTitle").textContent = d.company + (d.ticker?` (${d.ticker})`:"");
    const o=d.overview||{};
    document.getElementById("mSub").innerHTML =
      [o.sector,o.industry,o.exchange].filter(Boolean).map(esc).join(" · ") +
      (d.first_flagged?` · first flagged ${esc(d.first_flagged)}`:"") +
      (d.week_change!=null?` · ${d.week_change>0?"▲":d.week_change<0?"▼":"±"}${Math.abs(d.week_change)} vs last week`:"");
    const sc=d.score, cls=sc>=7?"hot":sc>=5?"warn":"mid";
    const sb=document.getElementById("mScore"); sb.textContent=sc; sb.className="score scorebadge "+cls;
    const f=d.financials||{};
    const sigs=(d.signals||"").split(" + ").filter(Boolean).map(s=>`<span class="tag sig">${esc(s)}</span>`).join(" ");
    const kv=(k,v)=>`<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    const filings=(d.filings||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.company)} — ${esc(x.title)}</a>
        <div class="meta"><span class="tag">${esc(x.form)}</span><span>${fmtDate(x.filed_at)}</span></div></div>`).join("") || `<div class="empty">No recent filings on record.</div>`;
    const news=(d.news||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>
        <div class="meta"><span class="tag">${esc(x.source)||"news"}</span><span>${fmtDate(x.published_at)}</span></div></div>`).join("") || `<div class="empty">No recent matched news.</div>`;
    document.getElementById("mBody").innerHTML = `
      ${o.description?`<div class="mh3">Overview</div><div class="desc">${esc(o.description)}</div>`:""}
      <div class="mh3">Why it's flagged</div><div class="siglist">${sigs||"—"}</div>
      <div class="mh3">Financials</div>
      <div class="grid">
        ${kv("Market cap", fmtCap(d.market_cap))}
        ${kv("Price / book", fmtNum(f.pb_ratio))}
        ${kv("P / E", fmtNum(f.pe_ratio))}
        ${kv("Revenue", fmtCap(f.revenue))}
        ${kv("Revenue growth", fmtPct(f.revenue_growth))}
        ${kv("Operating margin", fmtPct(f.operating_margin))}
        ${kv("Profit margin", fmtPct(f.profit_margin))}
        ${kv("Return on assets", fmtPct(f.roa))}
        ${kv("Return on equity", fmtPct(f.return_on_equity))}
        ${kv("SG&amp;A % of revenue", fmtPct(f.sga_pct))}
        ${kv("Cash / assets", fmtPct(f.cash_to_assets))}
        ${kv("Debt / assets", fmtPct(f.debt_to_assets))}
        ${kv("Dividend yield", fmtPct(f.dividend_yield))}
        ${kv("52-wk range", (f.week52_low!=null&&f.week52_high!=null)?("$"+fmtNum(f.week52_low)+" – $"+fmtNum(f.week52_high)):"—")}
        ${kv("Analyst target", f.analyst_target!=null?("$"+fmtNum(f.analyst_target)):"—")}
      </div>
      <div class="mh3">Recent SEC filings</div><div class="dlist">${filings}</div>
      <div class="mh3">Recent news</div><div class="dlist">${news}</div>
      <div style="margin-top:18px;"><a class="pill-link" href="${secUrl(cik)}" target="_blank" rel="noopener"><span style="color:var(--accent)">View all SEC filings on EDGAR →</span></a></div>
    `;
  }catch(e){ document.getElementById("mBody").innerHTML=`<div class="empty">Network error loading company.</div>`; }
}
function closeCompany(){ document.getElementById("overlay").classList.remove("open"); }
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeCompany(); });
async function manualRefresh(){
  const b=document.getElementById("refreshBtn"); b.disabled=true; b.textContent="Refreshing…";
  try{ await fetch("/api/refresh",{method:"POST"}); }catch(e){}
  await refreshAll(); b.disabled=false; b.textContent="↻ Refresh now";
}
async function subscribe(){
  const email=document.getElementById("emailInput").value, msg=document.getElementById("subMsg");
  try{ const r=await fetch("/api/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email})});
    const d=await r.json(); msg.textContent=d.ok?d.message:(d.error||"Error"); msg.className="msg "+(d.ok?"ok":"err"); if(d.ok)loadStatus();
  }catch(e){ msg.textContent="Network error"; msg.className="msg err"; }
}
async function unsubscribe(){
  const email=document.getElementById("emailInput").value, msg=document.getElementById("subMsg");
  try{ const r=await fetch("/api/unsubscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email})});
    const d=await r.json(); msg.textContent=d.message||"Done"; msg.className="msg ok"; loadStatus();
  }catch(e){ msg.textContent="Network error"; msg.className="msg err"; }
}
async function sendTestDigest(){
  const msg=document.getElementById("subMsg"); msg.textContent="Sending…"; msg.className="msg";
  try{ const r=await fetch("/api/send-test-digest",{method:"POST"}); const d=await r.json();
    msg.textContent=d.message||(d.ok?"Sent.":"Error"); msg.className="msg "+(d.ok?"ok":"err");
  }catch(e){ msg.textContent="Network error"; msg.className="msg err"; }
}
function weekChip(ch){
  if(ch==null) return ` <span class="chg new" title="newly tracked">new</span>`;
  if(ch>0) return ` <span class="chg up" title="vs last week">▲${ch}</span>`;
  if(ch<0) return ` <span class="chg down" title="vs last week">▼${Math.abs(ch)}</span>`;
  return ` <span class="chg flat" title="vs last week">±0</span>`;
}
function exportCsv(){ window.open("/api/shortlist.csv","_blank"); }

async function refreshAll(){
  await Promise.all([loadStatus(),loadFeed(),loadShortlist()]);
  document.getElementById("updated").textContent="Last updated "+new Date().toLocaleTimeString();
}
refreshAll(); setInterval(refreshAll, 5*60*1000);
