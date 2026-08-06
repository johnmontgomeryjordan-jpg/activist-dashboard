/* ===== Formatting helpers ===== */
const fmtCap = n => { if(n==null) return "—";
  if(n>=1e12) return "$"+(n/1e12).toFixed(2)+"T"; if(n>=1e9) return "$"+(n/1e9).toFixed(1)+"B";
  if(n>=1e6) return "$"+(n/1e6).toFixed(0)+"M"; if(n>=1e3) return "$"+(n/1e3).toFixed(0)+"K";
  return "$"+Math.round(n).toLocaleString(); };   // sub-$1M: round + group (was raw "$"+n -> "$528474.17999")
// Light phone formatter: format the common US 10-digit case; leave international/odd formats
// untouched (don't mangle a number we can't confidently parse, e.g. a UK "442073179700").
const fmtPhone = p => { if(!p) return p; const d=(""+p).replace(/\D/g,"");
  if(d.length===10) return `(${d.slice(0,3)}) ${d.slice(3,6)}-${d.slice(6)}`;
  if(d.length===11&&d[0]==="1") return `+1 (${d.slice(1,4)}) ${d.slice(4,7)}-${d.slice(7)}`;
  return p; };
const fmtPct = n => n==null? "—" : (n*100).toFixed(1)+"%";
const fmtNum = n => n==null? "—" : (typeof n==="number"? n.toLocaleString(undefined,{maximumFractionDigits:2}) : n);
const fmtDate = s => { if(!s) return ""; try{return new Date(s).toLocaleDateString(undefined,{month:"short",day:"numeric"});}catch(e){return s;} };
const _MON=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const fmtDateY = s => { if(!s) return ""; const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(String(s)); return m? _MON[+m[2]-1]+" "+(+m[3])+", "+m[1] : String(s); };
const fmtDateMD = s => { if(!s) return ""; const m=/^(\d{4})-(\d{2})-(\d{2})/.exec(String(s)); return m? _MON[+m[2]-1]+" "+(+m[3]) : fmtDate(s); };
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const secUrl = cik => `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(cik)}&type=&dateb=&owner=include&count=40`;

/* ===== Global state ===== */
let SHORTLIST=[], ACTIVE=[], STATUS={}, FEED={news:[],filings:[]};
let WATCHLIST_SET=new Set(), COMPANY_INFO={};
let FILTER="all", SORT={key:"vuln", dir:-1};
let CURRENT_VIEW="report", CURRENT_CIK=null, CURRENT_DATA=null, CURRENT_TAB="overview";
function regInfo(c){ if(c && c.cik) COMPANY_INFO[c.cik]={ticker:c.ticker, company:c.company}; }

/* ===== View routing ===== */
const VIEWS=["report","pitchkit","dashboard","active","accumulating","watchlist","feed","about","admin","company"];
function showView(name){
  VIEWS.forEach(v=>{ const p=document.getElementById("page-"+v); if(p) p.style.display = v===name?"block":"none"; });
  document.querySelectorAll(".navtab").forEach(b=>b.classList.remove("active"));
  const nb=document.getElementById("nt-"+name); if(nb) nb.classList.add("active");
  if(name!=="company") CURRENT_VIEW=name;
  if(name==="accumulating") loadAccumulating();
  if(name==="watchlist") loadWatchlist();
  if(name==="pitchkit") renderPitchKit();
  if(name==="about") loadFeedback();
  window.scrollTo(0,0);
}

/* ===== How it works: subtabs + feedback ===== */
function aboutTab(id){
  ["purpose","method","glossary","faq","using","limits","roadmap","feedback"].forEach(s=>{
    const sec=document.getElementById("about-"+s); if(sec) sec.style.display = s===id?"block":"none";
    const btn=document.getElementById("ab-"+s); if(btn) btn.classList.toggle("active", s===id);
  });
  if(id==="feedback") loadFeedback();
}
async function loadFeedback(){
  const box=document.getElementById("fbList"); if(!box) return;
  try{
    const d=await (await fetch("/api/feedback")).json();
    const items=d.feedback||[];
    box.innerHTML = items.length
      ? items.map(f=>`<div class="row2"><div>${esc(f.message)}</div><div class="meta"><span>${esc(f.name)||"Anonymous"}</span><span>${fmtDate((f.created_at||"").slice(0,10))}</span></div></div>`).join("")
      : `<div class="empty">No notes yet — be the first.</div>`;
  }catch(e){ box.innerHTML=`<div class="empty">Couldn't load notes.</div>`; }
}
async function submitFeedback(){
  const msg=(document.getElementById("fbMsg").value||"").trim();
  const out=document.getElementById("fbMsgOut");
  if(!msg){ if(out) out.textContent="Add a comment first."; return; }
  const name=(document.getElementById("fbName").value||"").trim();
  try{
    const r=await (await fetch("/api/feedback",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name,message:msg})})).json();
    if(out) out.textContent=r.message||"Saved.";
    document.getElementById("fbMsg").value="";
    loadFeedback();
  }catch(e){ if(out) out.textContent="Couldn't save — try again."; }
}

/* ===== Status + summary ===== */
async function loadStatus(){
  try{ STATUS=await (await fetch("/api/status")).json();
    document.getElementById("status").innerHTML =
      `Universe <b>${STATUS.universe_size}</b> · Refresh <b>${STATUS.refresh_minutes}m</b> · `+
      `Subscribers <b>${STATUS.subscribers}</b> · News <b>${STATUS.news_enabled?"on":"off"}</b> · `+
      `Email <b>${STATUS.email_enabled?"on":"off"}</b>`;
    const at=document.getElementById("aboutThreshold"); if(at) at.textContent=STATUS.threshold;
    renderSummary();
  }catch(e){}
}
function renderSummary(){
  const el=document.getElementById("summary"); if(!el) return;
  const severe=SHORTLIST.filter(c=>c.vuln!=null && c.vuln>=75).length;
  const fresh=SHORTLIST.filter(c=>withinDays(c.first_flagged,7)).length;
  const card=(lbl,val,col)=>`<div class="scard"><div class="lbl">${lbl}</div><div class="val" ${col?`style="color:${col}"`:""}>${val}</div></div>`;
  el.innerHTML = card("Tracked", STATUS.universe_size!=null?STATUS.universe_size.toLocaleString():"—")
    + card("Severe", severe, "var(--hot)")
    + card("New this week", fresh, "var(--accent)")
    + card("Active situations", ACTIVE.length, "var(--warn)");
}

/* ===== Vulnerability rating helpers ===== */
function vulnInfo(v){
  if(v==null) return {col:"var(--dim)", cls:"v0"};
  if(v>=75) return {col:"var(--hot)", cls:"v3"};
  if(v>=50) return {col:"var(--warn)", cls:"v2"};
  if(v>=25) return {col:"var(--accent)", cls:"v1"};
  return {col:"var(--dim)", cls:"v0"};
}
function vulnBand(v){ if(v==null) return "Unscored"; if(v>=75) return "Severe"; if(v>=50) return "High"; if(v>=25) return "Elevated"; return "Moderate"; }
// Rating shown as the qualitative BAND only (Severe/High/Elevated/Moderate) — the 0–92 number is
// kept in the data (c.vuln) for sorting/filtering but never displayed.
function vulnChip(v){ const i=vulnInfo(v); return `<span class="vchip ${i.cls}">${v==null?"—":vulnBand(v)}</span>`; }
function vulnCell(v){ const i=vulnInfo(v);
  return `<div class="vwrap"><span class="vband ${i.cls}">${vulnBand(v)}</span></div>`; }
function gaugeSvg(v){
  const val=(v==null)?0:Math.max(0,Math.min(100,v));
  const C=Math.PI*84, on=(val/100)*C, col=vulnInfo(v).col;
  // Severity ARC only — the numeric index is hidden; the band word ("Severe" etc.) is shown beside it.
  return `<svg viewBox="0 0 200 118" class="gauge" role="img" aria-label="activist-target profile: ${vulnBand(v)}">
    <path d="M16,102 A84,84 0 0 1 184,102" fill="none" stroke="var(--line2)" stroke-width="15" stroke-linecap="round"/>
    <path d="M16,102 A84,84 0 0 1 184,102" fill="none" stroke="${col}" stroke-width="15" stroke-linecap="round" stroke-dasharray="${on.toFixed(1)} ${(C+4).toFixed(1)}"/>
    <text x="100" y="98" text-anchor="middle" class="glabel" style="fill:${col};font-weight:700;text-transform:uppercase;letter-spacing:.08em;font-size:15px;">${v==null?"—":vulnBand(v)}</text>
  </svg>`;
}
function sparkline(hist, col){
  if(!hist || hist.length<2) return "";
  const w=200,h=30, mn=Math.min(...hist), mx=Math.max(...hist), rng=(mx-mn)||1;
  const pts=hist.map((v,i)=>`${(i/(hist.length-1)*w).toFixed(1)},${(h-2-((v-mn)/rng)*(h-4)).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" style="flex:1; height:26px;" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2"/></svg>`;
}
function kvMini(k,v){ return `<div class="kvm"><div class="kvm-k">${k}</div><div class="kvm-v">${v}</div></div>`; }
function priceChart(series, last){
  if(!series || series.length<2) return "";
  const w=600,h=120,pad=6, mn=Math.min(...series), mx=Math.max(...series), rng=(mx-mn)||1;
  const x=i=>(pad+i/(series.length-1)*(w-2*pad)).toFixed(1);
  const y=v=>(h-pad-((v-mn)/rng)*(h-2*pad)).toFixed(1);
  const pts=series.map((v,i)=>`${x(i)},${y(v)}`).join(" ");
  const up=series[series.length-1]>=series[0], col=up?"var(--ok)":"var(--hot)";
  return `<div style="margin-top:4px;">
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:120px;display:block;">
      <polygon points="${pad},${h-pad} ${pts} ${w-pad},${h-pad}" fill="${col}" opacity="0.08"/>
      <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6" vector-effect="non-scaling-stroke"/></svg>
    <div style="display:flex;justify-content:space-between;color:var(--muted);font-size:11.5px;margin-top:4px;">
      <span>${series.length} trading days</span><span>last <b style="color:var(--text)">$${fmtNum(last)}</b></span><span>close range $${fmtNum(mn)} – $${fmtNum(mx)}</span></div></div>`;
}
function weekChip(ch){
  if(ch==null) return ` <span class="chg new" title="newly tracked">new</span>`;
  if(ch>0) return ` <span class="chg up" title="vs last week">▲${ch}</span>`;
  if(ch<0) return ` <span class="chg down" title="vs last week">▼${Math.abs(ch)}</span>`;
  return ` <span class="chg flat" title="vs last week">±0</span>`;
}
function withinDays(s,n){ if(!s) return false; const d=new Date(s+"T00:00:00"); if(isNaN(d)) return false; return (Date.now()-d.getTime())<=n*86400000; }

/* ===== Pillars ===== */
const PILLAR_OF={
  cheap_abs:"value", cheap_pb:"value", cheap_ev_ebitda:"value",
  weak_tsr_1y:"perf", weak_tsr_3y:"perf", lags_own_peers:"perf",
  strategic_review:"strategic",
  low_margin:"ops", low_roa:"ops", weak_growth:"ops", high_sga:"ops",
  cash_hoard:"capital", underlevered:"capital", high_goodwill:"capital",
  gov_classified:"gov", gov_poison:"gov", gov_dual:"gov", overpaid_ceo:"gov",
  insider_selling:"insider", insider_buying:"insider", weak_vote_support:"vote",
  ceo_departure:"event", earnings_miss:"event", impairment:"event",
  layoffs:"event", leadership_change:"event", results_update:"event",
  news_negative:"event", activist_filing:"event", exec_reaction_drop:"event"
};
const PILLAR_META={
  value:{t:"Valuation gap", d:"Trading cheap relative to assets or peers"},
  perf:{t:"Shareholder returns", d:"Stock lagging the broader market"},
  strategic:{t:"Strategic vulnerability", d:"A de-rated but viable business — a sale / take-private setup"},
  ops:{t:"Operating performance", d:"Margins, returns or growth below peers"},
  capital:{t:"Capital allocation", d:"Balance sheet an activist could push to optimize"},
  gov:{t:"Governance red flags", d:"Entrenchment provisions and pay-for-performance gaps in the proxy"},
  vote:{t:"Shareholder support", d:"How shareholders voted at the last annual meeting"},
  insider:{t:"Insider activity", d:"What management is doing with their own money (Form 4)"},
  event:{t:"Recent catalysts", d:"Events that tend to draw activist attention"}
};
const PILLAR_ORDER=["value","perf","strategic","ops","capital","gov","vote","insider","event"];

/* ===== Dashboard: leads table + fresh picks ===== */
async function loadShortlist(){
  try{ const d=await (await fetch("/api/shortlist")).json();
    SHORTLIST=d.companies||[]; SHORTLIST.forEach(regInfo);
    renderSummary(); renderFresh(); renderLeads();
  }catch(e){}
}
function setFilter(f, el){
  FILTER=f;
  document.querySelectorAll(".fchip").forEach(c=>c.classList.toggle("on", c.getAttribute("data-filter")===f));
  renderLeads();
}
function sortLeads(key){
  if(SORT.key===key) SORT.dir*=-1;
  else SORT={key, dir:(key==="company"||key==="first_flagged")?1:-1};
  renderLeads();
}
function renderLeads(){
  const tb=document.getElementById("shortlist"); if(!tb) return;
  const q=(document.getElementById("leadSearch").value||"").toLowerCase().trim();
  let rows=SHORTLIST.slice();
  if(FILTER==="severe") rows=rows.filter(c=>c.vuln!=null&&c.vuln>=75);
  else if(FILTER==="high") rows=rows.filter(c=>c.vuln!=null&&c.vuln>=50);
  if(q) rows=rows.filter(c=>((c.company||"")+" "+(c.ticker||"")+" "+(c.signals||"")).toLowerCase().includes(q));
  const k=SORT.key;
  rows.sort((a,b)=>{
    let av=a[k], bv=b[k];
    if(k==="company"){ av=(av||"").toLowerCase(); bv=(bv||"").toLowerCase(); return av<bv?-1*SORT.dir:av>bv?1*SORT.dir:0; }
    av=av==null?-Infinity:av; bv=bv==null?-Infinity:bv;
    return (av-bv)*SORT.dir;
  });
  if(!rows.length){ tb.innerHTML=`<tr><td colspan="5" class="empty">No matching companies.</td></tr>`; return; }
  tb.innerHTML = rows.map(c=>{
    const star=`<span class="star" onclick="event.stopPropagation();toggleStar('${esc(c.cik)}',this)" title="Watchlist">${WATCHLIST_SET.has(c.cik)?'★':'☆'}</span>`;
    const dot=c.has_note?`<span class="notedot" title="has notes">●</span>`:"";
    return `<tr onclick="openCompany('${esc(c.cik)}')">
      <td>${star}<span class="co-link">${esc(c.company)}</span>${dot}<div class="co-meta">${esc(c.ticker)}</div></td>
      <td class="mcap">${fmtCap(c.market_cap)}</td>
      <td class="vcell" title="raw signal score ${c.score}">${vulnCell(c.vuln)}${weekChip(c.week_change)}</td>
      <td class="signals">${esc((c.signals||"").length>90?(c.signals.slice(0,90)+"…"):(c.signals||"—"))}</td>
      <td class="mcap">${esc(c.first_flagged||"")}</td></tr>`;
  }).join("");
}
function renderFresh(){
  const nrEl=document.getElementById("newRising"), spEl=document.getElementById("spotlight");
  const isNew=c=>withinDays(c.first_flagged,7);
  const fresh=SHORTLIST.filter(isNew);
  const rising=SHORTLIST.filter(c=>!isNew(c)&&c.week_change!=null&&c.week_change>0);
  const card=(c,badge)=>`<div class="nr-card" onclick="openCompany('${esc(c.cik)}')">${badge}<span class="nr-co">${esc(c.company)} <span class="meta">${esc(c.ticker)}</span></span>${vulnChip(c.vuln)}</div>`;
  if(nrEl){
    const html=fresh.slice(0,6).map(c=>card(c,`<span class="nr-badge new">NEW</span>`)).join("")
             + rising.slice(0,6).map(c=>card(c,`<span class="nr-badge up">▲${c.week_change}</span>`)).join("");
    nrEl.innerHTML=html||`<div class="empty" style="padding:8px 0;">No new entrants or risers yet.</div>`;
  }
  if(spEl){
    const freshCiks=new Set([...fresh,...rising].map(c=>c.cik));
    const pool=SHORTLIST.filter(c=>!freshCiks.has(c.cik));
    let spot=[];
    if(pool.length){
      const seed=Math.floor(Date.now()/86400000);
      const start=(seed*4)%pool.length;
      for(let i=0;i<Math.min(5,pool.length);i++) spot.push(pool[(start+i)%pool.length]);
    }
    spEl.innerHTML = spot.length
      ? spot.map(c=>card(c,`<span class="nr-badge spot">PICK</span>`)).join("")
      : `<div class="empty" style="padding:8px 0;">Spotlight builds as more companies are flagged.</div>`;
  }
}

/* ===== Top headlines (dashboard) ===== */
const NEWS_PRIORITY={ownership:0,activist:0,proxy:1,exec:2,movers:3,distress:4,market:5,other:6};
function toplineNews(news){
  return [...news].sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||"")).slice(0,12)
    .sort((a,b)=>(NEWS_PRIORITY[newsCategory(a.headline)]-NEWS_PRIORITY[newsCategory(b.headline)])||(b.published_at||"").localeCompare(a.published_at||"")).slice(0,5);
}
function newsItemRow(n){ const cat=newsCategory(n.headline);
  return `<div class="item"><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a>
    <div class="meta"><span class="tag cat-${cat}">${CAT_LABEL[cat]||"News"}</span><span class="tag">${esc(n.source)||"news"}</span><span>${fmtDate(n.published_at)}</span></div></div>`;
}
function renderTopNews(){
  const el=document.getElementById("topNews"); if(!el) return;
  const news=FEED.news_top||FEED.news||[];   // curated subset (falls back to full feed)
  el.innerHTML = news.length ? toplineNews(news).map(newsItemRow).join("") : `<div class="empty">No headlines yet.</div>`;
}

/* ===== Live feed page (categorized) ===== */
const MOVE_RE=/\b(slid|slide|slides|slip|slips|slipped|fall|falls|fell|drop|drops|dropped|dip|dips|dipped|sink|sinks|sank|slump|slumps|slumped|decline|declines|declined|retreat|retreats|retreated|plunge|plunges|tumble|tumbles|plummets|sell-?off)\b/;
const CAT_ACTIVIST=["activist","proxy fight","proxy battle","proxy contest","13d","schedule 13d","board seat","board seats","director nominee","nominates","builds stake","raises stake","takes stake","boosts stake","elliott management","starboard","trian","jana partners","jana","third point","carl icahn","icahn","nelson peltz","valueact","value act","engine no","ancora","politan","sachem head","legion partners","short seller","short-seller"];
const CAT_PROXY=["glass lewis","proxy advisor","proxy adviser","institutional shareholder services","iss recommends","iss advises","iss backs","recommends against","withhold vote","withhold votes"];
const CAT_EXEC=["steps down","stepping down","steps aside","to resign","resigns","resigned","ousted","ousts","departs","departure","interim ceo","interim cfo","names ceo","new ceo","appoints ceo","names new chief","leadership change","management shake","shake-up","shakeup","reshuffle","exits as ceo","exits as cfo"];
const CAT_MARKET=["nasdaq","s&p","dow jones"," dow ","wall street","stock market","stocks ","futures","treasury","global markets","indexes","indices"];
// Smart-money / ownership moves — a fund building (or taking) a position. The highest-value
// corroboration for a proactive lead, so it gets its own top-priority bucket instead of falling
// into "distress". 13D is left in CAT_ACTIVIST (it's an activist filing); passive 13G/13F land here.
const CAT_OWNERSHIP=["13g","13f","builds stake","raises stake","takes stake","boosts stake","new stake","million stake","initiates a position","initiates position","new position","boosts its position","raises its position","increases its position","increased its position","adds to its position","adds to position","takes a position","acquires a stake","buys a stake","snaps up","scoops up","hedge fund"];
// Genuine company distress (earnings / guidance / impairment / legal). The catch-all is now
// "other" (a neutral "News" chip) instead of "distress", so a benign per-company item — e.g. a
// screener blurb or a neutral product note — isn't mislabelled "Distress".
const CAT_DISTRESS=["guidance","profit warning","earnings miss","misses estimates","misses expectations","shortfall","downgrade","downgraded","impairment","write-down","writedown","restructuring","layoff","job cuts","warns","warned","disappoint","strategic review","explores sale","exploring sale","bankruptcy","default","investigation","lawsuit","probe","restatement","recall","cuts outlook","lowers outlook","cuts forecast","slashes","weak guidance","halts"];
const CATS=[["ownership","Ownership & stakes"],["activist","Activist activity"],["proxy","Proxy advisors"],["exec","Executive changes"],["movers","Price movers"],["distress","Earnings & distress"],["market","Market"],["other","Other"]];
const CAT_LABEL={ownership:"Ownership",activist:"Activist",proxy:"Proxy",exec:"Leadership",movers:"Price move",distress:"Distress",market:"Market",other:"News"};
const FILING_CATS=[["exec","Executive changes"],["earn","Earnings & guidance"],["impair","Impairments & write-downs"],["restr","Restructuring & layoffs"],["other","Other filings"]];
const FILING_PRIORITY={exec:0,earn:1,impair:2,restr:3,other:4};
let NEWS_GROUPS={}, FILING_GROUPS={};
function newsCategory(h){ const t=" "+(h||"").toLowerCase()+" "; const has=a=>a.some(k=>t.includes(k));
  if(has(CAT_PROXY)) return "proxy"; if(has(CAT_ACTIVIST)) return "activist"; if(has(CAT_OWNERSHIP)) return "ownership"; if(has(CAT_EXEC)) return "exec";
  const m=MOVE_RE.test(t); if(m&&has(CAT_MARKET)) return "market"; if(m) return "movers"; if(has(CAT_DISTRESS)) return "distress"; return "other"; }
function filingCategory(f){ const s=(f.signals||"").toLowerCase();
  if(/ceo_departure|leadership_change/.test(s)) return "exec"; if(/earnings_miss|results_update/.test(s)) return "earn";
  if(/impairment/.test(s)) return "impair"; if(/layoff|restructuring/.test(s)) return "restr"; return "other"; }
function toplineFilings(filings){
  return [...filings].sort((a,b)=>(b.filed_at||"").localeCompare(a.filed_at||"")).slice(0,12)
    .sort((a,b)=>(FILING_PRIORITY[filingCategory(a)]-FILING_PRIORITY[filingCategory(b)])||(b.filed_at||"").localeCompare(a.filed_at||"")).slice(0,5);
}
function filingItemRow(f){
  const sigs=(f.signals||"").split(",").filter(Boolean).map(s=>`<span class="tag sig">${esc(s.replace(/_/g," "))}</span>`).join(" ");
  return `<div class="item"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.company)} — ${esc(f.title)}</a>
    <div class="meta"><span class="tag">${esc(f.ticker)||esc(f.form)}</span><span>${fmtDate(f.filed_at)}</span>${sigs}</div></div>`;
}
function catRowsHtml(cats, groups, opener){
  let html=""; cats.forEach(([key,label])=>{ const items=groups[key]||[]; if(!items.length) return;
    html+=`<div class="catrow" onclick="${opener}('${key}')"><span class="catname">${esc(label)}</span>
      <span class="catright"><span class="acc-count">${items.length}</span><span class="chev">›</span></span></div>`; });
  return html;
}
function renderTicker(news){
  const t=document.getElementById("ticker"); if(!t) return;
  const items=[...news].sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||"")).slice(0,20);
  if(!items.length){ t.style.display="none"; return; }
  t.style.display="block";
  const seq=items.map(n=>`<span class="ticker-item"><span class="ticker-dot">●</span> <a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a></span>`).join("");
  t.innerHTML=`<div class="ticker-track" style="animation-duration:${Math.max(40,items.length*5)}s">${seq}${seq}</div>`;
}
async function loadFeed(){
  try{ const d=await (await fetch("/api/feed")).json();
    FEED={news:d.news||[], news_top:d.news_top||d.news||[], filings:d.filings||[]};
    renderTopNews();
    NEWS_GROUPS={}; CATS.forEach(([k])=>NEWS_GROUPS[k]=[]);
    FEED.news.forEach(n=>{ (NEWS_GROUPS[newsCategory(n.headline)] ||= []).push(n); });
    const nf=document.getElementById("newsFeed");
    if(nf) nf.innerHTML = FEED.news.length
      ? `<div><div class="topline-h">★ Top 5 — most relevant &amp; recent</div>`+toplineNews(FEED.news).map(newsItemRow).join("")+`</div>`
        + `<div class="cat-h">Browse by category</div>`+catRowsHtml(CATS,NEWS_GROUPS,"openNewsCat")
      : `<div class="empty">No headlines yet.</div>`;
    FILING_GROUPS={}; FILING_CATS.forEach(([k])=>FILING_GROUPS[k]=[]);
    FEED.filings.forEach(f=>{ (FILING_GROUPS[filingCategory(f)] ||= []).push(f); });
    const ff=document.getElementById("filingFeed");
    if(ff) ff.innerHTML = FEED.filings.length
      ? `<div><div class="topline-h">★ Top 5 — most relevant &amp; recent</div>`+toplineFilings(FEED.filings).map(filingItemRow).join("")+`</div>`
        + `<div class="cat-h">Browse by type</div>`+catRowsHtml(FILING_CATS,FILING_GROUPS,"openFilingCat")
      : `<div class="empty">No filings yet.</div>`;
    renderTicker(FEED.news);
  }catch(e){}
}
function openListModal(title, rows){
  document.getElementById("mTitle").textContent=title;
  document.getElementById("mSub").textContent=""; document.getElementById("mScore").textContent="";
  document.getElementById("mBody").innerHTML=`<div class="dlist">${rows||`<div class="empty">No items.</div>`}</div>`;
  document.getElementById("overlay").classList.add("open");
}
function closeOverlay(){ document.getElementById("overlay").classList.remove("open"); }
function modalNewsRow(n){ return `<div class="row2"><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}</a><div class="meta"><span class="tag">${esc(n.source)||"news"}</span><span>${fmtDate(n.published_at)}</span></div></div>`; }
function modalFilingRow(f){ const sigs=(f.signals||"").split(",").filter(Boolean).map(s=>`<span class="tag sig">${esc(s.replace(/_/g," "))}</span>`).join(" ");
  return `<div class="row2"><a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.company)} — ${esc(f.title)}</a><div class="meta"><span class="tag">${esc(f.ticker)||esc(f.form)}</span><span>${fmtDate(f.filed_at)}</span>${sigs}</div></div>`; }
function openNewsCat(key){ const items=NEWS_GROUPS[key]||[]; const label=(CATS.find(c=>c[0]===key)||["",key])[1];
  openListModal(`${label} — ${items.length} headline${items.length===1?"":"s"}`, items.map(modalNewsRow).join("")); }
function openFilingCat(key){ const items=FILING_GROUPS[key]||[]; const label=(FILING_CATS.find(c=>c[0]===key)||["",key])[1];
  openListModal(`${label} — ${items.length} filing${items.length===1?"":"s"}`, items.map(modalFilingRow).join("")); }
document.addEventListener("keydown", e=>{ if(e.key==="Escape") closeOverlay(); });

/* ===== Active situations (tiered: confirmed / reported / manual) ===== */
const TIER_ORDER=["confirmed","ma","reported","manual","settled"];
function tierMeta(t){
  if(t==="confirmed") return {label:"Confirmed", col:"var(--hot)", desc:"An activist filing is on record with the SEC (13D or contested proxy) — authoritative."};
  if(t==="ma")        return {label:"M&A / control contest", col:"var(--accent)", desc:"A corporate bidder is running a takeover or proxy contest to acquire or control the company — an M&A fight, not a shareholder-activist campaign."};
  if(t==="reported")  return {label:"Reported",  col:"var(--warn)", desc:"Named in the press alongside a known activist — confirm before you rely on it."};
  if(t==="settled")   return {label:"Recently settled / concluded", col:"var(--muted)", desc:"The campaign has stood down — the company and activist signed a cooperation / settlement agreement, or the contested annual meeting has already been held. Kept for context, not a live threat."};
  return {label:"Manual", col:"var(--accent)", desc:"Tagged by your team."};
}
function daysSince(d){ if(!d) return null; const t=Date.parse(d); if(isNaN(t)) return null; return Math.floor((Date.now()-t)/86400000); }
function freshBadge(m){ const ds=daysSince(m&&m.date);
  if(m&&(m.kind==="settled"||m.kind==="concluded"||m.kind==="ma")) return "";   // settled/concluded/M&A is not an "urgent defense lead"
  if(ds!=null && ds<=45 && (m.kind==="13d"||m.source==="SEC EDGAR")) return `<span class="as-fresh">⚡ fresh · ${ds}d — urgent defense lead</span>`;
  return ""; }
function asRow(c){
  const m=c.meta||{};
  const who=m.who?`<b>${esc(m.who)}</b> · `:"";
  const headline=c.top_item_url
    ? `<a href="${esc(c.top_item_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(c.top_item_title||m.label||"details")}</a>`
    : esc(c.top_item_title||m.label||"—");
  const dt=m.date?`<span class="as-date">${esc(fmtDateMD(m.date))}</span>`:"";
  const pin=(c.manual&&c.tier!=="manual")?`<span class="as-pin" title="manually pinned">📌</span>`:"";
  return `<div class="as-row" onclick="openCompany('${esc(c.cik)}')">
    <div><span class="co-link">${esc(c.company)}</span>${pin}<div class="co-meta">${esc(c.ticker||"")}</div></div>
    <div class="as-head">${who}${headline}${dt}${freshBadge(m)}</div>
    <div>${vulnChip(c.vuln)}</div>
    <div><button class="ghost as-mng" onclick="event.stopPropagation();openSituationModal('${esc(c.cik)}')">Manage</button></div>
  </div>`;
}
async function loadActiveSituations(){
  try{ const d=await (await fetch("/api/active-situations")).json();
    ACTIVE=d.companies||[]; ACTIVE.forEach(regInfo);
    const nav=document.getElementById("navActive"); if(nav) nav.textContent=ACTIVE.length?`(${ACTIVE.length})`:"";
    renderSummary();
    const el=document.getElementById("activeSit"); if(!el) return;
    const groups={confirmed:[],ma:[],reported:[],manual:[],settled:[]};
    ACTIVE.forEach(c=>{ (groups[c.tier]||groups.manual).push(c); });
    Object.values(groups).forEach(g=>g.sort((a,b)=>
      (((b.meta&&b.meta.date)||"").localeCompare((a.meta&&a.meta.date)||"")) || ((b.vuln||0)-(a.vuln||0))));
    let html="";
    TIER_ORDER.forEach(t=>{ const g=groups[t]; if(!g.length) return; const tm=tierMeta(t);
      html+=`<div class="as-section">
        <div class="as-section-h">
          <span class="as-tier-badge" style="background:color-mix(in srgb, ${tm.col} 14%, transparent);color:${tm.col};border-color:color-mix(in srgb, ${tm.col} 45%, transparent)">${tm.label}</span>
          <span class="as-section-d">${tm.desc}</span><span class="as-section-n">${g.length}</span></div>
        <div class="panel"><div class="as-list">${g.map(asRow).join("")}</div></div></div>`; });
    el.innerHTML = html || `<div class="empty">No active situations right now. The SEC 13D / contested-proxy sweep runs daily; named-activist news is matched continuously; or add one manually above.</div>`;
  }catch(e){}
}

/* ===== Activist fund display names — resolve raw filer tokens ("elliott", "h partners
   management") to proper firm names so Accumulating subheaders and the Disclosed tier read
   cleanly. Display-only; backend keys are unchanged. Unknown keys fall back to Title Case. ===== */
const FUND_NAMES={
  "alta fox":"Alta Fox Capital","ananym":"Ananym Capital","ancora":"Ancora Advisors",
  "anson funds":"Anson Funds Management","barington":"Barington Capital",
  "browning west":"Browning West","caligan":"Caligan Partners","carronade":"Carronade Capital",
  "cevian":"Cevian Capital","corvex":"Corvex Management","elliott":"Elliott Investment Management",
  "engaged capital":"Engaged Capital","engine capital":"Engine Capital",
  "glenview":"Glenview Capital Management","h partners":"H Partners Management",
  "h partners management":"H Partners Management","holdco asset":"HoldCo Asset Management",
  "hestia":"Hestia Capital","icahn capital":"Icahn Capital","impactive":"Impactive Capital",
  "irenic":"Irenic Capital Management","jana partners":"JANA Partners","kanen":"Kanen Wealth Management",
  "land & buildings":"Land & Buildings","legion partners":"Legion Partners Asset Management",
  "lynrock":"Lynrock Lake","mantle ridge":"Mantle Ridge","oasis management":"Oasis Management",
  "p2 capital":"P2 Capital Partners","politan":"Politan Capital Management",
  "sachem head":"Sachem Head Capital","sarissa":"Sarissa Capital Management",
  "soroban":"Soroban Capital Partners","starboard":"Starboard Value",
  "steel partners":"Steel Partners Holdings","the children's investment":"TCI Fund Management",
  "third point":"Third Point","trian":"Trian Fund Management","valueact":"ValueAct Holdings",
  "vision one":"Vision One Management","voss capital":"Voss Capital"
};
function fundLabel(f){
  if(!f) return "—";
  const k=String(f).trim().toLowerCase();
  if(FUND_NAMES[k]) return FUND_NAMES[k];
  return String(f).replace(/\b([a-z])([a-z0-9&.'’-]*)/g,(m,a,b)=>a.toUpperCase()+b);
}

/* ===== Disclosed holders (>=5%, 13D on file, no campaign in 18mo) — on Active Situations ===== */
function disclosedRow(c){
  // This is the ≥5% tier, so only show holders that are THEMSELVES ≥5% (a sub-5% co-holder like a
  // 2% stake doesn't belong under a "disclosed ≥5%" header).
  const who=(c.holders||[]).filter(h=>(h.ownership_pct||0)>=0.05).map(h=>{
    const o=h.ownership_pct!=null?` <span class="ac-conv">${(h.ownership_pct*100).toFixed(1)}%</span>`:"";
    const url=h.fund_cik?`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(h.fund_cik)}&type=13F-HR`:null;
    const nm=`<span class="ac-fund">${esc(fundLabel(h.fund))}</span>`;
    return (url?`<a href="${url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${nm}</a>`:nm)+o;
  }).join(", ");
  const q=c.quarter?`<span class="as-date">${esc(c.quarter)} 13F</span>`:"";
  const board=c.on_board?vulnChip(c.vuln):"";
  return `<div class="as-row" onclick="openCompany('${esc(c.cik)}')">
    <div><span class="co-link">${esc(c.company)}</span><div class="co-meta">${esc(c.ticker||"")}</div></div>
    <div class="as-head">${who} <span class="ac-disc" title="≥5% per the latest 13F — a Schedule 13D or 13G is on file (we haven't fetched which); this figure and link are the 13F">≥5% stake</span>${q}</div>
    <div>${board}</div>
    <div><button class="ghost as-mng" onclick="event.stopPropagation();openCompany('${esc(c.cik)}')">View</button></div>
  </div>`;
}
async function loadDisclosed(){
  try{ const d=await (await fetch("/api/disclosed-holders")).json();
    const rows=d.companies||[]; rows.forEach(regInfo);
    const el=document.getElementById("disclosedHolders"); if(!el) return;
    if(!rows.length){ el.innerHTML=""; return; }
    rows.sort((a,b)=>((b.top_ownership||0)-(a.top_ownership||0)));
    const col="var(--accent)";
    const desc="A known activist holds ≥5% per its latest 13F (so a Schedule 13D or 13G is on file — already public), but no NEW 13D / proxy in the last 18 months. A known holder to keep watch on, not a fresh accumulation. (Ownership is from the quarterly 13F, ~45-day lag.)";
    el.innerHTML=`<div class="as-section">
      <div class="as-section-h">
        <span class="as-tier-badge" style="background:color-mix(in srgb, ${col} 14%, transparent);color:${col};border-color:color-mix(in srgb, ${col} 45%, transparent)">Disclosed holder</span>
        <span class="as-section-d">${desc}</span><span class="as-section-n">${rows.length}</span></div>
      <div class="panel"><div class="as-list">${rows.map(disclosedRow).join("")}</div></div></div>`;
  }catch(e){}
}

/* ===== Accumulating (13F: activist holds, hasn't agitated yet) — grouped by activist ===== */
function accumFundRow(e){
  const bits=[];
  if(e.weight!=null) bits.push(`${(e.weight*100).toFixed(1)}% of book`);
  if(e.own!=null)    bits.push(`~${(e.own*100).toFixed(2)}% of co.`);
  const stake=bits.length?`<span class="ac-conv">${bits.join(" · ")}</span>`:"";
  // A stake >=5% forced a Schedule 13D/13G filing, so it's already DISCLOSED — flag it so a
  // known public campaign (Elliott/Southwest) never reads as a stealth accumulation.
  const disc=(e.own!=null && e.own>=0.05)?`<span class="ac-disc" title="≥5% per 13F — a Schedule 13D or 13G is on file (already public)">≥5% stake</span>`:"";
  const q=e.quarter?`<span class="as-date">${esc(e.quarter)} 13F</span>`:"";
  const board=e.on_board?vulnChip(e.vuln):`<span class="ac-note">not yet on the board</span>`;
  return `<div class="as-row" onclick="openCompany('${esc(e.cik)}')">
    <div><span class="co-link">${esc(e.company)}</span><div class="co-meta">${esc(e.ticker||"")}</div></div>
    <div class="as-head">${stake}${disc}${q}</div>
    <div>${board}</div>
    <div><button class="ghost as-mng" onclick="event.stopPropagation();openCompany('${esc(e.cik)}')">View</button></div>
  </div>`;
}
async function loadAccumulating(){
  try{ const d=await (await fetch("/api/accumulating")).json();
    const rows=d.companies||[]; rows.forEach(regInfo);
    const nav=document.getElementById("navAccum"); if(nav) nav.textContent=rows.length?`(${rows.length})`:"";
    const el=document.getElementById("accumBody"); if(!el) return;
    if(!rows.length){
      el.innerHTML=`<div class="empty">No accumulating names yet. This fills once the 13F refresh has run — a known activist holding a <b>material</b> stake (≥1% of the company) in a name that <b>isn't</b> already an active situation shows up here. If it's empty after a refresh, trigger one on the <b>Admin</b> tab.</div>`;
      return;
    }
    // Pivot company-rows into activist-fund groups (a company held by 3 funds appears under all 3).
    const byFund={};
    rows.forEach(c=>{ (c.holders||[]).forEach(h=>{
      const f=h.fund||"—";
      const g=byFund[f]||(byFund[f]={cik:h.fund_cik, items:[]});
      g.items.push({company:c.company, ticker:c.ticker, cik:c.cik, vuln:c.vuln,
        on_board:c.on_board, quarter:c.quarter, weight:h.weight_in_fund, own:h.ownership_pct});
    }); });
    const funds=Object.keys(byFund).sort((a,b)=>fundLabel(a).localeCompare(fundLabel(b)));
    el.innerHTML = funds.map(f=>{
      const g=byFund[f];
      g.items.sort((a,b)=>((b.own||0)-(a.own||0))||((b.weight||0)-(a.weight||0)));
      const url=g.cik?`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${encodeURIComponent(g.cik)}&type=13F-HR`:null;
      const lbl=fundLabel(f);
      const head=url?`<a href="${url}" target="_blank" rel="noopener">${esc(lbl)}</a>`:esc(lbl);
      const n=g.items.length;
      return `<div class="as-section">
        <div class="as-section-h"><span class="ac-fundhead">${head}</span>
          <span class="as-section-d">${n} position${n>1?"s":""} · not a current active campaign</span></div>
        <div class="panel"><div class="as-list">${g.items.map(accumFundRow).join("")}</div></div></div>`;
    }).join("");
  }catch(e){ const el=document.getElementById("accumBody"); if(el) el.innerHTML=`<div class="empty">Couldn't load — try again.</div>`; }
}

/* ----- Manual override modal + audit ----- */
function openModalRaw(title, html){
  document.getElementById("mTitle").textContent=title;
  document.getElementById("mSub").textContent=""; document.getElementById("mScore").textContent="";
  document.getElementById("mBody").innerHTML=html;
  document.getElementById("overlay").classList.add("open");
}
async function openSituationModal(cik){
  const info=COMPANY_INFO[cik]||{};
  let cur="", audit=[], company=info.company||cik, tier="";
  try{ const d=await (await fetch("/api/company?cik="+encodeURIComponent(cik))).json();
    if(d.ok){ cur=d.manual_situation||""; audit=d.situation_audit||[]; company=d.company||company; tier=d.situation_tier||""; }
  }catch(e){}
  const opt=(val,checked,disabled,t,desc)=>`<label class="sit-opt${disabled?" disabled":""}">
    <input type="radio" name="sitact" value="${val}" ${checked?"checked":""} ${disabled?"disabled":""}>
    <span><span class="ot">${t}</span><span class="od">${desc}</span></span></label>`;
  const def=cur||"active";
  const autoNote = tier&&!cur ? `<div class="hint" style="margin:0 0 10px">Currently auto-detected as <b>${esc(tierMeta(tier).label)}</b>. You can override that below.</div>` : "";
  const auditHtml = audit.length ? `<div class="sit-audit"><h4>Audit trail</h4>`+audit.map(a=>
    `<div class="sit-audit-row"><span>${esc((a.ts||"").replace("T"," ").slice(0,16))}</span><span class="aa">${esc(a.action)}</span>${a.actor?`<span>by ${esc(a.actor)}</span>`:""}${a.note?`<span>— ${esc(a.note)}</span>`:""}</div>`).join("")+`</div>` : "";
  openModalRaw(`Active situation — ${company}`, `
    ${autoNote}
    <div class="sit-opts">
      ${opt("active", def==="active", false, "Mark as an active situation", "Forces it into Active Situations as a Manual tag (wins over automated detection).")}
      ${opt("exclude", def==="exclude", false, "Not an active situation — hide it", "Suppresses a false positive. Removes it from Active Situations.")}
      ${opt("clear", false, !cur, "Use automatic detection", cur?"Clears your manual override and reverts to what the system detects.":"No manual override set — nothing to clear.")}
    </div>
    <div class="sit-field"><label>Your initials (for the audit trail)</label><input id="sitActor" placeholder="e.g. JJ"></div>
    <div class="sit-field"><label>Note (optional)</label><textarea id="sitNote" placeholder="Why? e.g. 'Elliott 13D filed 9/9' or 'headline was about a different company'"></textarea></div>
    <div class="sit-actions"><button class="ghost" onclick="closeOverlay()">Cancel</button><button id="sitSave" onclick="submitSituation('${esc(cik)}')">Save</button></div>
    ${auditHtml}`);
}
async function submitSituation(cik){
  const sel=document.querySelector('input[name="sitact"]:checked'); if(!sel) return;
  const action=sel.value;
  const actor=(document.getElementById("sitActor")||{}).value||"";
  const note=(document.getElementById("sitNote")||{}).value||"";
  const company=(COMPANY_INFO[cik]||{}).company||"";
  const btn=document.getElementById("sitSave"); if(btn){ btn.disabled=true; btn.textContent="Saving…"; }
  try{ await fetch("/api/situation",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({cik,action,actor:actor.trim(),note:note.trim(),company})}); }catch(e){}
  closeOverlay();
  await loadActiveSituations(); await loadShortlist();
  if(CURRENT_CIK===cik && CURRENT_VIEW==="company") openCompany(cik);
}
async function fullSweep(){
  if(!confirm("Run a full SEC activist sweep across all ~1,489 companies?\n\nThis is the authoritative pass — it adds newly-found situations and clears stale ones (like a mis-attributed filer). It takes ~10 minutes in the background; leave the app deployed and don't redeploy while it runs.")) return;
  const btn=document.getElementById("sweepBtn"); if(btn){ btn.disabled=true; btn.textContent="↻ Sweeping… (~10 min)"; }
  try{ const r=await (await fetch("/api/sweep-activists",{method:"POST"})).json();
    alert(r.message||"Full sweep started.");
  }catch(e){ alert("Could not start the sweep — try again in a moment."); }
}
function openSituationAdd(){
  const pool={}; [...SHORTLIST, ...ACTIVE].forEach(c=>{ if(c&&c.cik) pool[c.cik]={cik:c.cik,company:c.company,ticker:c.ticker}; });
  const list=Object.values(pool).sort((a,b)=>(a.company||"").localeCompare(b.company||""));
  window._SITPOOL=list;
  openModalRaw("Add a company to active situations", `
    <div class="hint" style="margin:0 0 10px">Pick a tracked company, then choose how to tag it. (To add a name that isn't tracked yet, open it from the dashboard table first.)</div>
    <div class="sit-field"><input id="sitSearch" placeholder="Search company or ticker…" oninput="renderSitPool(this.value)" autofocus></div>
    <div id="sitPool"></div>`);
  renderSitPool("");
}
function renderSitPool(q){
  q=(q||"").toLowerCase().trim();
  const list=(window._SITPOOL||[]).filter(c=>!q || (c.company||"").toLowerCase().includes(q) || (c.ticker||"").toLowerCase().includes(q)).slice(0,40);
  const el=document.getElementById("sitPool"); if(!el) return;
  el.innerHTML = list.length ? list.map(c=>`<button class="sit-pick" onclick="openSituationModal('${esc(c.cik)}')">${esc(c.company)} <span style="color:var(--muted)">· ${esc(c.ticker||"")}</span></button>`).join("")
    : `<div class="empty">No matches.</div>`;
}

/* ===== Watchlist (no inline notes — notes live on the profile) ===== */
async function loadWatchlist(){
  try{ const d=await (await fetch("/api/watchlist")).json();
    const items=d.items||[]; WATCHLIST_SET=new Set(items.map(i=>i.cik)); items.forEach(regInfo);
    const nav=document.getElementById("navWatch"); if(nav) nav.textContent=items.length?`(${items.length})`:"";
    const el=document.getElementById("watchlistBody"); if(!el) return;
    if(!items.length){ el.innerHTML=`<div class="empty">No companies yet. Click the ☆ star next to any company to track it here.</div>`; return; }
    el.innerHTML = items.map(c=>{
      const dot=c.has_note?`<span class="notedot" title="has notes">●</span>`:"";
      return `<div class="wl-row" onclick="openCompany('${esc(c.cik)}')">
        <div>
          <div class="wl-top"><span class="co-link">${esc(c.company)}</span><span class="co-meta">${esc(c.ticker)}</span>${statusBadge(c.status)}${dot}</div>
          ${c.signals?`<div class="wl-sig">${esc(c.signals)}</div>`:""}
        </div>
        <div style="display:flex; align-items:center; gap:10px;">${c.vuln!=null?vulnCell(c.vuln):""}
          <button class="ghost" onclick="event.stopPropagation();toggleStar('${esc(c.cik)}')">Remove</button></div>
      </div>`;
    }).join("");
  }catch(e){}
}
function statusBadge(s){
  if(s==="flagged") return `<span class="wl-badge ok">on shortlist</span>`;
  if(s==="active") return `<span class="wl-badge warn">activist engaged</span>`;
  if(s==="inactive") return `<span class="wl-badge gone">delisted / inactive</span>`;
  return `<span class="wl-badge muted">no longer flagged</span>`;
}
async function toggleStar(cik, el){
  const on=WATCHLIST_SET.has(cik); const info=COMPANY_INFO[cik]||{};
  try{
    if(on){ await fetch("/api/watchlist/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik})}); WATCHLIST_SET.delete(cik); }
    else { await fetch("/api/watchlist/add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik,ticker:info.ticker||"",company:info.company||""})}); WATCHLIST_SET.add(cik); }
  }catch(e){ return; }
  if(el && el.classList && el.classList.contains("star")) el.textContent=WATCHLIST_SET.has(cik)?'★':'☆';
  const pb=document.getElementById("cvStar"); if(pb && CURRENT_CIK===cik) pb.textContent=WATCHLIST_SET.has(cik)?'★ On watchlist':'☆ Add to watchlist';
  loadWatchlist(); renderLeads(); loadActiveSituations();
}

/* ===== Company profile (full page, tabbed) ===== */
async function openCompany(cik){
  showView("company");
  document.getElementById("companyView").innerHTML=`<div class="empty">Loading company profile…</div>`;
  try{
    const d=await (await fetch("/api/company?cik="+encodeURIComponent(cik))).json();
    if(!d.ok){ document.getElementById("companyView").innerHTML=`<div class="empty">Could not load this company.</div>`; return; }
    regInfo({cik, ticker:d.ticker, company:d.company}); CURRENT_CIK=cik; CURRENT_DATA=d; CURRENT_TAB="overview";
    renderCompany();
  }catch(e){ document.getElementById("companyView").innerHTML=`<div class="empty">Network error loading company.</div>`; }
}
function renderCompany(){
  const d=CURRENT_DATA, o=d.overview||{}, vi=vulnInfo(d.vuln);
  const facts=`<div class="cv-facts">
    ${kvMini("Market cap", fmtCap(d.market_cap))}
    ${kvMini("Price / book", fmtNum((d.financials||{}).pb_ratio))}
    ${kvMini("vs S&amp;P 1-yr", (d.tsr&&d.tsr.gap!=null)?`<span style="color:${d.tsr.gap<0?'var(--hot)':'var(--ok)'}">${(d.tsr.gap>0?"+":"")+(d.tsr.gap*100).toFixed(0)+" pts"}</span>`:"—")}
    ${kvMini("Next earnings", (d.earnings&&d.earnings.next_date)?fmtDateY(d.earnings.next_date):"—")}</div>`;
  const trend = (d.history&&d.history.length>=2)
    ? `<div class="cv-trend"><span class="lab">Rating trend</span>${sparkline(d.history,vi.col)}<span style="color:${vi.col}; font-size:12px; font-weight:600;">${d.week_change>0?"▲ rising":d.week_change<0?"▼ easing":"steady"}</span></div>`
    : "";
  const sm=d.situation_meta||{}; const stier=d.situation_tier||"";
  let warn="";
  if(d.active_situation){
    const tm=tierMeta(stier);
    const url=sm.url||(d.activist&&d.activist.url);
    const lab=sm.label||(d.activist&&d.activist.label)||"Activist engaged";
    const dt=sm.date?` (${esc(fmtDateMD(sm.date))})`:"";
    warn=`<div class="cv-warn" style="border-color:color-mix(in srgb, ${tm.col} 50%, transparent)">
      <span class="as-tier-badge cv-tierbadge" style="background:color-mix(in srgb, ${tm.col} 16%, transparent);color:${tm.col};border-color:color-mix(in srgb, ${tm.col} 45%, transparent)">${tm.label}</span>
      ⚠ Activist already engaged — ${esc(lab)}${dt}${url?` <a href="${esc(url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">view source ↗</a>`:""}
      ${freshBadge(sm)}</div>`;
  } else if(d.manual_situation==="exclude"){
    warn=`<div class="cv-warn" style="border-color:var(--line2);color:var(--muted)">Manually marked as <b>not</b> an active situation. <span style="cursor:pointer;text-decoration:underline" onclick="openSituationModal('${esc(d.cik)}')">change</span></div>`;
  }
  const star=WATCHLIST_SET.has(d.cik)?'★ On watchlist':'☆ Add to watchlist';
  const npeers=(d.peer_analysis&&d.peer_analysis.peers)?d.peer_analysis.peers.length:0;
  const _advc=((d.advisors&&d.advisors.firms)?d.advisors.firms.length:0)+(commsCompetitor((d.contacts||{}).comms_name)?1:0);
  const tabs=[["overview","Overview"],["evidence","Evidence",(d.evidence||[]).length],["financials","Financials"]];
  if(npeers) tabs.push(["peers","Peer analysis",npeers]);
  tabs.push(["ownership","Ownership & insiders"],["advisors","Advisors",_advc||undefined],["filings","Filings & news"],["notes","Notes"]);
  const tabbar=tabs.map(t=>`<div class="cv-tab ${CURRENT_TAB===t[0]?"active":""}" onclick="switchTab('${t[0]}')">${t[1]}${t[2]?` <span class="cv-tabcount">${t[2]}</span>`:""}</div>`).join("");
  document.getElementById("companyView").innerHTML=`
    <div class="cv-back" onclick="showView(CURRENT_VIEW)"><i class="ti ti-arrow-left"></i> Back</div>
    <div class="cv-head">
      <div><div class="cv-title">${esc(d.company)}</div>
        <div class="cv-meta">${[d.ticker,o.sector||o.industry,o.exchange].filter(Boolean).map(esc).join(" · ")}${d.first_flagged?` · first flagged ${esc(fmtDateY(d.first_flagged))}`:""}</div></div>
      <div class="cv-actions">
        <button class="ghost" id="cvStar" onclick="toggleStar('${esc(d.cik)}',this)">${star}</button>
        <button class="ghost" onclick="openSituationModal('${esc(d.cik)}')">⚑ Active situation</button>
        <button class="ghost" onclick="switchTab('notes')">✎ Notes</button>
      </div>
    </div>
    ${warn}
    <div class="cv-band">
      <div class="cv-gauge">${gaugeSvg(d.vuln)}<div class="cv-rank">matches the activist-target profile</div></div>
      <div>${facts}${trend}</div>
    </div>
    <div class="cv-tabs">${tabbar}</div>
    <div class="cv-body" id="cvBody"></div>`;
  renderTab();
}
function switchTab(t){ CURRENT_TAB=t;
  document.querySelectorAll(".cv-tab").forEach(el=>el.classList.remove("active"));
  renderCompany();
}
function evCard(e){
  const src=e.url?`<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.source||"source")} ↗</a>`:esc(e.source||"");
  const valCls=e.key==="insider_buying"?"evval good":"evval";
  const val=e.value?`<span class="${valCls}">${esc(e.value)}</span>`:"";
  const ctx=e.context?`<div class="evctx">${esc(e.context)}</div>`:"";
  const mp=[]; if(e.inputs)mp.push(esc(e.inputs)); if(e.period)mp.push(esc(e.period));
  const math=mp.length?`<div class="evmath">${mp.join(" · ")}</div>`:"";
  const sl=(e.source||e.url)?`<div class="evsrc">Source: ${src}</div>`:"";
  return `<div class="evrow"><div class="evtop"><span class="evlabel">${esc(e.label)}</span>${val}</div>${ctx}${math}${sl}</div>`;
}
/* Annual-meeting helpers: label past vs upcoming, and derive the upcoming director-nomination
   window from the proxy's advance-notice day-counts (anchored to the meeting anniversary). */
function agmIsPast(iso){
  if(!iso) return false; const p=String(iso).split('-'); if(p.length<3) return false;
  const d=new Date(+p[0],+p[1]-1,+p[2]); const t=new Date(); t.setHours(0,0,0,0); return d<t;
}
function nomWindow(meetingISO,minD,maxD){
  if(!meetingISO) return null;
  const p=String(meetingISO).split('-'); if(p.length<3) return null;
  const base=new Date(+p[0],+p[1]-1,+p[2]); if(isNaN(base)) return null;
  // Exact day-counts come from the proxy's advance-notice bylaw when we can parse them; otherwise
  // fall back to the standard 90–120-day window most US public companies use (labeled as such).
  const exact=!!(minD&&maxD); minD=minD||90; maxD=maxD||120;
  const now=new Date(); now.setHours(0,0,0,0);
  const iso=x=>`${x.getFullYear()}-${String(x.getMonth()+1).padStart(2,'0')}-${String(x.getDate()).padStart(2,'0')}`;
  const minus=(x,n)=>{const y=new Date(x); y.setDate(y.getDate()-n); return y;};
  const anniv=new Date(base); anniv.setFullYear(now.getFullYear());
  let close=minus(anniv,minD),guard=0;
  while(close<now&&guard<4){ anniv.setFullYear(anniv.getFullYear()+1); close=minus(anniv,minD); guard++; }
  return {open:iso(minus(anniv,maxD)), close:iso(close), exact:exact};
}

function daysUntil(iso){
  if(!iso) return null; const p=String(iso).split('-'); if(p.length<3) return null;
  const d=new Date(+p[0],+p[1]-1,+p[2]); if(isNaN(d)) return null;
  const t=new Date(); t.setHours(0,0,0,0); return Math.round((d-t)/86400000);
}
function humanIn(n){ return n<=0?"today":(n===1?"tomorrow":"in "+n+" days"); }

/* Shared: timing-&-catalysts + who-to-reach strip (used on profile overview + pitch kit) */
function pitchStrip(d){
  const sig=d.signals||""; const cat=[];
  if(/CEO\/exec departure/i.test(sig)) cat.push("Exec departure");
  if(/earnings miss|guidance cut/i.test(sig)) cat.push("Earnings miss");
  if(/impairment/i.test(sig)) cat.push("Impairment");
  const ernDate=(d.earnings&&d.earnings.next_date)?d.earnings.next_date:null;
  const gov=d.governance||{};
  const mtgDate=gov.annual_meeting_date||null;
  const mtgPast=agmIsPast(mtgDate);
  const nom=nomWindow(mtgDate,gov.nom_min_days,gov.nom_max_days);
  const tpills=[...(ernDate?["Earnings "+fmtDateMD(ernDate)]:[]),
                ...(mtgDate?[(mtgPast?"Last AGM ":"Next AGM ")+fmtDateMD(mtgDate)]:[]),
                ...(nom?["Nominate "+(nom.exact?"by ":"~")+fmtDateMD(nom.close)]:[]),
                ...cat].map(x=>`<span class="pill2">${esc(x)}</span>`).join("");
  const _cands=[];
  if(ernDate) _cands.push({label:"Earnings",d:daysUntil(ernDate)});
  if(nom) _cands.push({label:"Nomination deadline",d:daysUntil(nom.close)});
  if(mtgDate&&!mtgPast) _cands.push({label:"Annual meeting",d:daysUntil(mtgDate)});
  const _next=_cands.filter(c=>c.d!=null&&c.d>=0).sort((a,b)=>a.d-b.d)[0];
  const timingLines=[
    _next?`<b>Next catalyst:</b> ${esc(_next.label)} ${humanIn(_next.d)}.`:"",
    ernDate?`Next print <b>${esc(fmtDateY(ernDate))}</b> — often the most receptive window to reach out.`:"",
    nom?`Board-nomination window <b>${esc(fmtDateY(nom.open))} – ${esc(fmtDateY(nom.close))}</b> — the deadline for a dissident to nominate directors (${nom.exact?"per the company's bylaw":"standard 90–120-day window; confirm bylaws"}).`:""
  ].filter(Boolean).join("<br>");
  const timingBody=(ernDate||mtgDate||cat.length)
    ? `${tpills}${timingLines?`<br>${timingLines}`:""}`
    : `<span class="gov-note">No near-term catalyst on the calendar.</span>`;
  const ct=d.contacts||{};
  const ctRow=(label,name,email,phone)=>{
    if(!name&&!email&&!phone) return "";
    const init=name?name.trim().split(/\s+/).map(s=>s[0]).slice(0,2).join("").toUpperCase():label.slice(0,2).toUpperCase();
    const det=[email?`<div class="em">${esc(email)}</div>`:"",phone?`<div class="ph">${esc(fmtPhone(phone))}</div>`:""].join("");
    return `<div class="ctc"><div class="av">${esc(init)}</div><div><div class="nm">${esc(name||label)}</div><div class="rl">${esc(label)}</div></div><div class="det">${det}</div></div>`;
  };
  const irRow=ctRow("Investor Relations",ct.ir_name,ct.ir_email,ct.ir_phone);
  // Comms firm intentionally lives on the Advisors tab now (it's a competitor, not an outreach
  // target). "Who to reach" is the emailable IR contact only.
  const reachHtml=irRow
    ? irRow
    : `<span class="gov-note">No IR contact parsed yet — pulled from recent 8-K press releases on the daily run.</span>
       <div class="links" style="margin-top:8px;"><a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||"")+" investor relations contact")}" target="_blank" rel="noopener">Search IR ↗</a></div>`;
  return `<div class="strip2">
      <div><div class="mh3" style="margin:0 0 10px">Timing &amp; catalysts</div><div class="timing">${timingBody}</div></div>
      <div><div class="mh3" style="margin:0 0 10px">Who to reach</div>${reachHtml}</div></div>`;
}
/* External strategic-comms / IR firms (FGS competitors). We only surface an incumbent comms firm
   when the parsed media contact IS one of these — an in-house media person or a parsing artifact
   ("Customer Care", the company's own name, an individual) is not a comms advisor and must not show
   here. FGS Global's own brands (Sard Verbinnen, Finsbury) are excluded so we never flag a client. */
const _COMMS_RE=/\b(joele frank|wilkinson brimmer|edelman|icr|westwicke|brunswick group|kekst|teneo|abernathy macgregor|gladstone place|prosek|longacre square|collected strategies|reevemark|gasthalter|dukas linden|sloane (?:&|and) co|solebury trout|trout group|alpha ir|stanton prg|fti consulting|gagnier|h\/advisors)\b/i;
function commsCompetitor(name){
  const s=name||"";
  if(!s) return "";
  if(/fgs global|sard verbinnen|finsbury/i.test(s)) return "";   // FGS's own brands — not a competitor
  return _COMMS_RE.test(s)?name:"";
}
/* Advisors tab — who already advises this company (banks + law firms scanned from its deal 8-Ks,
   plus the incumbent comms firm when it's a recognized external firm). Landscape/competitive intel,
   NOT an outreach list. */
function advRow(name){
  const init=(name||"").trim().split(/\s+/).map(s=>s[0]).slice(0,2).join("").toUpperCase();
  return `<div class="ctc"><div class="av">${esc(init)}</div><div><div class="nm">${esc(name)}</div></div></div>`;
}
function advisorsHtml(d){
  const adv=(d.advisors&&d.advisors.firms)?d.advisors.firms:[];
  const banks=adv.filter(a=>a.type==="bank"), laws=adv.filter(a=>a.type==="law");
  const ct=d.contacts||{}; const comms=commsCompetitor(ct.comms_name);
  const advSrc=(d.advisors&&d.advisors.source_url)||null, advDate=(d.advisors&&d.advisors.source_date)||null;
  const commsSrc=ct.contacts_source||null;
  if(!banks.length&&!laws.length&&!comms){
    return `<div class="empty">No advisors identified yet.<br><span style="color:var(--muted);font-size:13px;">The scan reads each tracked company's recent deal / strategic-review 8-Ks (Items 1.01, 2.01, 7.01, 8.01) and matches the banks and law firms named in them against a curated allowlist. Most companies have no recent deal, so this stays empty until one appears — coverage fills in over successive daily runs.</span></div>`;
  }
  // Every section links back to the exact SEC filing the names were pulled from.
  const srcLink=(url,date)=>url?`<div class="gov-note" style="text-transform:none;letter-spacing:0;margin:6px 0 2px;">Source: <a class="extlink" href="${esc(url)}" target="_blank" rel="noopener">SEC filing${date?` · ${esc(fmtDateY(date))}`:""} ↗</a></div>`:`<div class="gov-note" style="text-transform:none;letter-spacing:0;margin:6px 0 2px;color:var(--dim)">Source filing link pending next refresh.</div>`;
  const intro=`<div class="verdict" style="font-size:13.5px;">Who's already advising this company, pulled from its own recent SEC filings. This is <b>landscape intel, not an outreach list</b> — these are incumbents you'd be working alongside or displacing.</div>`;
  const grp=(title,rows,note,footer)=>rows.length?`<div class="mh3">${title}</div>${note||""}<div class="panel" style="padding:4px 0;">${rows.join("")}</div>${footer||""}`:"";
  // Banks + law come from the same deal 8-K (the scan records the newest filing that named them),
  // so the source link sits under whichever of the two is shown last.
  const bankB=grp("Financial advisors",banks.map(a=>advRow(a.name)),"",laws.length?"":srcLink(advSrc,advDate));
  const lawB=grp("Legal advisors",laws.map(a=>advRow(a.name)),"",srcLink(advSrc,advDate));
  const commsNote=`<div class="gov-note" style="text-transform:none;letter-spacing:0;margin:0 0 6px;">Incumbent comms firm — a competitor, not an outreach target.</div>`;
  const commsB=comms?grp("Former Comms Advisor",[advRow(comms)],commsNote,srcLink(commsSrc,null)):"";
  return intro+bankB+lawB+commsB;
}

/* ===== Pitch kit (home) ===== */
let PK_LEAD_CIK=null;
/* The "publish day" flips at 6 AM ET (when the daily rebuild + digest run), so the lead
   of the day is fresh each morning and stable through the day, in step with the new data. */
function publishDayIndex(){
  try{
    const p=Object.fromEntries(new Intl.DateTimeFormat("en-US",{timeZone:"America/New_York",
      year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",hour12:false})
      .formatToParts(new Date()).map(x=>[x.type,x.value]));
    let ms=Date.UTC(+p.year,+p.month-1,+p.day);
    if(+p.hour<6) ms-=86400000;                  // before 6 AM ET -> still on yesterday's kit
    return Math.floor(ms/86400000);
  }catch(e){ const n=new Date(); return Math.floor((n-new Date(n.getFullYear(),0,0))/86400000); }
}
/* A short, DISTINCTIVE descriptor so target cards don't all read the same generic signals. */
const ARCH_LABEL={cash_laggard:"Cash-rich laggard",turnaround:"Operational turnaround",
  value:"Cheap / break-up candidate",governance:"Entrenched · governance",default:"Multi-signal profile"};
const _DISTINCT=["cash-heavy","staggered","poison pill","dual-class","insider selling","say-on-pay",
  "ceo/exec departure","impairment","under-levered","1-yr stock return","shrinking","bloated sg&a",
  "low return on assets","low operating margin","cheap"];
function distinctiveSignal(sig){
  const parts=(sig||"").split(" + ").map(s=>s.trim()).filter(Boolean);
  if(!parts.length) return "";
  for(const key of _DISTINCT){ const hit=parts.find(p=>p.toLowerCase().includes(key)); if(hit) return hit; }
  return parts[0];
}
function leadHook(c){
  let arch="";
  try{ const p=typeof c.pitch==="string"?JSON.parse(c.pitch||"{}"):(c.pitch||{}); arch=ARCH_LABEL[p.archetype]||""; }catch(e){}
  return [arch,distinctiveSignal(c.signals)].filter(Boolean).join(" · ");
}
const NEWS_RANK={ownership:0,activist:0,exec:1,distress:2,proxy:3,movers:4,market:5,other:6};
function pkPickLead(){
  const pool=(SHORTLIST||[]).filter(c=>!c.active_situation);
  if(!pool.length) return null;
  const byVuln=(a,b)=>(b.vuln||0)-(a.vuln||0);
  const hasCatalyst=c=>/recent (ceo|exec|earnings|results|impairment|layoff|leadership)/i.test(c.signals||"") || (c.week_change>0);
  // Catalyst names first (fresh angle), then PADDED with the highest-vuln names so the
  // rotation pool is always deep enough to give a different lead each day. (Without the
  // padding, a day with only one catalyst name would lock the lead to that name forever.)
  const cats=pool.filter(hasCatalyst).sort(byVuln);
  const rest=pool.filter(c=>!hasCatalyst(c)).sort(byVuln);
  const top=cats.concat(rest).slice(0, Math.min(8, pool.length));
  if(!top.length) return null;
  const idx=((publishDayIndex()%top.length)+top.length)%top.length;
  return top[idx];
}
function effPitch(d){
  // Prefer the Haiku-polished pitch when present; fall back to the templated one. Keeps the
  // archetype from the deterministic pitch. _ai flags that prose came from the AI layer.
  const ai=(d&&d.ai_pitch)||{};
  if(ai.thesis){
    const p=Object.assign({}, d.pitch||{}, {thesis:ai.thesis, _ai:true});
    if(ai.points&&ai.points.length) p.points=ai.points;
    return p;
  }
  return (d&&d.pitch)||{};
}
function pkHero(d){
  const vi=vulnInfo(d.vuln); const pitch=effPitch(d); const points=pitch.points||[]; const o=d.overview||{};
  const pts=points.length?`<ol class="pitch-points">${points.map((p,i)=>`<li><span class="num">${i+1}</span><span>${esc(p)}</span></li>`).join("")}</ol>`:"";
  const thesis=pitch.thesis?`<div class="verdict" style="margin-top:14px;">${esc(pitch.thesis)}</div>`:`<div class="verdict" style="margin-top:14px;">${esc(o.description||"")}</div>`;
  return `<div class="pk-hero">
    <div class="pk-hero-top">
      <div><div class="cv-title" style="font-size:27px; cursor:pointer;" onclick="openCompany('${esc(d.cik)}')">${esc(d.company)}</div>
        <div class="cv-meta">${[d.ticker,o.sector||o.industry,fmtCap(d.market_cap)].filter(Boolean).map(esc).join(" · ")}</div></div>
      <div style="text-align:center; flex-shrink:0;"><div class="vband ${vi.cls}" style="font-size:15px;letter-spacing:.08em;">${vulnBand(d.vuln)}</div></div>
    </div>
    ${thesis}${pts}${pitchStrip(d)}
    <div style="margin-top:16px;"><button onclick="openCompany('${esc(d.cik)}')">View full pitch →</button></div>
  </div>`;
}
async function renderPitchKit(){
  const dEl=document.getElementById("pkDate");
  if(dEl) dEl.textContent=new Date().toLocaleDateString(undefined,{weekday:"long",month:"long",day:"numeric",year:"numeric"});
  const host=document.getElementById("pkLead"); if(!host) return;
  // Backend is the source of truth for the lead (locked per day, no-repeat rotation, and
  // identical to the emailed pitch kit). Fall back to the client picker only if it's unreachable.
  let leadCik=null;
  try{ leadCik=(await (await fetch("/api/lead")).json()).cik; }catch(e){}
  if(!leadCik){ const f=pkPickLead(); leadCik=f?f.cik:null; }
  if(!leadCik){ host.innerHTML=`<div class="empty">No leads yet — data is still loading.</div>`; }
  else{
    PK_LEAD_CIK=leadCik;
    if(!host.dataset.cik || host.dataset.cik!==leadCik){ host.innerHTML=`<div class="empty">Loading the lead…</div>`; }
    try{ const d=await (await fetch("/api/company?cik="+encodeURIComponent(leadCik))).json();
      if(d.ok){ host.innerHTML=pkHero(d); host.dataset.cik=leadCik; } }catch(e){}
  }
  const tg=document.getElementById("pkTargets");
  if(tg){ const targets=(SHORTLIST||[]).filter(c=>c.cik!==PK_LEAD_CIK).slice(0,4);
    tg.innerHTML=targets.map(c=>{ const i=vulnInfo(c.vuln); const hook=leadHook(c);
      return `<div class="nr-card" style="flex:1 1 260px; align-items:flex-start;" onclick="openCompany('${esc(c.cik)}')">
        <span class="vchip ${i.cls}">${c.vuln==null?"—":vulnBand(c.vuln)}</span>
        <div class="nr-co">${esc(c.company)}<div class="meta">${esc(c.ticker||"")}${hook?" · "+esc(hook):""}</div></div></div>`; }).join("")||`<div class="empty">—</div>`; }
  const nEl=document.getElementById("pkNews");
  if(nEl){ const news=(FEED.news_top||FEED.news||[]).slice()
      .sort((a,b)=>(NEWS_RANK[newsCategory(a.headline)]??9)-(NEWS_RANK[newsCategory(b.headline)]??9)).slice(0,5);
    nEl.innerHTML=news.length?news.map(newsItemRow).join(""):`<div class="empty">No headlines yet.</div>`; }
  const fEl=document.getElementById("pkFilings");
  if(fEl){ const fil=(FEED.filings||[]).slice(0,5); fEl.innerHTML=fil.length?fil.map(filingItemRow).join(""):`<div class="empty">No filings yet.</div>`; }
}
function fmtMetricVal(key,v){
  if(v==null) return "—";
  if(key==="pb_ratio") return fmtNum(v)+"x";
  if(key==="ev_ebitda") return v.toFixed(1)+"x";
  return (v*100).toFixed(1)+"%";
}
function finCard(m){
  const v=fmtMetricVal(m.key,m.value);
  const cut=m.cutoff!=null?fmtMetricVal(m.key,m.cutoff):null;
  const cls=m.verdict==="bad"?"bad":m.verdict==="opp"?"opp":"mid";
  const lab=m.verdict==="bad"?"Vulnerability":m.verdict==="opp"?"Opportunity":"In line";
  const pos=m.pct!=null?Math.max(2,Math.min(98,Math.round(m.pct*100))):null;
  const bar=pos!=null?`<div class="ptrack"><div class="pmark" style="left:${pos}%"></div></div>`:`<div class="ptrack"></div>`;
  const cap=cut!=null?`Peer cutoff ${cut} · ${m.n||0} sector peers`:`${m.n||0} sector peers`;
  return `<div class="metric"><div class="metric-top"><span class="mk">${esc(m.label)}</span><span class="chip ${cls}">${lab}</span></div>
    <div class="metric-top" style="margin-top:5px;"><span class="mv">${v}</span></div>${bar}<div class="mc">${esc(cap)}</div></div>`;
}
function peerTable(pa){
  const f=(v)=>v==null?"—":((v>=0?"+":"")+(v*100).toFixed(0)+"%");
  const x=(v)=>v==null?"—":v.toFixed(1)+"x";
  const tcol=(v)=>v==null?"":(v<0?"color:var(--hot)":"color:var(--ok)");
  const bd="border-bottom:1px solid rgba(0,0,0,.08);";
  const th=(t,a)=>`<th style="text-align:${a};padding:7px 8px;${bd}color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;">${t}</th>`;
  const td=(t,a,extra)=>`<td style="text-align:${a};padding:7px 8px;${bd}${extra||""}">${t}</td>`;
  const row=(o,extra)=>`<tr>${td((o.name?esc(o.name):esc(o.ticker||"—"))+(o.ticker&&o.name?` <span style="color:var(--muted)">${esc(o.ticker)}</span>`:""),"left",extra)}
     ${td(f(o.tsr_1y),"right",extra+tcol(o.tsr_1y))}${td(f(o.operating_margin),"right",extra)}${td(x(o.pb_ratio),"right",extra)}${td(x(o.ev_ebitda),"right",extra)}</tr>`;
  const self=pa.self||{}, med=pa.median||{};
  const peers=(pa.peers||[]).slice().sort((a,b)=>((b.tsr_1y==null?-9:b.tsr_1y)-(a.tsr_1y==null?-9:a.tsr_1y)));
  const head=`<tr>${th("Company","left")}${th("1-yr return","right")}${th("Op margin","right")}${th("P / B","right")}${th("EV/EBITDA","right")}</tr>`;
  const selfRow=row(self,"background:rgba(47,95,166,.08);font-weight:700;");
  const medRow=row({name:"Peer median",tsr_1y:med.tsr_1y,operating_margin:med.operating_margin,pb_ratio:med.pb_ratio,ev_ebitda:med.ev_ebitda},"font-style:italic;color:var(--muted);");
  return `<table style="width:100%;border-collapse:collapse;font-size:13px;">${head}${selfRow}${medRow}${peers.map(o=>row(o,"")).join("")}</table>`;
}
function pillarsHtml(ev){
  if(!ev.length) return `<div class="empty">No evidence recorded.</div>`;
  const groups={}; ev.forEach(e=>{ const p=PILLAR_OF[e.key]||"event"; (groups[p]=groups[p]||[]).push(e); });
  return PILLAR_ORDER.filter(p=>groups[p]).map(p=>{ const m=PILLAR_META[p];
    return `<div class="pillar"><div class="pillar-h"><span class="pillar-t">${m.t}</span><span class="pillar-n">${groups[p].length}</span></div>
      <div class="pillar-d">${m.d}</div><div class="evlist">${groups[p].map(evCard).join("")}</div></div>`; }).join("");
}
function renderTab(){
  const d=CURRENT_DATA, body=document.getElementById("cvBody"); if(!body) return;
  const f=d.financials||{}, o=d.overview||{};
  if(CURRENT_TAB==="overview"){
    const top=(d.signals||"").split(" + ").filter(Boolean);
    const chips=top.map(s=>`<span class="sigchip${/say-on-pay|vote/.test(s)?" warnchip":""}">${esc(s)}</span>`).join("");
    const tsr=d.tsr||{}; let tsrPanel="";
    const tsrRow=(label,stock,spy,gap)=>{
      if(stock==null) return "";
      const gcol=(gap!=null)?(gap<0?"var(--hot)":"var(--ok)"):"var(--muted)";
      const gtxt=(gap!=null)?((gap>0?"+":"")+(gap*100).toFixed(0)+" pts"):"—";
      const c="padding:5px 8px;border-bottom:1px solid var(--line);font-family:var(--serif);font-size:15px;";
      return `<tr><td style="padding:5px 8px;border-bottom:1px solid var(--line);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em;">${label}</td>`+
        `<td style="${c}">${fmtPct(stock)}</td>`+
        `<td style="${c}color:var(--muted);">${fmtPct(spy)}</td>`+
        `<td style="${c}color:${gcol};">${gtxt}</td></tr>`;
    };
    const tsrRows=tsrRow("1-yr",tsr.tsr_1y,tsr.spy_1y,tsr.gap)
      +tsrRow("3-yr",tsr.tsr_3y,tsr.spy_3y,tsr.gap_3y)
      +tsrRow("5-yr",tsr.tsr_5y,tsr.spy_5y,tsr.gap_5y);
    if(tsrRows){
      const th="text-align:left;padding:2px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);font-weight:600;";
      tsrPanel=`<div class="tsr-panel"><div class="tsr-h">Price return vs S&amp;P 500</div>
        <table style="width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;">
        <thead><tr><th style="${th}"></th><th style="${th}">This stock</th><th style="${th}">S&amp;P 500</th><th style="${th}">Gap</th></tr></thead>
        <tbody>${tsrRows}</tbody></table></div>`;
    }
    const g=d.governance||{};
    const mtgDate=g.annual_meeting_date||null;
    const mtgPast=agmIsPast(mtgDate);
    const nom=nomWindow(mtgDate,g.nom_min_days,g.nom_max_days);
    const ern=[
      (d.earnings&&d.earnings.next_date)?`<div class="timing"><i>◷</i> Next earnings <b>${esc(d.earnings.next_date)}</b> — often the most receptive window to reach out</div>`:"",
      mtgDate?`<div class="timing"><i>◷</i> ${mtgPast?"Last":"Next"} annual meeting <b>${esc(fmtDateY(mtgDate))}</b></div>`:"",
      nom?`<div class="timing"><i>◷</i> Board-nomination window <b>${esc(fmtDateY(nom.open))} – ${esc(fmtDateY(nom.close))}</b> <span style="color:var(--dim)">${nom.exact?"(from advance-notice bylaw)":"(standard 90–120-day window; confirm in bylaws)"}</span></div>`:""
    ].join("");
    const gb=(on,t)=>`<span class="gov-badge ${on?'on':'off'}">${on?'●':'○'} ${t}</span>`;
    const anyGov=g.classified_board||g.poison_pill||g.dual_class;
    const govRow=`<div class="gov-row">${gb(g.classified_board,"Classified board")}${gb(g.poison_pill,"Poison pill")}${gb(g.dual_class,"Dual-class stock")}`
      +(g.proxy_url?`<a class="extlink" href="${esc(g.proxy_url)}" target="_blank" rel="noopener">DEF 14A ↗</a>`:"")
      +(!anyGov&&!g.proxy_url?`<span class="gov-note">proxy not yet parsed</span>`:"")+`</div>`;
    const tk=d.ticker, L=[];
    if(tk) L.push(`<a class="extlink" href="https://finance.yahoo.com/quote/${encodeURIComponent(tk)}" target="_blank" rel="noopener">Yahoo Finance ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||tk||"")+" stock")}" target="_blank" rel="noopener">Google ↗</a>`);
    L.push(`<a class="extlink" href="${secUrl(d.cik)}" target="_blank" rel="noopener">SEC EDGAR ↗</a>`);
    if(o.website) L.push(`<a class="extlink" href="${esc(o.website)}" target="_blank" rel="noopener">Company site ↗</a>`);
    L.push(`<a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||"")+" investor relations")}" target="_blank" rel="noopener">IR / contacts ↗</a>`);
    const an=d.analysts||{};
    const aBuy=(an.strong_buy||0)+(an.buy||0), aSell=(an.sell||0)+(an.strong_sell||0), aHold=an.hold||0;
    const analystHtml=(aBuy+aHold+aSell)>0
      ? `<div class="mh3">Analysts <span class="gov-note" style="text-transform:none;letter-spacing:0;">— sell-side view, context only</span></div>
         <div class="verdict" style="font-size:13.5px;"><span style="color:var(--ok)">${aBuy} buy</span> · ${aHold} hold · <span style="color:var(--hot)">${aSell} sell</span>${an.period?` <span class="gov-note">(${esc(an.period)})</span>`:""}</div>`
      : "";
    const px=d.prices||{}; const pchart=priceChart(px.series, px.last_close);
    const pitch=effPitch(d); const points=pitch.points||[];
    const aiTag=pitch._ai?` <span class="gov-note" style="text-transform:none;letter-spacing:0;color:var(--accent);">✦ AI-polished</span>`:"";
    const thesisHtml = pitch.thesis
      ? `<div class="verdict">${esc(pitch.thesis)}</div>`
      : `<div class="verdict">${o.description?esc(o.description):`Flagged on: ${top.slice(0,5).map(esc).join(", ")||"—"}.`}</div>`;
    const ptsHtml = points.length
      ? `<div class="mh3">The pitch</div><ol class="pitch-points">${points.map((p,i)=>`<li><span class="num">${i+1}</span><span>${esc(p)}</span></li>`).join("")}</ol>`
      : "";
    const strip=pitchStrip(d);
    body.innerHTML=`
      <div class="mh3">Why it's a potential target${aiTag}</div>
      ${thesisHtml}
      ${ptsHtml}
      ${strip}
      ${pchart?`<div class="mh3">Price · 1-year</div>${pchart}`:""}
      ${tsrPanel?`<div class="mh3">Returns</div>${tsrPanel}`:""}
      <div class="mh3">Governance</div>${govRow}
      ${analystHtml}
      <div class="mh3">Screen signals</div><div class="sigchips">${chips||"—"}</div>
      ${o.description?`<div class="mh3">About the company</div><div class="desc">${esc(o.description)}</div>`:""}
      <div class="mh3">Quick links</div><div class="links">${L.join("")}</div>`;
  }
  else if(CURRENT_TAB==="advisors"){
    body.innerHTML=advisorsHtml(d);
  }
  else if(CURRENT_TAB==="evidence"){
    body.innerHTML=pillarsHtml(d.evidence||[]);
  }
  else if(CURRENT_TAB==="financials"){
    const fc=d.fin_context||[];
    const finGrid=fc.length?`<div class="fin">${fc.map(finCard).join("")}</div>`:"";
    const kv=(k,v)=>`<div class="kv"><div class="k">${k}</div><div class="v">${v}</div></div>`;
    const rawGrid=`<div class="grid">
      ${kv("Market cap",fmtCap(d.market_cap))}${kv("P / E",fmtNum(f.pe_ratio))}${kv("Profit margin",fmtPct(f.profit_margin))}
      ${kv("Return on equity",fmtPct(f.return_on_equity))}${kv("Revenue",fmtCap(f.revenue))}${kv("Dividend yield",fmtPct(f.dividend_yield))}
      ${kv("52-wk range",(f.week52_low!=null&&f.week52_high!=null)?("$"+fmtNum(f.week52_low)+" – $"+fmtNum(f.week52_high)):"—")}
      ${kv("Analyst target",f.analyst_target!=null?("$"+fmtNum(f.analyst_target)):"—")}</div>`;
    body.innerHTML=(finGrid?`<div class="mh3">Versus peers — what each number means</div>
        <p class="hint" style="margin:-4px 0 12px">Red flags a <b style="color:var(--hot)">vulnerability</b> an activist would attack; gold marks an <b style="color:var(--warn)">opportunity</b> (idle capital, a cheap entry); the bar shows where the company sits in its sector range.</p>${finGrid}`:"")
      +`<div class="mh3">All metrics</div>${rawGrid}`;
  }
  else if(CURRENT_TAB==="peers"){
    const pa=d.peer_analysis||{};
    const rank=pa.rank, rof=pa.rank_of;
    // The rank is taken over the peers that actually have 1-yr returns (rof-1 of them) plus the
    // company itself = rof. State that instead of "6 of 6 against the 14 peers", which read as a
    // contradiction — the company named pa.n peers, but only some have returns to rank against.
    let summary;
    if(rank&&rof){
      const withRet=rof-1;
      const scope=(withRet < (pa.n||0))
        ? `among the ${withRet} of the ${pa.n} peers <b>${esc(d.company)}</b> named that have 1-year returns`
        : `against the ${pa.n} peers <b>${esc(d.company)}</b> named`;
      summary=`<p class="hint" style="margin:-4px 0 12px">Ranked <b>${rank} of ${rof}</b> by 1-year price return ${scope} — its self-selected compensation peer group (DEF 14A).</p>`;
    } else {
      summary=`<p class="hint" style="margin:-4px 0 12px">The compensation peer group <b>${esc(d.company)}</b> named in its own proxy (DEF 14A), versus the metrics we track.</p>`;
    }
    body.innerHTML=`<div class="mh3">Peer analysis — self-selected proxy peer group</div>${summary}
      ${peerTable(pa)}
      <p class="hint" style="margin-top:12px">Peers are the companies the board picked to benchmark executive pay against; we show only those in our coverage. "Underperformance by its own yardstick" is one of the most credible activist arguments. Price return from Finnhub; margins &amp; valuation from SEC XBRL + market data.</p>`;
  }
  else if(CURRENT_TAB==="ownership"){
    const ins=d.insider||{};
    const m=v=>v==null?"—":fmtCap(v);
    const insPanel=(ins.n_buyers||ins.n_sellers)?`<div class="ins-grid">
      ${kvMini("Insider buys", m(ins.buy_value))}${kvMini("Insider sells", m(ins.sell_value))}
      ${kvMini("Net", (ins.net_value!=null?((ins.net_value>=0?"+":"")+fmtCap(Math.abs(ins.net_value))):"—"))}
      ${kvMini("Buyers", ins.n_buyers!=null?ins.n_buyers:"—")}${kvMini("Sellers", ins.n_sellers!=null?ins.n_sellers:"—")}
      ${kvMini("Window", ins.window_days?(ins.window_days+"d"):"—")}</div>
      ${ins.top_url?`<div class="links" style="margin-top:10px;"><a class="extlink" href="${esc(ins.top_url)}" target="_blank" rel="noopener">Largest Form 4 ↗</a></div>`:""}`
      : `<div class="gov-note">No open-market insider trades on record in the recent window.</div>`;
    const insiderEv=(d.evidence||[]).filter(e=>e.key==="insider_selling"||e.key==="insider_buying");
    const se=d.sentiment||{}; let sentHtml="";
    // MSPR (Finnhub monthly buy/sell skew) suppressed (#2): it repeatedly contradicted our own
    // Form-4 flow on the same profile — showing "+100 buying-skewed" for ServiceNow while insiders
    // sold $24M, and "-20 balanced" for Intuit amid $359M of net selling. We rely on the netted
    // Form-4 aggregate above instead; leaving MSPR off rather than print a conflicting readout.
    const ct=d.contacts||{};
    const addr=[ct.address,[ct.city,ct.state].filter(Boolean).join(", "),ct.zip].filter(Boolean).join(" · ");
    const hasContacts=ct.ceo||ct.phone||addr||ct.website||ct.employees;
    const contactsHtml = hasContacts
      ? `<div class="ins-grid" style="grid-template-columns:1fr 1fr;max-width:560px;">
          ${ct.ceo?kvMini("CEO", esc(ct.ceo)):""}
          ${ct.phone?kvMini("Phone", esc(fmtPhone(ct.phone))):""}
          ${addr?kvMini("Headquarters", esc(addr)):""}
          ${ct.employees?kvMini("Employees", ct.employees.toLocaleString()):""}
          ${ct.ipo?kvMini("IPO", esc(ct.ipo)):""}</div>
         <div class="links" style="margin-top:10px;">
          ${ct.website?`<a class="extlink" href="${esc(ct.website)}" target="_blank" rel="noopener">Company site ↗</a>`:""}
          <a class="extlink" href="https://www.google.com/search?q=${encodeURIComponent((d.company||"")+" investor relations")}" target="_blank" rel="noopener">IR / contacts ↗</a></div>`
      : `<div class="gov-note">No contact data yet — add an FMP_API_KEY (then "Run enrichment now") to populate CEO, HQ &amp; IR.</div>`;
    body.innerHTML=`<div class="mh3">Insider activity (Form 4)</div>${insPanel}
      ${insiderEv.length?`<div class="mh3">Detail</div><div class="evlist">${insiderEv.map(evCard).join("")}</div>`:""}
      ${sentHtml}
      <div class="mh3">Contacts</div>${contactsHtml}`;
  }
  else if(CURRENT_TAB==="filings"){
    const filings=(d.filings||[]).map(x=>`<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.company)} — ${esc(x.title)}</a><div class="meta"><span class="tag">${esc(x.form)}</span><span>${fmtDate(x.filed_at)}</span></div></div>`).join("")||`<div class="empty">No recent filings on record.</div>`;
    const news=(d.news||[]).slice()
      .sort((a,b)=>(b.published_at||"").localeCompare(a.published_at||""))
      .sort((a,b)=>((NEWS_PRIORITY[newsCategory(a.headline)]??9)-(NEWS_PRIORITY[newsCategory(b.headline)]??9)))
      .map(x=>{const cat=newsCategory(x.headline);
        return `<div class="row2"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a><div class="meta"><span class="tag cat-${cat}">${CAT_LABEL[cat]||"News"}</span><span class="tag">${esc(x.source)||"news"}</span><span>${fmtDate(x.published_at)}</span></div></div>`;}).join("")||`<div class="empty">No recent matched news.</div>`;
    body.innerHTML=`<div class="mh3">Recent SEC filings</div><div class="dlist">${filings}</div>
      <div class="mh3">Recent news</div><div class="dlist">${news}</div>
      <div style="margin-top:16px;"><a class="extlink" href="${secUrl(d.cik)}" target="_blank" rel="noopener">All SEC filings on EDGAR ↗</a></div>`;
  }
  else if(CURRENT_TAB==="notes"){
    body.innerHTML=`<div class="mh3">Pitch notes <span class="gov-note" style="text-transform:none; letter-spacing:0;">— shared with the team</span></div>
      <textarea class="notes-area" id="notesArea" placeholder="Angle, contacts, status, who's reached out…">${esc(d.note||"")}</textarea>
      <div class="notes-actions"><button onclick="saveNote('${esc(d.cik)}')">Save note</button><span class="notes-msg" id="notesMsg"></span></div>`;
  }
}
async function saveNote(cik){
  const ta=document.getElementById("notesArea"); if(!ta) return;
  const msg=document.getElementById("notesMsg"); const val=ta.value;
  try{ await fetch("/api/note",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cik,note:val})});
    if(CURRENT_DATA) CURRENT_DATA.note=val;
    const c=SHORTLIST.find(x=>x.cik===cik); if(c) c.has_note=!!val.trim();
    if(msg){ msg.textContent="Saved ✓"; setTimeout(()=>{ if(msg) msg.textContent=""; },1500); }
    renderLeads();
  }catch(e){ if(msg) msg.textContent="Error saving"; }
}

/* ===== Actions ===== */
async function manualRefresh(){ const b=document.getElementById("refreshBtn"); b.disabled=true; b.textContent="Refreshing…";
  try{ await fetch("/api/refresh",{method:"POST"}); }catch(e){} await refreshAll(); b.disabled=false; b.textContent="↻ Refresh"; }
async function runEnrichment(){ const b=document.getElementById("enrichBtn"); b.disabled=true; b.textContent="Starting…";
  try{ const r=await fetch("/api/run-enrichment",{method:"POST"}); const d=await r.json(); b.textContent=d.ok?"Running in background ✓":"Error"; }
  catch(e){ b.textContent="Network error"; }
  setTimeout(async()=>{ await refreshAll(); b.disabled=false; b.textContent="⟳ Run enrichment now"; },120000); }
async function refresh13f(){
  if(!confirm("Refresh the 13F activist-holder data now?\n\nResolves each activist fund's latest Form 13F and re-maps holdings onto the universe (~1–2 minutes in the background). Safe to run anytime; it updates the Accumulating tab and the holder boost on leads.")) return;
  const b=document.getElementById("r13fBtn"); if(b){ b.disabled=true; b.textContent="↻ Refreshing… (~1–2 min)"; }
  try{ const r=await (await fetch("/api/refresh-13f",{method:"POST"})).json();
    const m=document.getElementById("r13fMsg"); if(m) m.textContent=r.message||"13F refresh started.";
  }catch(e){ const m=document.getElementById("r13fMsg"); if(m) m.textContent="Could not start the refresh — try again."; }
  setTimeout(async()=>{ await loadAccumulating(); if(b){ b.disabled=false; b.textContent="↻ Refresh 13F holdings"; } },120000);
}
async function subscribe(){ const email=document.getElementById("emailInput").value, msg=document.getElementById("subMsg");
  try{ const r=await fetch("/api/subscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email})}); const d=await r.json();
    msg.textContent=d.ok?d.message:(d.error||"Error"); msg.className="msg "+(d.ok?"ok":"err"); if(d.ok)loadStatus(); }
  catch(e){ msg.textContent="Network error"; msg.className="msg err"; } }
async function unsubscribe(){ const email=document.getElementById("emailInput").value, msg=document.getElementById("subMsg");
  try{ const r=await fetch("/api/unsubscribe",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({email})}); const d=await r.json();
    msg.textContent=d.message||"Done"; msg.className="msg ok"; loadStatus(); }catch(e){ msg.textContent="Network error"; msg.className="msg err"; } }
async function sendTestDigest(){ const msg=document.getElementById("subMsg"); msg.textContent="Sending…"; msg.className="msg";
  try{ const r=await fetch("/api/send-test-digest",{method:"POST"}); const d=await r.json(); msg.textContent=d.message||(d.ok?"Sent.":"Error"); msg.className="msg "+(d.ok?"ok":"err"); }
  catch(e){ msg.textContent="Network error"; msg.className="msg err"; } }
/* ===== Biweekly report: regenerate + approve workflow ===== */
async function regenerateIssue(){
  if(!confirm("Regenerate the biweekly issue now?\n\nThis recomputes fundamentals, scores and theses (5–15 min in the background), then rebuilds the SAME five vetted names with corrected numbers. It re-audits and freezes the issue AWAITING YOUR APPROVAL — nothing is emailed. Continue?")) return;
  const b=document.getElementById("regenBtn"), m=document.getElementById("regenMsg");
  b.disabled=true; b.textContent="Starting…"; m.className="msg"; m.textContent="";
  try{
    const r=await (await fetch("/api/report/generate-now?confirm=1",{method:"POST"})).json();
    if(!r.ok){ m.className="msg err"; m.textContent=r.message||"Could not start."; b.disabled=false; b.textContent="⟳ Regenerate (recompute + rebuild)"; return; }
    m.className="msg"; m.textContent="Running… recomputing data and rebuilding (5–15 min). Safe to leave this page.";
    pollRegen();
  }catch(e){ m.className="msg err"; m.textContent="Network error — try again."; b.disabled=false; b.textContent="⟳ Regenerate (recompute + rebuild)"; }
}
async function pollRegen(){
  const b=document.getElementById("regenBtn"), m=document.getElementById("regenMsg");
  try{
    const s=await (await fetch("/api/report/generate-now/status")).json();
    if(s.running){ m.className="msg"; m.textContent="Running… "+(s.started_at?("started "+String(s.started_at).replace('T',' ').slice(0,19)+" UTC"):"in progress")+". Safe to leave this page."; setTimeout(pollRegen,15000); return; }
    b.disabled=false; b.textContent="⟳ Regenerate (recompute + rebuild)";
    if(s.error){ m.className="msg err"; m.textContent="Regenerate failed: "+s.error+" — check the logs."; return; }
    if(s.result){ m.className="msg ok"; m.textContent="Done ✓ Board: "+((s.result.pin||[]).join(", "))+" · audit "+(s.result.summary||s.result.status||"")+". Press Preview latest to review, then Approve."; return; }
    m.textContent="Idle — nothing has been regenerated yet.";
  }catch(e){ setTimeout(pollRegen,15000); }
}
async function approveIssue(){
  if(!confirm("Approve this issue for the Wednesday 7 AM send?\n\nThis releases it to the FULL distribution list at the next send window. Preview it first.")) return;
  const b=document.getElementById("approveBtn"), m=document.getElementById("regenMsg");
  b.disabled=true; b.textContent="Approving…";
  try{
    let r=await (await fetch("/api/report/approve",{method:"POST"})).json();
    if(r.held && !r.ok){
      if(confirm("The auto-audit HELD this issue:\n\n"+((r.flags||[]).join("\n")||"(open Preview latest for detail)")+"\n\nApprove anyway (override the hold)?")){
        r=await (await fetch("/api/report/approve?override=1",{method:"POST"})).json();
      } else { m.className="msg"; m.textContent="Left on hold — not approved."; b.disabled=false; b.textContent="✓ Approve for send"; return; }
    }
    m.className="msg "+(r.ok?"ok":"err"); m.textContent=r.message||(r.ok?"Approved.":"Could not approve.");
  }catch(e){ m.className="msg err"; m.textContent="Network error — try again."; }
  b.disabled=false; b.textContent="✓ Approve for send";
}
async function holdIssue(){
  if(!confirm("Hold this issue so Wednesday's send SKIPS it?\n\nReversible — you can Approve later.")) return;
  const b=document.getElementById("holdBtn"), m=document.getElementById("regenMsg");
  b.disabled=true; b.textContent="Holding…";
  try{ const r=await (await fetch("/api/report/hold",{method:"POST"})).json();
    m.className="msg "+(r.ok?"ok":"err"); m.textContent=r.message||(r.ok?"Held.":"Could not hold."); }
  catch(e){ m.className="msg err"; m.textContent="Network error — try again."; }
  b.disabled=false; b.textContent="⏸ Hold (block send)";
}
function exportCsv(){ window.open("/api/shortlist.csv","_blank"); }

async function refreshAll(){
  await loadWatchlist();
  await Promise.all([loadStatus(), loadFeed(), loadShortlist(), loadActiveSituations(), loadDisclosed(), loadAccumulating()]);
  renderPitchKit();
  const u=document.getElementById("updated"); if(u) u.textContent="Last updated "+new Date().toLocaleTimeString();
}
refreshAll(); setInterval(refreshAll, 5*60*1000);
/* Deep-link: /?company=CIK opens that company's profile directly. Used by the biweekly report's
   "View full profile" buttons (which target the top window from inside the landing-page iframe). */
try{ const _co=new URLSearchParams(location.search).get("company"); if(_co) openCompany(_co); }catch(e){}
