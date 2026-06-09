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
  "takes stake","boosts stake","elliott management","starboard","trian","jana partners","jana",
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
// Display order + labels (high-value buckets first).
const CATS = [
  ["activist","Activist activity"],
  ["proxy","Proxy advisors"],
  ["exec","Executive changes"],
  ["movers","Price movers"],
  ["distress","Earnings & distress"],
  ["market","Market"],
];
// Filing categories, derived from the EDGAR signal tags.
const FILING_CATS = [
  ["exec","Executive changes"],
  ["earn","Earnings & guidance"],
  ["impair","Impairments & write-downs"],
  ["restr","Restructuring & layoffs"],
  ["other","Other filings"],
];

let NEWS_GROUPS = {};
let FILING_GROUPS = {};

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
function filingCategory(f){
  const s = (f.signals||"").toLowerCase();
  if(/ceo_departure|leadership_change/.test(s)) return "exec";
  if(/earnings_miss|results_update/.test(s)) return "earn";
  if(/impairment/.test(s)) return "impair";
  if(/layoff|restructuring/.test(s)) return "restr";
  return "other";
}
function newsModalRow(n){
  return `<div class="row2"><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a>
    <div class="meta"><span class="tag">${esc(n.source)||"news"}</span><span>${fmtDate(n.published_at)}</span></div></div>`;
}
function filingModalRow(f){
  const sigs=(f.signals||"").split(",").filter(Boolean).map(s=>`<span class="tag sig">${esc(s.replace(/_/g," "))}</span>`).join(" ");
  return `<div class="row2"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.company)} — ${esc(f.title)}</a>
    <div class="meta"><span class="tag">${esc(f.ticker)||esc(f.form)}</span><span>${fmtDate(f.filed_at)}</span>${sigs}</div></div>`;
}
function catRowsHtml(cats, groups, opener){
  let html="";
  cats.forEach(([key,label])=>{
    const items=groups[key]||[]; if(!items.length) return;
    html += `<div class="catrow" onclick="${opener}('${key}')">
      <span class="catname">${esc(label)}</span>
      <span class="catright"><span class="acc-count">${items.length}</span><span class="chev">›</span></span>
    </div>`;
  });
  return html;
}
function openListModal(title, rows){
  document.getElementById("mTitle").textContent = title;
  document.getElementById("mSub").textContent = "";
  const sb=document.getElementById("mScore"); sb.textContent=""; sb.className="score scorebadge";
  document.getElementById("mBody").innerHTML = `<div class="dlist">${rows||`<div class="empty">No items.</div>`}</div>`;
  document.getElementById("overlay").classList.add("open");
}
function openNewsCat(key){
  const items = NEWS_GROUPS[key]||[];
  const label = (CATS.find(c=>c[0]===key)||["",key])[1];
  openListModal(`${label} — ${items.length} headline${items.length===1?"":"s"}`,
                items.map(newsModalRow).join(""));
}
function openFilingCat(key){
  const items = FILING_GROUPS[key]||[];
  const label = (FILING_CATS.find(c=>c[0]===key)||["",key])[1];
  openListModal(`${label} — ${items.length} filing${items.length===1?"":"s"}`,
                items.map(filingModalRow).join(""));
}

/* ---- Top 5 "most relevant & recent": take recent items, then float the
   high-value categories up so activist/exec stories aren't buried. ---- */
const NEWS_PRIORITY = {activist:0, proxy:1, exec:2, movers:3, distress:4, market:5};
const FILING_PRIORITY = {exec:0, earn:1, impair:2, restr:3, other:4};
function toplineNews(news){
  return [...news]
    .sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||"")).slice(0,12)
    .sort((a,b)=> (NEWS_PRIORITY[newsCategory(a.headline)]-NEWS_PRIORITY[newsCategory(b.headline)])
                  || (b.published_at||"").localeCompare(a.published_at||"")).slice(0,5);
}
function toplineFilings(filings){
  return [...filings]
    .sort((a,b)=>(b.filed_at||"").localeCompare(a.filed_at||"")).slice(0,12)
    .sort((a,b)=> (FILING_PRIORITY[filingCategory(a)]-FILING_PRIORITY[filingCategory(b)])
                  || (b.filed_at||"").localeCompare(a.filed_at||"")).slice(0,5);
}
function newsItemRow(n){
  return `<div class="item"><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a>
    <div class="meta"><span class="tag">${esc(n.source)||"news"}</span><span>${fmtDate(n.published_at)}</span></div></div>`;
}
function filingItemRow(f){
  const sigs=(f.signals||"").split(",").filter(Boolean).map(s=>`<span class="tag sig">${esc(s.replace(/_/g," "))}</span>`).join(" ");
  return `<div class="item"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.company)} — ${esc(f.title)}</a>
    <div class="meta"><span class="tag">${esc(f.ticker)||esc(f.form)}</span><span>${fmtDate(f.filed_at)}</span>${sigs}</div></div>`;
}
function renderTicker(news){
  const t=document.getElementById("ticker"); if(!t) return;
  const items=[...news].sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||"")).slice(0,20);
  if(!items.length){ t.style.display="none"; return; }
  t.style.display="block";
  const seq=items.map(n=>`<span class="ticker-item"><span class="ticker-dot">●</span> <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a></span>`).join("");
  const dur=Math.max(40, items.length*5);
  t.innerHTML=`<div class="ticker-track" style="animation-duration:${dur}s">${seq}${seq}</div>`;
}

async function loadFeed(){
  try{ const d=await (await fetch("/api/feed")).json();
    // ---- News: top-5 strip + click-to-open categories ----
    const news = d.news||[];
    NEWS_GROUPS={}; CATS.forEach(([k])=>NEWS_GROUPS[k]=[]);
    news.forEach(n=>{ (NEWS_GROUPS[newsCategory(n.headline)] ||= []).push(n); });
    const nf=document.getElementById("newsFeed");
    nf.innerHTML = news.length
      ? `<div class="topline"><div class="topline-h">★ Top 5 — most relevant &amp; recent</div>`
        + toplineNews(news).map(newsItemRow).join("") + `</div>`
        + `<div class="cat-h">Browse by category</div>`
        + catRowsHtml(CATS, NEWS_GROUPS, "openNewsCat")
      : `<div class="empty">No headlines yet.</div>`;
    // ---- Filings: top-5 strip + grouped by type ----
    const filings = d.filings||[];
    FILING_GROUPS={}; FILING_CATS.forEach(([k])=>FILING_GROUPS[k]=[]);
    filings.forEach(f=>{ (FILING_GROUPS[filingCategory(f)] ||= []).push(f); });
    const ff=document.getElementById("filingFeed");
    ff.innerHTML = filings.length
      ? `<div class="topline"><div class="topline-h">★ Top 5 — most relevant &amp; recent</div>`
        + toplineFilings(filings).map(filingItemRow).join("") + `</div>`
        + `<div class="cat-h">Browse by type</div>`
        + catRowsHtml(FILING_CATS, FILING_GROUPS, "openFilingCat")
      : `<div class="empty">No filings yet.</div>`;
    // ---- Broadcast ticker ----
    renderTicker(news);
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
    // Evidence cards: value + peer context + source (with link for events).
    const ev=d.evidence||[];
    const evHtml = ev.length ? ev.map(e=>{
        const src = e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source)} ↗</a>` : esc(e.source);
        const val = e.value ? `<span class="evval">${esc(e.value)}</span>` : "";
        const ctx = e.context ? `${esc(e.context)} · ` : "";
        return `<div class="evrow"><div class="evtop"><span class="evlabel">${esc(e.label)}</span>${val}</div>
          <div class="evctx">${ctx}<span class="evsrc">${src}</span></div></div>`;
      }).join("") : `<div class="siglist">${sigs||"—"}</div>`;
    // External quick-links built from ticker / CIK / company name.
    const tk=d.ticker;
    const L=[];
    if(tk) L.push(`<a class="extlink" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">Yahoo Finance ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||tk||"")+" stock")}" target="_blank" rel="noopener">Google ↗</a>`);
    L.push(`<a class="extlink" href="${secUrl(cik)}" target="_blank" rel="noopener">SEC EDGAR ↗</a>`);
    if(o.website) L.push(`<a class="extlink" href="${esc(o.website)}" target="_blank" rel="noopener">Company site ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||"")+" investor relations")}" target="_blank" rel="noopener">IR / contacts ↗</a>`);
    const linkBar=`<div class="links">${L.join("")}</div>`;
    const kv=(k,v)=>`<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    const filings=(d.filings||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.company)} — ${esc(x.title)}</a>
        <div class="meta"><span class="tag">${esc(x.form)}</span><span>${fmtDate(x.filed_at)}</span></div></div>`).join("") || `<div class="empty">No recent filings on record.</div>`;
    const news=(d.news||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>
        <div class="meta"><span class="tag">${esc(x.source)||"news"}</span><span>${fmtDate(x.published_at)}</span></div></div>`).join("") || `<div class="empty">No recent matched news.</div>`;
    document.getElementById("mBody").innerHTML = `
      ${linkBar}
      ${o.description?`<div class="mh3">Overview</div><div class="desc">${esc(o.description)}</div>`:""}
      <div class="mh3">Why it's flagged</div><div class="evlist">${evHtml}</div>
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
