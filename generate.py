#!/usr/bin/env python3
"""
Zimed Sampling Dashboard generator.
Pulls live submissions from JotForm and writes two self-contained HTML files:
  out/public/index.html   -> aggregates only, NO PII (for GitHub Pages)
  out/private/index.html  -> full detail incl. per-submission table (Tailscale only)

The public page embeds ONLY pre-computed aggregates; the raw records are never
written into it, so physician PII cannot leak into the public file.
"""
import json, os, sys, urllib.request, collections, datetime

def load_key():
    k = os.environ.get("JOTFORM_API_KEY")
    if k:
        return k.strip()
    return open(os.path.expanduser("~/.config/zimed/jotform_key")).read().strip()

KEY = load_key()
FORM = "251544653849063"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

QID = {"doctor":"2","email":"3","phone":"4","address":"5","samples":"6",
       "license":"9","clinic":"14","rep":"23"}

def api(path):
    url = f"https://api.jotform.com/{path}{'&' if '?' in path else '?'}apiKey={KEY}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)

def ans(sub, qid):
    return sub.get("answers", {}).get(qid, {})

def fullname(a):
    v = a.get("answer")
    if isinstance(v, dict):
        return " ".join(x for x in [v.get("first"), v.get("last")] if x).strip()
    return (a.get("prettyFormat") or v or "").strip() if not isinstance(v, dict) else ""

def phone(a):
    v = a.get("answer")
    if isinstance(v, dict):
        return (v.get("full") or " ".join(x for x in [v.get("area"), v.get("phone")] if x)).strip()
    return (v or "").strip()

def address(a):
    v = a.get("answer")
    if isinstance(v, dict):
        parts = [v.get("addr_line1"), v.get("addr_line2"), v.get("city"),
                 v.get("state"), v.get("postal")]
        return ", ".join(p for p in parts if p)
    return (a.get("prettyFormat") or v or "").strip() if not isinstance(v, dict) else ""

def text(a):
    v = a.get("answer")
    return (v if isinstance(v, str) else (a.get("prettyFormat") or "")).strip()

def fetch_records():
    subs, off = [], 0
    while True:
        page = api(f"form/{FORM}/submissions?limit=1000&offset={off}")["content"]
        if not page:
            break
        subs += page
        off += len(page)
        if len(page) < 1000:
            break
    recs = []
    for s in subs:
        d = (s.get("created_at") or "")[:10]
        if not d:
            continue
        try:
            samples = int(re_int(text(ans(s, QID["samples"]))) or 0)
        except ValueError:
            samples = 0
        recs.append({
            "date": d,
            "clinic": text(ans(s, QID["clinic"])),
            "doctor": fullname(ans(s, QID["doctor"])),
            "email": text(ans(s, QID["email"])),
            "phone": phone(ans(s, QID["phone"])),
            "address": address(ans(s, QID["address"])),
            "samples": samples,
            "license": text(ans(s, QID["license"])),
            "rep": text(ans(s, QID["rep"])) or "(blank)",
        })
    recs.sort(key=lambda r: r["date"], reverse=True)
    return recs

def re_int(s):
    return "".join(ch for ch in str(s) if ch.isdigit())

def quarter_of(d):
    m = int(d[5:7]); return (m - 1)//3 + 1

def build_aggregates(records):
    # group by (year, quarter)
    quarters = {}
    for r in records:
        y = r["date"][:4]; q = quarter_of(r["date"])
        quarters.setdefault((y, q), []).append(r)
    qcharts = []
    for (y, q) in sorted(quarters, reverse=True):
        rows = quarters[(y, q)]
        by = collections.Counter(x["rep"] for x in rows)
        qcharts.append({
            "label": f"Q{q} {y}",
            "year": y, "q": q,
            "bars": [{"rep": rep, "count": c} for rep, c in by.most_common()],
            "total": len(rows),
            "samples": sum(x["samples"] for x in rows),
        })
    # current-year monthly trend
    now = datetime.datetime.now()
    yr = str(now.year)
    months = []
    for m in range(1, 13):
        key = f"{yr}-{m:02d}"
        cnt = sum(1 for r in records if r["date"][:7] == key)
        months.append({"label": datetime.date(int(yr), m, 1).strftime("%b"), "key": key, "count": cnt})
    months = months[:now.month]  # only up to current month
    ytd = [r for r in records if r["date"][:4] == yr]
    by_ytd = collections.Counter(r["rep"] for r in ytd)
    top = by_ytd.most_common(1)
    kpis = {
        "year": yr,
        "submissions": len(ytd),
        "samples": sum(r["samples"] for r in ytd),
        "referrers": len(by_ytd),
        "top_rep": (top[0][0] if top else "-"),
        "top_rep_n": (top[0][1] if top else 0),
        "latest": (records[0]["date"] if records else "-"),
        "all_time": len(records),
    }
    return {"quarters": qcharts, "months": months, "kpis": kpis}

# ----- HTML -----
CHART_JS = r"""
const SVGNS="http://www.w3.org/2000/svg";
function el(t,a,x){const e=document.createElementNS(SVGNS,t);for(const k in a)e.setAttribute(k,a[k]);if(x!=null)e.textContent=x;return e;}
function svg(w,h){return el("svg",{viewBox:`0 0 ${w} ${h}`,width:w,height:h});}
const esc=s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const NONREP=new Set(["Unsure","Another doctor","(blank)"]);

function kpiCards(k){
  const cards=[
    [k.submissions, `Submissions (${k.year})`, "year to date"],
    [k.samples.toLocaleString(), "Samples requested", `${k.year} year to date`],
    [k.referrers, "Referrers", "distinct names on forms"],
    [k.top_rep_n, "Top referrer", esc(k.top_rep)],
    [k.latest, "Latest submission", `${k.all_time} all-time`],
  ];
  document.getElementById("kpis").innerHTML=cards.map(c=>
    `<div class="kpi"><div class="n">${esc(c[0])}</div><div class="l">${esc(c[1])}</div><div class="s">${esc(c[2])}</div></div>`).join("");
}

function repBarChart(host, bars){
  const W=860,rowH=32,padL=160,padR=64,padT=8,H=padT+bars.length*rowH+8;
  const max=Math.max(1,...bars.map(b=>b.count));
  const s=svg(W,H),x0=padL,xW=W-padL-padR;
  bars.forEach((b,i)=>{
    const y=padT+i*rowH+5,bw=Math.max(2,(b.count/max)*xW),non=NONREP.has(b.rep);
    s.appendChild(el("text",{x:x0-10,y:y+13,"text-anchor":"end",class:"blab"},b.rep));
    const rc=el("rect",{x:x0,y:y,width:bw,height:rowH-13,rx:4,fill:non?"var(--grey)":"var(--primary)",class:"bar"});
    rc.appendChild(el("title",null,`${b.rep}: ${b.count}`));s.appendChild(rc);
    s.appendChild(el("text",{x:x0+bw+8,y:y+13,class:"vlab"},b.count));
  });
  host.appendChild(s);
}

function monthChart(host, months){
  if(!months.length) return;
  const W=520,H=240,padL=34,padR=16,padT=16,padB=30;
  const vals=months.map(m=>m.count),max=Math.max(5,...vals),plotW=W-padL-padR,plotH=H-padT-padB;
  const s=svg(W,H);
  for(let g=0;g<=4;g++){const yv=Math.round(max*g/4),y=padT+plotH-(yv/max)*plotH;
    s.appendChild(el("line",{x1:padL,y1:y,x2:W-padR,y2:y,class:"gridline"}));
    s.appendChild(el("text",{x:padL-8,y:y+4,"text-anchor":"end",class:"tick"},yv));}
  const st=plotW/Math.max(1,months.length-1);
  const pts=vals.map((v,i)=>[padL+i*st,padT+plotH-(v/max)*plotH]);
  let dp="";pts.forEach((p,i)=>dp+=(i?"L":"M")+p[0]+" "+p[1]+" ");
  if(pts.length>1){s.appendChild(el("path",{d:dp+`L ${pts[pts.length-1][0]} ${padT+plotH} L ${pts[0][0]} ${padT+plotH} Z`,fill:"rgba(14,124,134,.10)"}));
    s.appendChild(el("path",{d:dp,fill:"none",stroke:"var(--primary)","stroke-width":2.5}));}
  pts.forEach((p,i)=>{const c=el("circle",{cx:p[0],cy:p[1],r:4,fill:"#fff",stroke:"var(--primary)","stroke-width":2.5});
    c.appendChild(el("title",null,`${months[i].label}: ${vals[i]}`));s.appendChild(c);
    s.appendChild(el("text",{x:p[0],y:p[1]-9,"text-anchor":"middle",class:"vlab"},vals[i]));
    s.appendChild(el("text",{x:p[0],y:H-10,"text-anchor":"middle",class:"tick"},months[i].label));});
  host.appendChild(s);
}

function renderQuarters(host, quarters){
  quarters.forEach(q=>{
    const card=document.createElement("div");card.className="card";
    card.innerHTML=`<h2>${esc(q.label)} &mdash; sample requests by referrer</h2>
      <p class="note">${q.total} submissions &middot; ${q.samples.toLocaleString()} samples requested. Shaded bars are non-rep answers.</p>
      <div class="qhost"></div>`;
    host.appendChild(card);
    repBarChart(card.querySelector(".qhost"), q.bars);
  });
}

function renderTable(records){
  if(!records) return;
  const wrap=document.getElementById("tablewrap");
  wrap.style.display="block";
  const headers=[["date","Date"],["clinic","Clinic"],["doctor","Doctor"],["email","Email"],
    ["phone","Phone"],["address","Shipping address"],["samples","Samples"],["license","License #"],["rep","Referrer"]];
  document.getElementById("thead").innerHTML=headers.map(h=>`<th data-k="${h[0]}">${esc(h[1])} <span class="arrow"></span></th>`).join("");
  let sk="date",sd=-1,flt="",rep="";
  const sel=document.getElementById("repfilter");
  const reps=[...new Set(records.map(r=>r.rep))].sort();
  sel.innerHTML='<option value="">All referrers</option>'+reps.map(r=>`<option>${esc(r)}</option>`).join("");
  function draw(){
    let rows=records.filter(r=>(!rep||r.rep===rep)&&(!flt||(r.clinic+" "+r.doctor+" "+r.address+" "+r.rep+" "+r.email+" "+r.license).toLowerCase().includes(flt)));
    rows.sort((a,b)=>{let av=a[sk],bv=b[sk];if(sk==="samples")return(av-bv)*sd;return String(av).localeCompare(String(bv))*sd;});
    document.getElementById("tbody").innerHTML=rows.map(r=>{
      const non=NONREP.has(r.rep);
      return `<tr class="${non?'nonrep':''}"><td>${esc(r.date)}</td><td>${esc(r.clinic)}</td><td>${esc(r.doctor)}</td><td>${esc(r.email)}</td><td>${esc(r.phone)}</td><td>${esc(r.address)}</td><td class="num">${esc(r.samples)}</td><td>${esc(r.license)}</td><td><span class="pill ${non?'g':''}">${esc(r.rep)}</span></td></tr>`;
    }).join("");
    document.getElementById("rowcount").textContent=`${rows.length} of ${records.length} submissions`;
    document.querySelectorAll("#thead th").forEach(th=>th.querySelector(".arrow").textContent=th.dataset.k===sk?(sd<0?"▼":"▲"):"");
  }
  document.querySelectorAll("#thead th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(k===sk)sd*=-1;else{sk=k;sd=(k==="samples"||k==="date")?-1:1;}draw();});
  document.getElementById("search").oninput=e=>{flt=e.target.value.toLowerCase().trim();draw();};
  sel.onchange=e=>{rep=e.target.value;draw();};
  draw();
}

kpiCards(AGG.kpis);
renderQuarters(document.getElementById("quarters"), AGG.quarters);
monthChart(document.getElementById("monthChart"), AGG.months);
renderTable(typeof RECORDS!=="undefined"?RECORDS:null);
document.getElementById("stamp").textContent=GENERATED_AT;
"""

CSS = r"""
:root{--bg:#f4f6f8;--card:#fff;--ink:#1f2d3d;--muted:#6b7a8d;--line:#e3e8ee;--primary:#0e7c86;--accent:#f2785c;--grey:#b8c2cc;--grey-soft:#d7dde3;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{background:linear-gradient(120deg,#0e7c86,#0a5f67);color:#fff;padding:24px 32px 20px}
header h1{margin:0;font-size:21px}header .sub{margin-top:5px;font-size:13px;opacity:.9}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 60px}
.banner{border-radius:8px;padding:9px 14px;font-size:12.5px;margin:16px 0 4px}
.banner.pub{background:#eef6f7;border:1px solid #bfe0e3;color:#0a5f67}
.banner.priv{background:#fff4f1;border:1px solid #f7b3a3;color:#9c3b25}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:13px;margin:16px 0 12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px}
.kpi .n{font-size:23px;font-weight:700;line-height:1}.kpi .l{font-size:12px;color:var(--muted);margin-top:6px}.kpi .s{font-size:11px;color:var(--muted);margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px 14px;margin-top:16px}
.card h2{margin:0 0 2px;font-size:15px}.card .note{font-size:11.5px;color:var(--muted);margin:0 0 8px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}.cols .card{margin-top:0}
svg{width:100%;height:auto;display:block;overflow:visible}
.gridline{stroke:#eef2f6}.axis{stroke:var(--line)}.tick{fill:var(--muted);font-size:11px}
.blab{fill:var(--ink);font-size:11px}.vlab{fill:var(--ink);font-size:11px;font-weight:600}.bar:hover{opacity:.82}
.toolbar{display:flex;gap:10px;align-items:center;margin:6px 0 12px;flex-wrap:wrap}
.toolbar input{flex:1;min-width:220px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px}
.toolbar select{padding:8px 11px;border:1px solid var(--line);border-radius:8px;font-size:13px;background:#fff}
.toolbar .count{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:#f0f4f6;cursor:pointer;font-size:11.5px;text-transform:uppercase;letter-spacing:.3px;color:#46586b;white-space:nowrap}
td.num{text-align:right;font-variant-numeric:tabular-nums}.tablewrapin{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}
tr.nonrep td{background:#fbfbf3}.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#dff0f1;color:#0a5f67}.pill.g{background:var(--grey-soft);color:#4b5a68}
.foot{font-size:11.5px;color:var(--muted);margin-top:24px;line-height:1.5}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}
"""

def page(agg, records, is_public, generated_at):
    banner = ('<div class="banner pub">Public view. Aggregate competition standings only, no patient or physician details.</div>'
              if is_public else
              '<div class="banner priv"><strong>Confidential.</strong> Full physician contact detail. Private (Tailscale) only, do not distribute.</div>')
    table_block = "" if is_public else """
    <div class="card" id="tablewrap" style="display:none">
      <h2>All submissions</h2>
      <p class="note">Full detail, one row per submission. Click a header to sort; type to filter.</p>
      <div class="toolbar"><input id="search" placeholder="Search clinic, doctor, address, referrer…"><select id="repfilter"></select><span class="count" id="rowcount"></span></div>
      <div class="tablewrapin"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div>
    </div>"""
    data_js = f"const AGG={dumps(agg)};\n"
    if not is_public:
        data_js += f"const RECORDS={dumps(records)};\n"
    data_js += f"const GENERATED_AT={json.dumps(generated_at)};\n"
    robots = '<meta name="robots" content="noindex">' if is_public else ''
    foot = ("Source: live JotForm “Zimed PF Sample Request Form”. Counts are raw (every submission as entered, including non-rep answers and internal rep-stock orders). "
            "Auto-refreshed from JotForm about every 20 minutes · <span id=\"stamp\"></span>.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{robots}
<title>Zimed Sampling Competition{'' if is_public else ' — Full Detail'}</title>
<style>{CSS}</style></head><body>
<header><h1>Zimed Sampling Competition{'' if is_public else ' — Full Detail'}</h1>
<div class="sub">Live from JotForm · sample requests by referrer, one chart per quarter</div></header>
<div class="wrap">
{banner}
<div class="kpis" id="kpis"></div>
<div class="card"><h2>Submissions by month ({agg['kpis']['year']})</h2><p class="note">Sample request forms submitted each month this year.</p><div id="monthChart"></div></div>
<div id="quarters"></div>
{table_block}
<div class="foot">{foot}</div>
</div>
<script>{data_js}{CHART_JS}</script>
</body></html>"""

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

def pii_guard(public_html, records):
    """Hard fail if any physician email or phone appears in the public file."""
    bad = []
    for r in records:
        for fld in ("email", "phone"):
            v = r.get(fld)
            if v and v in public_html:
                bad.append(v)
    return bad

def main():
    args = set(sys.argv[1:])
    public_only = "--public-only" in args
    out_dir = "."
    if "--out" in sys.argv:
        out_dir = sys.argv[sys.argv.index("--out") + 1]

    records = fetch_records()
    agg = build_aggregates(records)
    # Data-derived stamp (NOT wall clock) so an unchanged dataset produces a
    # byte-identical file and the scheduled Action commits only on real change.
    generated_at = f"data through {agg['kpis']['latest']} ({agg['kpis']['all_time']} submissions all-time)"

    public_html = page(agg, None, True, generated_at)
    leak = pii_guard(public_html, records)
    if leak:
        sys.exit(f"ABORT: {len(leak)} PII values would leak into the public file; refusing to write.")

    if public_only:
        # CI / GitHub Pages: write ONLY the public page to <out>/index.html.
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "index.html"), "w") as f:
            f.write(public_html)
        print(f"records={len(records)} quarters={len(agg['quarters'])} ytd={agg['kpis']['submissions']} PII=OK")
        print("wrote", os.path.join(out_dir, "index.html"), "(public only)")
    else:
        # Local: write both public and private for review / on-demand detail.
        os.makedirs(os.path.join(OUT, "public"), exist_ok=True)
        os.makedirs(os.path.join(OUT, "private"), exist_ok=True)
        with open(os.path.join(OUT, "public", "index.html"), "w") as f:
            f.write(public_html)
        with open(os.path.join(OUT, "private", "index.html"), "w") as f:
            f.write(page(agg, records, False, generated_at))
        print(f"records={len(records)} quarters={len(agg['quarters'])} ytd={agg['kpis']['submissions']} PII=OK")
        print("wrote", os.path.join(OUT, "public/index.html"), "and private/index.html")

if __name__ == "__main__":
    main()
