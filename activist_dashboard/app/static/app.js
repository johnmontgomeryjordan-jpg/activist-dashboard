const fmtCap = n => { if(n==null) return "—";
  if(n>=1e12) return "$"+(n/1e12).toFixed(2)+"T"; if(n>=1e9) return "$"+(n/1e9).toFixed(1)+"B";
  if(n>=1e6) return "$"+(n/1e6).toFixed(0)+"M"; return "$"+n; };
const fmtPct = n => n==null? "—" : (n*100).toFixed(1)+"%";
const fmtNum = n => n==null? "—" : (typeof n==="number"? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n);
const fmtDate = s => { if(!s) return ""; try{return new Date(s).toLocaleDateString(undefined,{month:"short",day:"numeric"});}catch(e){return s;} };
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const secUrl = cik => `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(cik)}&type=&dateb=&owner=include&count=40`;

function showTab(name){
  ["dashboard","about","watchlist"].forEach(n=>{
    const t=document.getElementById("tab-"+n); if(t) t.style.display = n===name?"block":"none";
    const b=document.getElementById("tabbtn-"+n); if(b) b.classList.toggle("active", n===name);
  });
  if(name==="watchlist") loadWatchlist();
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
let WATCHLIST_SET = new Set();
let COMPANY_INFO = {};
let CURRENT_MODAL_CIK = null;
function regInfo(c){ if(c && c.cik) COMPANY_INFO[c.cik] = {ticker:c.ticker, company:c.company}; }

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
    renderNewRising(d.companies||[]);
    const tb=document.getElementById("shortlist");
    if(!d.companies.length){ tb.innerHTML=`<tr><td colspan="7" class="empty">No companies flagged yet. Scores build as data loads.</td></tr>`; return; }
    tb.innerHTML = d.companies.map((c,i)=>{
      regInfo(c);
      const chg = weekChip(c.week_change);
      const link=c.top_item_url?`<span class="pill-link"><a href="${esc(c.top_item_url)}" target="_blank" rel="noopener">${esc(c.top_item_title)||"view"}</a></span>`:"—";
      const star=`<span class="star" onclick="event.stopPropagation();toggleStar('${esc(c.cik)}', this)" title="Add to watchlist">${WATCHLIST_SET.has(c.cik)?'★':'☆'}</span>`;
      const co=`${star}<span class="co-link" onclick="openCompany('${esc(c.cik)}')">${esc(c.company)}</span>`;
      return `<tr><td>${i+1}</td>
        <td>${co}<div class="meta">${esc(c.ticker)}</div></td>
        <td class="mcap">${fmtCap(c.market_cap)}</td>
        <td class="vcell" title="raw signal score ${c.score}">${vulnChip(c.vuln)}${chg}</td>
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
    regInfo({cik, ticker:d.ticker, company:d.company}); CURRENT_MODAL_CIK=cik;
    document.getElementById("mTitle").textContent = d.company + (d.ticker?` (${d.ticker})`:"");
    const o=d.overview||{};
    document.getElementById("mSub").innerHTML =
      [o.sector,o.industry,o.exchange].filter(Boolean).map(esc).join(" · ") +
      (d.first_flagged?` · first flagged ${esc(d.first_flagged)}`:"") +
      (d.week_change!=null?` · ${d.week_change>0?"▲":d.week_change<0?"▼":"±"}${Math.abs(d.week_change)} vs last week`:"");
    // Header badge now shows the 0–100 vulnerability percentile.
    const vi=vulnInfo(d.vuln);
    const sb=document.getElementById("mScore");
    sb.textContent=(d.vuln==null?"—":d.vuln); sb.className="score scorebadge"; sb.style.color=vi.col;
    const f=d.financials||{};
    const sigs=(d.signals||"").split(" + ").filter(Boolean).map(s=>`<span class="tag sig">${esc(s)}</span>`).join(" ");

    // Single evidence card.
    const evCard = e=>{
        const src = e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source||"source")} ↗</a>` : esc(e.source||"");
        const val = e.value ? `<span class="evval">${esc(e.value)}</span>` : "";
        const ctxLine = e.context ? `<div class="evctx">${esc(e.context)}</div>` : "";
        const mp=[]; if(e.inputs) mp.push(esc(e.inputs)); if(e.period) mp.push(esc(e.period));
        const mathLine = mp.length ? `<div class="evmath">${mp.join(" · ")}</div>` : "";
        const srcLine = (e.source||e.url) ? `<div class="evsrc">Source: ${src}</div>` : "";
        return `<div class="evrow"><div class="evtop"><span class="evlabel">${esc(e.label)}</span>${val}</div>
          ${ctxLine}${mathLine}${srcLine}</div>`;
      };
    // Group evidence into the five activist pillars + catalysts.
    const ev=d.evidence||[];
    const groups={}; ev.forEach(e=>{ const p=PILLAR_OF[e.key]||"event"; (groups[p]=groups[p]||[]).push(e); });
    const pillarHtml = ev.length ? PILLAR_ORDER.filter(p=>groups[p]).map(p=>{
        const m=PILLAR_META[p];
        return `<div class="pillar"><div class="pillar-h"><span class="pillar-t">${m.t}</span>
            <span class="pillar-n">${groups[p].length}</span></div>
          <div class="pillar-d">${m.d}</div>
          <div class="evlist">${groups[p].map(evCard).join("")}</div></div>`;
      }).join("") : `<div class="siglist">${sigs||"—"}</div>`;

    // Scorecard header: gauge + return-vs-S&P + key facts.
    const rankLabel = d.vuln!=null ? `<b>${vulnBand(d.vuln)}</b> activist exposure` : "Vulnerability score pending next refresh";
    const tsr=d.tsr||{}; let tsrPanel="";
    if(tsr.tsr_1y!=null){
      const gap=tsr.gap, gcol=(gap!=null)?(gap<0?"var(--hot)":"var(--ok)"):"var(--muted)";
      const gtxt=(gap!=null)?((gap>0?"+":"")+(gap*100).toFixed(0)+" pts"):"—";
      tsrPanel=`<div class="tsr-panel"><div class="tsr-h">1-yr total return vs S&amp;P 500</div>
        <div class="tsr-grid">
          <div><div class="tsr-k">This stock</div><div class="tsr-v">${fmtPct(tsr.tsr_1y)}</div></div>
          <div><div class="tsr-k">S&amp;P 500</div><div class="tsr-v">${fmtPct(tsr.spy_1y)}</div></div>
          <div><div class="tsr-k">Gap</div><div class="tsr-v" style="color:${gcol}">${gtxt}</div></div>
        </div></div>`;
    }
    const scHtml=`<div class="scorecard">
      <div class="sc-gauge">${gaugeSvg(d.vuln)}<div class="sc-rank">${rankLabel}</div></div>
      <div class="sc-side">${tsrPanel}
        <div class="sc-facts">
          ${kvMini("Market cap", fmtCap(d.market_cap))}
          ${kvMini("Price / book", fmtNum(f.pb_ratio))}
          ${kvMini("Signal score", d.score!=null?d.score:"—")}
        </div></div></div>`;
    // Governance badge strip.
    const g=d.governance||{};
    const gbadge=(on,txt)=>`<span class="gov-badge ${on?'on':'off'}">${on?'●':'○'} ${txt}</span>`;
    const anyGov=g.classified_board||g.poison_pill||g.dual_class;
    const govRow=`<div class="gov-row">${gbadge(g.classified_board,"Classified board")}${gbadge(g.poison_pill,"Poison pill")}${gbadge(g.dual_class,"Dual-class stock")}`+
      (g.proxy_url?`<a class="extlink" href="${esc(g.proxy_url)}" target="_blank" rel="noopener">DEF 14A ↗</a>`:"")+
      (!anyGov && !g.proxy_url?`<span class="gov-note">proxy not yet parsed</span>`:"")+`</div>`;
    // External quick-links built from ticker / CIK / company name.
    const tk=d.ticker;
    const L=[];
    if(tk) L.push(`<a class="extlink" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">Yahoo Finance ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||tk||"")+" stock")}" target="_blank" rel="noopener">Google ↗</a>`);
    L.push(`<a class="extlink" href="${secUrl(cik)}" target="_blank" rel="noopener">SEC EDGAR ↗</a>`);
    if(o.website) L.push(`<a class="extlink" href="${esc(o.website)}" target="_blank" rel="noopener">Company site ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||"")+" investor relations")}" target="_blank" rel="noopener">IR / contacts ↗</a>`);
    const linkBar=`<div class="links">${L.join("")}</div>`;
    const warn = d.active_situation ? `<div class="modal-warn">⚠ Activist already engaged — likely too late to pitch proactively.</div>` : "";
    const starBtn = `<button class="ghost wl-star-btn" id="mStarBtn" onclick="toggleStar('${esc(cik)}', this)">${WATCHLIST_SET.has(cik)?'★ On watchlist':'☆ Add to watchlist'}</button>`;
    const kv=(k,v)=>`<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    const filings=(d.filings||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.company)} — ${esc(x.title)}</a>
        <div class="meta"><span class="tag">${esc(x.form)}</span><span>${fmtDate(x.filed_at)}</span></div></div>`).join("") || `<div class="empty">No recent filings on record.</div>`;
    const news=(d.news||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>
        <div class="meta"><span class="tag">${esc(x.source)||"news"}</span><span>${fmtDate(x.published_at)}</span></div></div>`).join("") || `<div class="empty">No recent matched news.</div>`;
    document.getElementById("mBody").innerHTML = `
      ${warn}
      <div class="wl-star-row">${starBtn}</div>
      ${scHtml}
      ${linkBar}
      ${o.description?`<div class="mh3">Overview</div><div class="desc">${esc(o.description)}</div>`:""}
      <div class="mh3">Why it's flagged</div><div class="pillars">${pillarHtml}</div>
      <div class="mh3">Governance</div>${govRow}
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
/* ---- Vulnerability score helpers (0–100 percentile) ---- */
function vulnInfo(v){
  if(v==null) return {col:"var(--muted)", cls:"v0"};
  if(v>=75) return {col:"var(--hot)", cls:"v3"};
  if(v>=50) return {col:"var(--warn)", cls:"v2"};
  if(v>=25) return {col:"var(--accent)", cls:"v1"};
  return {col:"var(--muted)", cls:"v0"};
}
function vulnChip(v){
  const i=vulnInfo(v);
  return `<span class="vchip ${i.cls}">${v==null?"—":v}</span>`;
}
function vulnBand(v){
  if(v==null) return "Unscored";
  if(v>=75) return "Severe";
  if(v>=50) return "High";
  if(v>=25) return "Elevated";
  return "Moderate";
}
function gaugeSvg(v){
  const val=(v==null)?0:Math.max(0,Math.min(100,v));
  const C=Math.PI*84, on=(val/100)*C, col=vulnInfo(v).col;
  return `<svg viewBox="0 0 200 118" class="gauge" role="img" aria-label="vulnerability ${val} of 100">
    <path d="M16,102 A84,84 0 0 1 184,102" fill="none" stroke="var(--line)" stroke-width="15" stroke-linecap="round"/>
    <path d="M16,102 A84,84 0 0 1 184,102" fill="none" stroke="${col}" stroke-width="15" stroke-linecap="round" stroke-dasharray="${on.toFixed(1)} ${(C+4).toFixed(1)}"/>
    <text x="100" y="88" text-anchor="middle" class="gnum" fill="${col}">${v==null?"—":val}</text>
    <text x="100" y="108" text-anchor="middle" class="glabel">/ 100 vulnerability</text>
  </svg>`;
}
function kvMini(k,v){ return `<div class="kvm"><div class="kvm-k">${k}</div><div class="kvm-v">${v}</div></div>`; }
const PILLAR_OF={
  cheap_abs:"value", cheap_pb:"value",
  weak_tsr_1y:"perf", weak_tsr_3y:"perf",
  low_margin:"ops", low_roa:"ops", weak_growth:"ops", high_sga:"ops",
  cash_hoard:"capital", underlevered:"capital",
  gov_classified:"gov", gov_poison:"gov", gov_dual:"gov",
  ceo_departure:"event", earnings_miss:"event", impairment:"event",
  layoffs:"event", leadership_change:"event", results_update:"event", news_negative:"event"
};
const PILLAR_META={
  value:{t:"Valuation gap", d:"Trading cheap relative to assets or peers"},
  perf:{t:"Shareholder returns", d:"Stock lagging the broader market"},
  ops:{t:"Operating performance", d:"Margins, returns or growth below peers"},
  capital:{t:"Capital allocation", d:"Balance sheet an activist could push to optimize"},
  gov:{t:"Governance red flags", d:"Entrenchment provisions in the proxy"},
  event:{t:"Recent catalysts", d:"Events that tend to draw activist attention"}
};
const PILLAR_ORDER=["value","perf","ops","capital","gov","event"];
function exportCsv(){ window.open("/api/shortlist.csv","_blank"); }

function withinDays(dateStr, n){
  if(!dateStr) return false;
  const d=new Date(dateStr+"T00:00:00"); if(isNaN(d)) return false;
  return (Date.now()-d.getTime()) <= n*86400000;
}
function renderNewRising(companies){
  const sec=document.getElementById("newRisingSection"), el=document.getElementById("newRising");
  if(!sec||!el) return;
  const isNew = c => withinDays(c.first_flagged, 7);
  const fresh = companies.filter(isNew);
  const rising = companies.filter(c => !isNew(c) && c.week_change!=null && c.week_change>0);
  const card = (c,badge) => `<div class="nr-card" onclick="openCompany('${esc(c.cik)}')">
      ${badge}<span class="nr-co">${esc(c.company)} <span class="meta">${esc(c.ticker)}</span></span>
      <span class="nr-score">${vulnChip(c.vuln)}</span></div>`;
  const html = fresh.map(c=>card(c,`<span class="nr-badge new">NEW</span>`)).join("")
             + rising.map(c=>card(c,`<span class="nr-badge up">▲${c.week_change}</span>`)).join("");
  sec.style.display="block";
  el.innerHTML = html || `<div class="empty" style="padding:10px 0;">No new entrants or risers yet — week-over-week movement builds over a few days of history.</div>`;
}
async function loadActiveSituations(){
  try{ const d=await (await fetch("/api/active-situations")).json();
    const sec=document.getElementById("activeSitSection"), el=document.getElementById("activeSit");
    if(!sec||!el) return;
    const list=d.companies||[];
    if(!list.length){ sec.style.display="none"; return; }
    sec.style.display="block";
    el.innerHTML = list.map(c=>{ regInfo(c); return `<div class="as-row" onclick="openCompany('${esc(c.cik)}')">
        <div class="as-co"><span class="co-link">${esc(c.company)}</span><div class="meta">${esc(c.ticker)}</div></div>
        <div class="as-head">${c.top_item_url?`<a href="${esc(c.top_item_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(c.top_item_title)||"activist headline"}</a>`:(esc(c.top_item_title)||"—")}</div>
        <div class="as-score">${vulnChip(c.vuln)}</div></div>`; }).join("");
  }catch(e){}
}

/* ---- Watchlist (shared) ---- */
async function loadWatchlist(){
  try{ const d=await (await fetch("/api/watchlist")).json();
    const items=d.items||[];
    WATCHLIST_SET = new Set(items.map(i=>i.cik));
    items.forEach(regInfo);
    const cnt=document.getElementById("wlCount"); if(cnt) cnt.textContent = items.length?` (${items.length})`:"";
    renderWatchlist(items);
  }catch(e){}
}
function statusBadge(s){
  if(s==="flagged") return `<span class="wl-badge ok">on shortlist</span>`;
  if(s==="active") return `<span class="wl-badge warn">activist engaged</span>`;
  if(s==="inactive") return `<span class="wl-badge gone">delisted / inactive</span>`;
  return `<span class="wl-badge muted">no longer flagged</span>`;
}
function renderWatchlist(items){
  const el=document.getElementById("watchlistBody"); if(!el) return;
  if(!items.length){ el.innerHTML=`<div class="empty">No companies yet. Click the ☆ star next to any company on the dashboard to add it here.</div>`; return; }
  el.innerHTML = items.map(c=>{
    const score = c.vuln!=null ? vulnChip(c.vuln) : (c.score!=null ? `<span class="wl-score">${c.score}</span>` : "");
    return `<div class="wl-row">
      <div class="wl-top">
        <span class="co-link" onclick="openCompany('${esc(c.cik)}')">${esc(c.company)}</span>
        <span class="meta">${esc(c.ticker)}</span>
        ${statusBadge(c.status)} ${score}
      </div>
      ${c.signals?`<div class="wl-sig">${esc(c.signals)}</div>`:""}
      <textarea class="wl-note" id="note-${esc(c.cik)}" placeholder="Pitch notes — angle, contact, status…">${esc(c.note||"")}</textarea>
      <div class="wl-actions">
        <button onclick="saveNote('${esc(c.cik)}')">Save note</button>
        <button class="ghost" onclick="toggleStar('${esc(c.cik)}')">Remove</button>
        <span class="wl-msg" id="wlmsg-${esc(c.cik)}"></span>
      </div></div>`;
  }).join("");
}
async function saveNote(cik){
  const ta=document.getElementById("note-"+cik); if(!ta) return;
  const msg=document.getElementById("wlmsg-"+cik);
  try{ await fetch("/api/watchlist/note",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik,note:ta.value})});
    if(msg){ msg.textContent="Saved ✓"; setTimeout(()=>{ if(msg) msg.textContent=""; },1500); }
  }catch(e){ if(msg) msg.textContent="Error saving"; }
}
async function toggleStar(cik, el){
  const on = WATCHLIST_SET.has(cik);
  const info = COMPANY_INFO[cik] || {};
  try{
    if(on){
      await fetch("/api/watchlist/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik})});
      WATCHLIST_SET.delete(cik);
    } else {
      await fetch("/api/watchlist/add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik,ticker:info.ticker||"",company:info.company||""})});
      WATCHLIST_SET.add(cik);
    }
  }catch(e){ return; }
  if(el && el.classList){
    if(el.classList.contains("star")) el.textContent = WATCHLIST_SET.has(cik)?'★':'☆';
    else el.textContent = WATCHLIST_SET.has(cik)?'★ On watchlist':'☆ Add to watchlist';
  }
  loadWatchlist(); loadShortlist(); loadActiveSituations();
}

async function refreshAll(){
  await loadWatchlist();
  await Promise.all([loadStatus(),loadFeed(),loadShortlist(),loadActiveSituations()]);
  document.getElementById("updated").textContent="Last updated "+new Date().toLocaleTimeString();
}
refreshAll(); setInterval(refreshAll, 5*60*1000);
