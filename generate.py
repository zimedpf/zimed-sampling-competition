#!/usr/bin/env python3
"""
Zimed Sampling Competition dashboard generator (Zimed / LUVO branded).

Writes two self-contained HTML files:
  out/public/index.html   -> competition leaderboard only, NO PII (GitHub Pages)
  out/private/index.html  -> leaderboard + high-level sample-consumption analytics
                             + full submission table (private / on-demand)

Competition scoring:
  * 1 point per UNIQUE doctor who signs and names a rep, per quarter. A doctor
    signing again for the same rep in the same quarter does NOT add a point.
  * Krish and Aymeric (program runners) and non-rep answers are EXCLUDED from
    standings (still counted in program/consumption totals).

High-level analytics are intentionally aggregate (no per-account drill-down),
mirroring Krish's July 21 2025 spec to OpenFlow: volume, frequency, cadence.
"""
import json, os, sys, re, base64, urllib.request, collections, datetime

def load_key():
    k = os.environ.get("JOTFORM_API_KEY")
    return (k or open(os.path.expanduser("~/.config/zimed/jotform_key")).read()).strip()

KEY = load_key()
FORM = "251544653849063"
SELF = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SELF, "out")
QID = {"doctor":"2","email":"3","phone":"4","address":"5","samples":"6",
       "license":"9","clinic":"14","rep":"23"}

# --- Competition config (edit here) ---------------------------------------
EXCLUDE = {"Krish Khurana", "Aymeric Paillet", "(blank)", ""}
# Shown in the standings but NOT eligible for medals/prizes/leader (catch-all answers).
NONREP_NAMES = ["Unsure", "Another doctor"]
# GOTCHA / referrer attribution: the form's referrer field ("From whom...") only began
# capturing answers on 2025-07-21. The 28 forms before that are blank. Per Krish, blank
# referrers are attributed to him for the dashboard (he's a program runner, excluded from
# standings, so this does not affect the competition). We KEEP a `rep_blank` flag on each
# record so the data-quality panel still surfaces blanks honestly — if a NEW blank ever
# appears post-launch it will show up there even though it's attributed to Krish here.
REP_REMAP = {"(blank)": "Krish Khurana", "": "Krish Khurana"}
PRIZES = [1000, 500, 250]
DROPS_PER_BOTTLE = 100
PROV = {"british columbia":"BC","bc":"BC","alberta":"AB","ab":"AB","saskatchewan":"SK",
        "sk":"SK","manitoba":"MB","mb":"MB","ontario":"ON","on":"ON","quebec":"QC",
        "québec":"QC","qc":"QC","new brunswick":"NB","nb":"NB","nova scotia":"NS","ns":"NS",
        "newfoundland":"NL","newfoundland and labrador":"NL","nl":"NL",
        "prince edward island":"PE","pe":"PE","pei":"PE","yukon":"YT","yt":"YT",
        "northwest territories":"NT","nt":"NT","nunavut":"NU","nu":"NU",
        # common variants seen in the live data (Québec "PQ", "Ont", one-word "Novascotia")
        "pq":"QC","p.q.":"QC","que":"QC","ont":"ON","ontario.":"ON","novascotia":"NS"}
# Canadian postal-code first letter -> province (most reliable fallback; an address can
# misspell the province but the postal code still pins it). X is NT/NU (default NT, rare).
POSTAL_PROV = {"A":"NL","B":"NS","C":"PE","E":"NB","G":"QC","H":"QC","J":"QC","K":"ON",
               "L":"ON","M":"ON","N":"ON","P":"ON","R":"MB","S":"SK","T":"AB","V":"BC",
               "X":"NT","Y":"YT"}
# Last-resort city fallback for addresses with no parseable state token AND no postal code.
CITY_PROV = {"montreal":"QC","montréal":"QC","laval":"QC","gatineau":"QC","longueuil":"QC",
             "sherbrooke":"QC","trois-rivières":"QC","lévis":"QC",
             "toronto":"ON","ottawa":"ON","mississauga":"ON","hamilton":"ON",
             "calgary":"AB","edmonton":"AB","winnipeg":"MB","saskatoon":"SK","regina":"SK"}

# Province -> region rollup (geographic L-to-R: West, Central, Atlantic). Anything
# unmapped (incl. blank/"—") falls into "Unknown" and is only shown if non-empty.
REGION = {"BC":"West","AB":"West","SK":"West","MB":"West","YT":"West","NT":"West","NU":"West",
          "ON":"Central","QC":"Central",
          "NB":"Atlantic","NS":"Atlantic","PE":"Atlantic","NL":"Atlantic"}
REGION_ORDER = ["West","Central","Atlantic","Unknown"]
LAPSED_DAYS = 90  # a doctor with no request in this many days counts as "lapsed"

def eligible_reps(current_reps):
    """Competing reps = current dropdown options minus program-runners and catch-alls."""
    return [r for r in current_reps if r not in EXCLUDE and r not in NONREP_NAMES]

# ---- JotForm fetch ----
def api(path):
    url = f"https://api.jotform.com/{path}{'&' if '?' in path else '?'}apiKey={KEY}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)

def a(s, q): return s.get("answers", {}).get(q, {})

def fullname(x):
    v = x.get("answer")
    if isinstance(v, dict):
        return " ".join(p for p in [v.get("first"), v.get("last")] if p).strip()
    return (x.get("prettyFormat") or v or "").strip() if not isinstance(v, dict) else ""

def phone(x):
    v = x.get("answer")
    if isinstance(v, dict):
        return (v.get("full") or " ".join(p for p in [v.get("area"), v.get("phone")] if p)).strip()
    return (v or "").strip()

def addr_parts(x):
    v = x.get("answer")
    if isinstance(v, dict):
        line = ", ".join(p for p in [v.get("addr_line1"), v.get("addr_line2"),
                v.get("city"), v.get("state"), v.get("postal")] if p)
        return line, (v.get("state") or "")
    return (x.get("prettyFormat") or (v if isinstance(v, str) else "") or ""), ""

def text(x):
    v = x.get("answer")
    return (v if isinstance(v, str) else (x.get("prettyFormat") or "")).strip()

def to_int(s):
    d = "".join(c for c in str(s) if c.isdigit()); return int(d) if d else 0

def province(state, address):
    s = (state or "").strip().lower().rstrip(".")
    if s in PROV: return PROV[s]
    blob = (address or "").lower()
    for name, code in PROV.items():
        if len(name) > 3 and name in blob: return code
    # postal-code prefix (reliable even when the province is misspelled / "Select..." / blank)
    m = re.search(r"\b([ABCEGHJKLMNPRSTVXY])\d[A-Z]\s*\d[A-Z]\d\b", (address or "").upper())
    if m: return POSTAL_PROV[m.group(1)]
    for tok in re.findall(r"\b([a-z]{2})\b", blob):
        if tok.upper() in {"BC","AB","SK","MB","ON","QC","NB","NS","NL","PE","YT","NT","NU"}:
            return tok.upper()
    for city, code in CITY_PROV.items():
        if city in blob: return code
    return "—"

def doctor_key(doctor, license):
    lic = re.sub(r"[^a-z0-9]", "", (license or "").lower())
    digits = re.sub(r"\D", "", lic)
    if len(digits) >= 3 and not any(w in lic for w in ("rep","stock","order","carstock","sample")):
        return "L:" + digits
    return "N:" + re.sub(r"\s+", " ", (doctor or "").strip().lower())

def fetch_records():
    subs, off = [], 0
    while True:
        page = api(f"form/{FORM}/submissions?limit=1000&offset={off}")["content"]
        if not page: break
        subs += page; off += len(page)
        if len(page) < 1000: break
    recs = []
    for s in subs:
        d = (s.get("created_at") or "")[:10]
        if not d: continue
        addr, state = addr_parts(a(s, QID["address"]))
        doctor = fullname(a(s, QID["doctor"])); lic = text(a(s, QID["license"]))
        m = int(d[5:7])
        recs.append({"date": d, "clinic": text(a(s, QID["clinic"])), "doctor": doctor,
            "email": text(a(s, QID["email"])), "phone": phone(a(s, QID["phone"])),
            "address": addr, "province": province(state, addr),
            "samples": to_int(text(a(s, QID["samples"]))), "license": lic,
            "rep": (lambda rp: REP_REMAP.get(rp, rp))(text(a(s, QID["rep"])) or "(blank)"),
            "rep_blank": text(a(s, QID["rep"])).strip() == "",
            "year": d[:4], "q": (m-1)//3+1,
            "month": d[:7], "dkey": doctor_key(doctor, lic)})
    recs.sort(key=lambda r: r["date"], reverse=True)
    return recs

def fetch_rep_options():
    """Current valid selections in the 'From whom...' dropdown. Reps no longer in this
    list (e.g. departed reps) are dropped from the competition board entirely."""
    q = api(f"form/{FORM}/question/{QID['rep']}")["content"]
    return set(o.strip() for o in (q.get("options") or "").split("|") if o.strip())

# ---- aggregation helpers ----
def qkey(r): return f"{r['year']}-Q{r['q']}"
def qlabel(y, q): return f"Q{q} {y}"
def quarter_end(y, q): return datetime.date(y, *{1:(3,31),2:(6,30),3:(9,30),4:(12,31)}[q])

def build_competition(records, today, current_reps):
    buckets = {}
    for r in records:
        # Only count reps who are excluded-free AND still a valid current dropdown option.
        if r["rep"] in EXCLUDE or r["rep"] not in current_reps: continue
        buckets.setdefault((r["year"], r["q"]), {}).setdefault(r["rep"], set()).add(r["dkey"])
    def rows(key):
        reps = buckets.get(key, {})
        return [{"rep": rep, "points": n} for rep, n in
                sorted(((rep, len(ds)) for rep, ds in reps.items()), key=lambda x: -x[1])]
    cy, cq = today.year, (today.month-1)//3+1
    days_left = (quarter_end(cy, cq) - today).days
    current = {"label": qlabel(cy, cq), "days_left": max(0, days_left),
               "prizes": PRIZES, "rows": rows((str(cy), cq))}
    past = []
    for (y, q) in sorted(buckets, reverse=True):
        if (int(y), q) == (cy, cq): continue
        rr = rows((y, q))
        past.append({"label": qlabel(y, q), "rows": rr, "winner": rr[0]["rep"] if rr else None})
    return current, past

def build_consumption(records, today, current_reps):
    yr = str(today.year)
    bottles = sum(r["samples"] for r in records)
    dcount = collections.Counter(r["dkey"] for r in records)
    uniq = len(dcount); reorder = sum(1 for v in dcount.values() if v > 1)

    # monthly series (all months present, chronological)
    months_present = sorted(set(r["month"] for r in records))
    def mlabel(m): return datetime.datetime.strptime(m, "%Y-%m").strftime("%b %y")
    by_month, cum, run = [], [], 0
    for m in months_present:
        rs = [r for r in records if r["month"] == m]
        b = sum(r["samples"] for r in rs); run += b
        by_month.append({"label": mlabel(m), "bottles": b, "requests": len(rs),
                         "avg": round(b/len(rs), 1) if rs else 0})
        cum.append({"label": mlabel(m), "total": run})

    # province
    pc = collections.Counter()
    for r in records: pc[r["province"]] += r["samples"]
    by_province = [{"k": k, "bottles": v} for k, v in pc.most_common() if k]

    # by rep (all, incl excluded) -> bottles
    rc = collections.Counter()
    for r in records: rc[r["rep"]] += r["samples"]
    by_rep = [{"k": k, "bottles": v} for k, v in rc.most_common()]

    # quarters chronological with growth
    qs = sorted(set((r["year"], r["q"]) for r in records))
    by_quarter, prev = [], None
    for (y, q) in qs:
        rs = [r for r in records if r["year"] == y and r["q"] == q]
        b = sum(r["samples"] for r in rs)
        growth = None if prev in (None, 0) else round((b-prev)/prev*100)
        by_quarter.append({"label": qlabel(y, q), "bottles": b, "requests": len(rs), "growth": growth})
        prev = b

    # adoption buckets (per doctor # of requests)
    buckets = {"1×":0,"2×":0,"3×":0,"4×":0,">4×":0}
    for v in dcount.values():
        buckets[{1:"1×",2:"2×",3:"3×",4:"4×"}.get(v, ">4×")] += 1
    adoption = [{"k": k, "n": buckets[k]} for k in ["1×","2×","3×","4×",">4×"]]

    # order-size mix
    om = collections.Counter(r["samples"] for r in records)
    order_mix = [{"k": (str(k) if k else "other"), "n": om[k]} for k in sorted(om)]

    # first-seen quarter per doctor (reach + new vs repeat)
    asc = sorted(records, key=lambda r: r["date"])
    first_q = {}
    for r in asc:
        first_q.setdefault(r["dkey"], (r["year"], r["q"]))
    reach, newrep = [], []
    for (y, q) in qs:
        new_docs = sum(1 for k, fq in first_q.items() if fq == (y, q))
        nb = sum(r["samples"] for r in records if r["year"]==y and r["q"]==q and first_q[r["dkey"]]==(y,q))
        rb = sum(r["samples"] for r in records if r["year"]==y and r["q"]==q and first_q[r["dkey"]]!=(y,q))
        reach.append({"label": qlabel(y, q), "new": new_docs})
        newrep.append({"label": qlabel(y, q), "new": nb, "repeat": rb})

    # cadence: median days between repeat requests (per doctor)
    gaps = []
    bydoc = {}
    for r in asc: bydoc.setdefault(r["dkey"], []).append(r["date"])
    for k, ds in bydoc.items():
        for i in range(1, len(ds)):
            d0 = datetime.date.fromisoformat(ds[i-1]); d1 = datetime.date.fromisoformat(ds[i])
            gaps.append((d1-d0).days)
    gaps.sort()
    median_gap = gaps[len(gaps)//2] if gaps else None

    nq = len(qs) or 1
    # run-rate for current quarter
    cy, cq = today.year, (today.month-1)//3+1
    qstart = datetime.date(cy, {1:1,2:4,3:7,4:10}[cq], 1)
    qend = quarter_end(cy, cq)
    cur_b = sum(r["samples"] for r in records if r["year"]==str(cy) and r["q"]==cq)
    elapsed = (today - qstart).days + 1; total_days = (qend - qstart).days + 1
    frac = elapsed / total_days if total_days else 1
    projected = round(cur_b / elapsed * total_days) if elapsed > 0 else cur_b

    # ---- management-only / competition analytics ----
    eligible = eligible_reps(current_reps)

    # territory rollup (West / Central / Atlantic / Unknown)
    reg = collections.Counter()
    for r in records: reg[REGION.get(r["province"], "Unknown")] += r["samples"]
    by_region = [{"k": k, "bottles": reg[k]} for k in REGION_ORDER if reg[k]]

    # lapsed reach: days since each doctor's most recent request (asc => last wins)
    last_seen = {}
    for r in asc: last_seen[r["dkey"]] = r["date"]
    lap = {"active": 0, "mid": 0, "deep": 0}
    for d in last_seen.values():
        days = (today - datetime.date.fromisoformat(d)).days
        if days <= LAPSED_DAYS: lap["active"] += 1
        elif days <= 2*LAPSED_DAYS: lap["mid"] += 1
        else: lap["deep"] += 1
    lapsed = {"days": LAPSED_DAYS, "total": uniq, "lapsed": lap["mid"]+lap["deep"],
              "active": lap["active"], "mid": lap["mid"], "deep": lap["deep"]}

    # rep efficiency: unique doctors reached, bottles, bottles/doctor (eligible reps only)
    efficiency = []
    for rep in eligible:
        rrs = [r for r in records if r["rep"] == rep]
        dset = set(r["dkey"] for r in rrs); b = sum(r["samples"] for r in rrs)
        if not dset: continue
        efficiency.append({"k": rep, "docs": len(dset), "bottles": b,
                           "per_doc": round(b/len(dset), 1)})
    efficiency.sort(key=lambda x: -x["docs"])

    # data-quality flags (aggregate counts only). no_rep uses the raw rep_blank flag so the
    # panel stays honest even though blanks are attributed to Krish in `rep` (see REP_REMAP).
    dq = {"no_rep": sum(1 for r in records if r["rep_blank"]),
          "no_rep_bottles": sum(r["samples"] for r in records if r["rep_blank"]),
          "no_province": sum(1 for r in records if not r["province"] or r["province"] == "—"),
          "no_license": sum(1 for r in records if not r["license"]),
          "total": len(records)}

    # rep momentum: unique doctors per quarter, per eligible rep (small-multiples)
    momentum = []
    for rep in eligible:
        series = []
        for (y, q) in qs:
            docs = len(set(r["dkey"] for r in records
                           if r["rep"] == rep and r["year"] == y and r["q"] == q))
            series.append({"label": qlabel(y, q), "n": docs})
        if any(s["n"] for s in series):
            momentum.append({"k": rep, "series": series})
    momentum.sort(key=lambda m: -sum(s["n"] for s in m["series"]))

    # competition projection: current-quarter unique doctors per rep, projected at pace
    proj = []
    for rep in eligible:
        now = len(set(r["dkey"] for r in records
                      if r["rep"] == rep and r["year"] == str(cy) and r["q"] == cq))
        projected_docs = round(now/frac) if frac > 0 else now
        if now or projected_docs:
            proj.append({"k": rep, "now": now, "proj": max(projected_docs, now)})
    proj.sort(key=lambda x: (-x["proj"], -x["now"]))
    projection = {"label": qlabel(cy, cq), "pct_elapsed": round(frac*100),
                  "days_left": max(0, (qend - today).days), "rows": proj}

    return {
        "kpis": {"bottles": bottles, "drops": bottles*DROPS_PER_BOTTLE, "requests": len(records),
                 "unique_docs": uniq, "avg_per_req": round(bottles/len(records),1) if records else 0,
                 "reorder_pct": round(100*reorder/uniq) if uniq else 0,
                 "avg_req_per_q": round(len(records)/nq,1), "avg_bottles_per_q": round(bottles/nq),
                 "median_gap": median_gap},
        "by_month": by_month, "cumulative": cum, "by_province": by_province, "by_rep": by_rep,
        "by_quarter": by_quarter, "adoption": adoption, "order_mix": order_mix,
        "reach": reach, "newrep": newrep,
        "run": {"label": qlabel(cy, cq), "so_far": cur_b, "projected": projected,
                "elapsed": elapsed, "total": total_days},
        "by_region": by_region, "lapsed": lapsed, "efficiency": efficiency,
        "dq": dq, "momentum": momentum, "projection": projection,
    }

def build(records):
    today = datetime.datetime.now().date()
    current_reps = fetch_rep_options()
    current, past = build_competition(records, today, current_reps)
    team = {"clinics": len(set(r["dkey"] for r in records)),
            "bottles": sum(r["samples"] for r in records)}
    latest = records[0]["date"] if records else "-"
    return {"current": current, "past": past, "team": team,
            "consumption": build_consumption(records, today, current_reps),
            "stamp": f"data through {latest} ({len(records)} submissions all-time)"}

def brand_img(is_public):
    if is_public: return "zimed-box.png"
    for p in (os.path.join(SELF, "zimed-box.png"), os.path.join(os.getcwd(), "zimed-box.png")):
        if os.path.exists(p):
            return "data:image/png;base64," + base64.b64encode(open(p,"rb").read()).decode()
    return ""

def dumps(o): return json.dumps(o, ensure_ascii=False).replace("</", "<\\/")

CSS = r"""
:root{--teal:#03BAB3;--teal-d:#06827b;--gold:#FAB718;--ink:#233534;--muted:#6a807e;
--bg:#eef5f4;--card:#fff;--line:#dfeae8;--grey:#b8c8c6;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{background:linear-gradient(120deg,var(--teal-d),var(--teal));color:#fff;padding:22px 30px;
display:flex;align-items:center;gap:20px;border-bottom:4px solid var(--gold)}
header h1{margin:0;font-size:22px;font-weight:800}header .sub{margin-top:4px;font-size:13px;opacity:.95}
header img.prod{height:96px;margin-left:auto;background:#fff;padding:4px;border-radius:6px;filter:drop-shadow(0 6px 14px rgba(0,0,0,.25))}
header svg{width:34px;height:34px;flex:none}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 60px}
.sec{margin:26px 0 6px;font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.6px;color:var(--teal-d);border-bottom:2px solid var(--line);padding-bottom:6px}
.banner{border-radius:9px;padding:10px 14px;font-size:12.5px;margin:16px 0 6px}
.banner.pub{background:#e2f5f3;border:1px solid #b6e6e1;color:#0a6b65}
.banner.priv{background:#fff4f1;border:1px solid #f7b3a3;color:#9c3b25}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:14px 15px;position:relative;overflow:hidden}
.kpi:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--teal)}
.kpi .n{font-size:22px;font-weight:800;line-height:1.05}.kpi .l{font-size:12px;color:var(--muted);margin-top:6px}.kpi .s{font-size:11px;color:var(--muted);margin-top:3px}
.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:16px 18px 14px;margin-top:14px}
.card h2{margin:0 0 3px;font-size:15px}.card .note{font-size:11.5px;color:var(--muted);margin:0 0 12px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}.cols .card{margin-top:0}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;background:#e2f5f3;color:#0a6b65}
.countdown{font-size:12px;font-weight:700;color:var(--teal-d);background:#e2f5f3;padding:4px 11px;border-radius:20px;margin-left:8px}
.lb{display:flex;flex-direction:column;gap:7px}
.lrow{display:grid;grid-template-columns:40px 1fr 150px 70px;align-items:center;gap:12px;padding:7px 8px;border-radius:9px}
.lrow.top{background:#f3fbfa}.lrow .rank{font-weight:800;text-align:center}.lrow .who{font-weight:600}
.lrow .barwrap{background:#eef3f2;border-radius:6px;height:18px;overflow:hidden}.lrow .bar{height:100%;background:var(--teal)}
.lrow .pts{text-align:right;font-weight:800;font-variant-numeric:tabular-nums}.lrow .prize{font-size:11px;color:#9a6b00;font-weight:700}
.lrow .prize.tie{color:#c0392b;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.3px}
.lrow .rank .rbadge{display:inline-block;min-width:26px;text-align:center;font-weight:800;font-size:12.5px;color:#5a716e;background:#eef3f2;border-radius:7px;padding:3px 0}
.lrow .rank .rbadge.t{color:#b9531f;background:#fdeee2}
.lrow .stake{font-size:9.5px;color:#9a6b00;font-weight:600;margin-top:1px}
.lrow.non{opacity:.62}.lrow.non .who{font-style:italic;font-weight:500}
.lrow .rank .rbadge.dash{background:none;color:#9fb0ae}
.lrow .gap{font-size:10.5px;color:var(--muted)}.medal{font-size:16px}
.qpast{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.qbox{border:1px solid var(--line);border-radius:11px;padding:12px 14px}.qbox h3{margin:0 0 8px;font-size:13px}
.qbox .r{display:flex;justify-content:space-between;font-size:12.5px;padding:2px 0}.qbox .r.win{font-weight:800;color:var(--teal-d)}.qbox .r.non{opacity:.55;font-style:italic}
.card svg{width:100%;height:auto;display:block;overflow:visible}
.gridline{stroke:#eef3f2}.tick{fill:var(--muted);font-size:10.5px}.vlab{fill:var(--ink);font-size:10.5px;font-weight:700}
.hbars{display:flex;flex-direction:column;gap:6px}
.hbar{display:grid;grid-template-columns:120px 1fr 64px;align-items:center;gap:10px;font-size:12px}
.hbar .t{background:#eef3f2;border-radius:5px;height:16px;overflow:hidden}.hbar .f{height:100%;background:var(--teal)}
.hbar .v{text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12.5px}th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#eef5f4;font-size:11px;text-transform:uppercase;letter-spacing:.3px;color:#46625f;white-space:nowrap;cursor:pointer}
td.num{text-align:right;font-variant-numeric:tabular-nums}.tin{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.toolbar{display:flex;gap:10px;align-items:center;margin:4px 0 12px;flex-wrap:wrap}
.toolbar input{flex:1;min-width:200px;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px}
.toolbar select{padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:#fff}
.foot{font-size:11.5px;color:var(--muted);margin-top:24px;line-height:1.55}.luvo{font-weight:800;letter-spacing:1px;color:var(--teal-d)}
.empty{fill:var(--grey);font-size:13px;font-weight:600}
.pmut{color:var(--muted);font-weight:600}
.mgmttag{font-size:9.5px;font-weight:800;background:#fff4f1;color:#9c3b25;border:1px solid #f7b3a3;border-radius:20px;padding:1px 8px;vertical-align:middle;margin-left:6px}
/* windowing toggle */
.winctl{display:flex;justify-content:flex-end;align-items:center;gap:8px;font-size:11px;color:var(--muted);margin:-2px 0 2px}
.winctl button{font:inherit;font-size:11px;font-weight:700;color:var(--teal-d);background:#e2f5f3;border:1px solid #b6e6e1;border-radius:20px;padding:2px 10px;cursor:pointer}
.winctl button:hover{background:#d2efec}
/* competition projection */
.projbars{display:flex;flex-direction:column;gap:7px;margin-top:2px}
.projrow{display:grid;grid-template-columns:96px 1fr 70px;align-items:center;gap:10px;font-size:12px}
.projrow .pname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ptrack{position:relative;background:#eef3f2;border-radius:5px;height:16px;overflow:hidden}
.ptrack .ppace{position:absolute;inset:0 auto 0 0;height:100%;background:repeating-linear-gradient(135deg,#cdeeeb,#cdeeeb 5px,#dbf2f0 5px,#dbf2f0 10px)}
.ptrack .pnow{position:absolute;inset:0 auto 0 0;height:100%;background:var(--teal);border-radius:5px}
.projrow .pval{text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
/* rep momentum small-multiples */
.sparkgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:4px}
.sparkcell{border:1px solid var(--line);border-radius:10px;padding:9px 11px 8px;background:#fbfdfd}
.sparkcell .sname{font-size:12px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sparkcell .snums{display:flex;align-items:baseline;gap:7px;margin:1px 0 2px}
.sparkcell .slast{font-size:20px;font-weight:800;line-height:1}
.sparkcell .sdelta{font-size:11px;font-weight:700}
.sdelta.up{color:#06827b}.sdelta.down{color:#c0392b}.sdelta.flat{color:var(--muted)}
.spark{width:100%;height:44px;display:block}
.sparkcell .sfoot{font-size:9.5px;color:var(--muted);margin-top:3px}
/* mini efficiency table */
table.mini{font-size:12.5px}table.mini th{cursor:default}table.mini td{border-bottom:1px solid var(--line)}
/* lapsed reach */
.bigstat .n{font-size:30px;font-weight:800;line-height:1}.bigstat .l{font-size:12.5px;margin-top:4px}.bigstat .s{font-size:11.5px;color:var(--muted);margin-top:3px}
.segbar{display:flex;height:16px;border-radius:6px;overflow:hidden;margin:12px 0 7px;background:#eef3f2}
.seg{min-width:2px}.seg.act{background:#9fd9d4}.seg.mid{background:#FAB718}.seg.deep{background:#e07a52}
.seglegend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted)}
.seglegend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
.seglegend i.act{background:#9fd9d4}.seglegend i.mid{background:#FAB718}.seglegend i.deep{background:#e07a52}
/* data-quality */
.dqgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.dqcard{border:1px solid var(--line);border-radius:10px;padding:11px 12px;background:#fbfdfd}
.dqcard.warn{border-color:#f3cbbe;background:#fff8f4}
.dqcard .n{font-size:21px;font-weight:800;line-height:1}.dqcard.warn .n{color:#c0562f}.dqcard.ok .n{color:#06827b}
.dqcard .l{font-size:11.5px;font-weight:600;margin-top:4px}.dqcard .s{font-size:10.5px;color:var(--muted);margin-top:2px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}header img.prod{display:none}.dqgrid{grid-template-columns:1fr}.projrow{grid-template-columns:78px 1fr 64px}}
"""

DROP_SVG = ('<svg width="34" height="34" viewBox="0 0 24 24">'
            '<path d="M12 2C12 2 4 11 4 16a8 8 0 0016 0c0-5-8-14-8-14z" fill="#fff"/>'
            '<circle cx="12" cy="15.5" r="3.4" fill="#03BAB3"/></svg>')

JS = r"""
const esc=s=>String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
const NS="http://www.w3.org/2000/svg";
function el(t,a,x){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(x!=null)e.textContent=x;return e;}
const MEDAL=["🥇","🥈","🥉"];
const fmt=n=>(+n).toLocaleString();
// abbreviate large axis/value numbers: 11090 -> 11.1k, 4000 -> 4k
const kfmt=n=>{n=+n;const a=Math.abs(n);if(a>=1e6)return(n/1e6).toFixed(a%1e6?1:0).replace(/\.0$/,"")+"M";if(a>=1000)return(n/1000).toFixed(a%1000?1:0).replace(/\.0$/,"")+"k";return""+n;};
// Window long time-series so labels never collide: show the most recent N by
// default with a "Show all" toggle that re-renders the full history in place.
function windowed(host,items,draw){
  const WIN=12;host.innerHTML="";
  if(items.length<=WIN){draw(host,items);return;}
  const all=host.dataset.all==="1",shown=all?items:items.slice(-WIN);
  const ctl=document.createElement("div");ctl.className="winctl";
  ctl.innerHTML=`<span>${all?`All ${items.length} points`:`Last ${WIN} of ${items.length}`}</span>`;
  const btn=document.createElement("button");btn.textContent=all?`Recent ${WIN}`:"Show all";
  btn.onclick=()=>{host.dataset.all=all?"":"1";windowed(host,items,draw);};
  ctl.appendChild(btn);host.appendChild(ctl);
  const box=document.createElement("div");host.appendChild(box);draw(box,shown);}

function ordinal(n){const v=n%100,s=["th","st","nd","rd"];return n+(s[(v-20)%10]||s[v]||s[0]);}
function leaderboard(host,q){
  const rows=q.rows||[];
  if(!rows.length){host.innerHTML='<p class="note">No qualifying signatures yet this quarter — wide open.</p>';return;}
  const prizes=q.prizes||[], maxPts=rows[0].points, leaderPts=rows[0].points;
  // rank with ties: everyone sharing a point value shares a rank
  const cnt={};rows.forEach(r=>cnt[r.points]=(cnt[r.points]||0)+1);
  host.innerHTML="";const lb=document.createElement("div");lb.className="lb";
  rows.forEach(r=>{
    const rank=1+rows.filter(x=>x.points>r.points).length;
    const size=cnt[r.points], tied=size>1, isRep=!NONREP.has(r.rep);
    let rankCell, sub, prizeHtml="";
    if(!isRep){
      // catch-all answer: visible in standings, but outside the ranking (no rank/medal/prize)
      rankCell='<span class="rbadge dash">–</span>';
      sub='form answer · not a competing rep';
    }else{
      // medal ONLY for a clean (uncontested) podium place; tied places get a "T#" badge
      if(!tied && rank<=3) rankCell='<span class="medal">'+MEDAL[rank-1]+'</span>';
      else rankCell='<span class="rbadge'+(tied?' t':'')+'">'+(tied?'T'+rank:rank)+'</span>';
      if(rank===1 && !tied) sub='leader';
      else if(tied) sub='tied for '+ordinal(rank);
      else sub=(leaderPts-r.points)+' behind the lead';
      if(!tied){
        if(rank<=prizes.length) prizeHtml='<div class="prize">$'+fmt(prizes[rank-1])+'</div>';
      }else{
        // tiebreaker shown identically for every member of the tied group
        const stake=[];for(let p=rank;p<=rank+size-1;p++) if(p<=prizes.length) stake.push(prizes[p-1]);
        if(stake.length) prizeHtml='<div class="prize tie">tiebreaker needed!</div><div class="stake">for '+stake.map(p=>'$'+fmt(p)).join(' / ')+'</div>';
      }
    }
    const d=document.createElement("div");d.className="lrow"+(isRep&&!tied&&rank<=3?" top":"")+(isRep?"":" non");
    d.innerHTML=`<div class="rank">${rankCell}</div>
      <div><div class="who">${esc(r.rep)}</div><div class="gap">${sub}</div></div>
      <div class="barwrap"><div class="bar" style="width:${Math.max(4,r.points/Math.max(1,maxPts)*100)}%"></div></div>
      <div class="pts">${r.points}${prizeHtml}</div>`;
    lb.appendChild(d);
  });
  host.appendChild(lb);
}
function pastQ(host,past){
  host.innerHTML="";if(!past.length)return;const g=document.createElement("div");g.className="qpast";
  past.forEach(q=>{const b=document.createElement("div");b.className="qbox";let h=`<h3>${esc(q.label)}</h3>`;
    const rows=q.rows||[];
    const repPts=rows.filter(r=>!NONREP.has(r.rep)).map(r=>r.points);
    const top=repPts.length?Math.max(...repPts):null;   // winner = top real rep(s)
    rows.slice(0,5).forEach(r=>{const non=NONREP.has(r.rep),win=!non&&r.points===top;
      h+=`<div class="r${win?' win':''}${non?' non':''}"><span>${win?'🏆 ':''}${esc(r.rep)}</span><span>${r.points}</span></div>`;});
    b.innerHTML=h;g.appendChild(b);});host.appendChild(g);
}
function hbars(id,items,key){key=key||"bottles";const host=document.getElementById(id);if(!host)return;
  const max=Math.max(1,...items.map(i=>i[key]));host.innerHTML="";const w=document.createElement("div");w.className="hbars";
  items.forEach(it=>{const row=document.createElement("div");row.className="hbar";
    row.innerHTML=`<div title="${esc(it.k)}">${esc(it.k)}</div><div class="t"><div class="f" style="width:${Math.max(3,it[key]/max*100)}%"></div></div><div class="v">${fmt(it[key])}</div>`;
    w.appendChild(row);});host.appendChild(w);}
const EMPTY=(s,W,H)=>{s.appendChild(el("text",{x:W/2,y:H/2,"text-anchor":"middle",class:"empty"},"No data yet"));return s;};
function gridY(s,max,pL,pR,pT,ph,W){for(let g=0;g<=4;g++){const yv=Math.round(max*g/4),y=pT+ph-(yv/max)*ph;
  s.appendChild(el("line",{x1:pL,y1:y,x2:W-pR,y2:y,class:"gridline"}));
  s.appendChild(el("text",{x:pL-7,y:y+4,"text-anchor":"end",class:"tick"},kfmt(yv)));}}
function buildLine(items,key,color){
  const W=520,H=210,pL=44,pR=16,pT=16,pB=28,vals=items.map(m=>m[key]),max=Math.max(1,...vals),pw=W-pL-pR,ph=H-pT-pB;
  const s=el("svg",{viewBox:`0 0 ${W} ${H}`});if(!items.length)return EMPTY(s,W,H);
  gridY(s,max,pL,pR,pT,ph,W);
  const single=items.length===1,st=single?0:pw/(items.length-1);
  const xp=i=>single?pL+pw/2:pL+i*st;
  const pts=vals.map((v,i)=>[xp(i),pT+ph-(v/max)*ph]);
  if(pts.length>1){let dp="";pts.forEach((p,i)=>dp+=(i?"L":"M")+p[0]+" "+p[1]+" ");
    s.appendChild(el("path",{d:dp+`L ${pts.at(-1)[0]} ${pT+ph} L ${pts[0][0]} ${pT+ph} Z`,fill:color+"22"}));
    s.appendChild(el("path",{d:dp,fill:"none",stroke:color,"stroke-width":2.5}));}
  const lstep=Math.ceil(items.length/12);  // thin x-labels so they never collide
  pts.forEach((p,i)=>{const c=el("circle",{cx:p[0],cy:p[1],r:3.5,fill:"#fff",stroke:color,"stroke-width":2.5});
    c.appendChild(el("title",null,`${items[i].label}: ${fmt(vals[i])}`));s.appendChild(c);
    if(items.length<=8)s.appendChild(el("text",{x:p[0],y:p[1]-8,"text-anchor":"middle",class:"vlab"},kfmt(vals[i])));
    if(i%lstep===0||i===items.length-1)s.appendChild(el("text",{x:p[0],y:H-9,"text-anchor":"middle",class:"tick"},items[i].label));});
  return s;}
function line(id,items,key,color){const host=document.getElementById(id);if(!host)return;color=color||"#03BAB3";
  windowed(host,items,(box,data)=>box.appendChild(buildLine(data,key,color)));}
function buildVbars(items,key,labelKey,extra){
  const W=520,H=220,pL=40,pR=12,pT=18,pB=34,vals=items.map(i=>i[key]),max=Math.max(1,...vals),pw=W-pL-pR,ph=H-pT-pB;
  const s=el("svg",{viewBox:`0 0 ${W} ${H}`});if(!items.length)return EMPTY(s,W,H);
  gridY(s,max,pL,pR,pT,ph,W);
  const step=pw/items.length,bw=Math.min(54,step*0.6),lstep=Math.ceil(items.length/12),showVal=items.length<=14;
  items.forEach((it,i)=>{const x=pL+i*step+step/2,h=(it[key]/max)*ph,y=pT+ph-h;
    const rc=el("rect",{x:x-bw/2,y:y,width:bw,height:h,rx:3,fill:"#03BAB3"});rc.appendChild(el("title",null,`${it[labelKey]}: ${fmt(it[key])}`));s.appendChild(rc);
    if(showVal)s.appendChild(el("text",{x:x,y:y-5,"text-anchor":"middle",class:"vlab"},kfmt(it[key])));
    if(i%lstep===0||i===items.length-1)s.appendChild(el("text",{x:x,y:H-18,"text-anchor":"middle",class:"tick"},it[labelKey]));
    if(extra&&it[extra]!=null&&(i%lstep===0||i===items.length-1))s.appendChild(el("text",{x:x,y:H-6,"text-anchor":"middle",class:"tick",fill:(it[extra]>=0?"#06827b":"#c0392b")},(it[extra]>0?"+":"")+it[extra]+"%"));});
  return s;}
function vbars(id,items,key,labelKey,extra){const host=document.getElementById(id);if(!host)return;labelKey=labelKey||"k";
  windowed(host,items,(box,data)=>box.appendChild(buildVbars(data,key,labelKey,extra)));}
function buildStacked(items){
  const W=520,H=220,pL=40,pR=12,pT=18,pB=30,tot=items.map(i=>i.new+i.repeat),max=Math.max(1,...tot),pw=W-pL-pR,ph=H-pT-pB;
  const s=el("svg",{viewBox:`0 0 ${W} ${H}`});if(!items.length)return EMPTY(s,W,H);
  gridY(s,max,pL,pR,pT,ph,W);
  const step=pw/items.length,bw=Math.min(50,step*0.55),lstep=Math.ceil(items.length/12);
  items.forEach((it,i)=>{const x=pL+i*step+step/2;const hN=(it.new/max)*ph,hR=(it.repeat/max)*ph;
    let y=pT+ph;const r1=el("rect",{x:x-bw/2,y:y-hN,width:bw,height:hN,fill:"#03BAB3"});r1.appendChild(el("title",null,`${it.label} new: ${it.new}`));s.appendChild(r1);y-=hN;
    const r2=el("rect",{x:x-bw/2,y:y-hR,width:bw,height:hR,fill:"#FAB718"});r2.appendChild(el("title",null,`${it.label} repeat: ${it.repeat}`));s.appendChild(r2);
    if(i%lstep===0||i===items.length-1)s.appendChild(el("text",{x:x,y:H-6,"text-anchor":"middle",class:"tick"},it.label));});
  return s;}
function stacked(id,items){const host=document.getElementById(id);if(!host)return;
  windowed(host,items,(box,data)=>box.appendChild(buildStacked(data)));}
// ---- competition outlook (both views) ----
function projection(id,p){const host=document.getElementById(id);if(!host)return;host.innerHTML="";
  const rows=(p&&p.rows)||[];
  if(!rows.length){host.innerHTML='<p class="note">No qualifying signatures yet this quarter — wide open.</p>';return;}
  const max=Math.max(1,...rows.map(r=>r.proj));
  const w=document.createElement("div");w.className="projbars";
  rows.forEach(r=>{const row=document.createElement("div");row.className="projrow";
    row.innerHTML=`<div class="pname" title="${esc(r.k)}">${esc(r.k)}</div>
      <div class="ptrack"><div class="ppace" style="width:${Math.max(2,r.proj/max*100)}%"></div><div class="pnow" style="width:${Math.max(2,r.now/max*100)}%" title="${r.now} so far"></div></div>
      <div class="pval">${r.now}<span class="pmut"> → ${r.proj}</span></div>`;
    w.appendChild(row);});
  host.appendChild(w);}
// ---- management-only renderers (containers only emitted in the mgmt view) ----
function sparkrep(id,reps){const host=document.getElementById(id);if(!host)return;host.innerHTML="";
  if(!reps||!reps.length){host.innerHTML='<p class="note">No rep activity yet.</p>';return;}
  const grid=document.createElement("div");grid.className="sparkgrid";
  reps.forEach(rep=>{const ser=rep.series,vals=ser.map(p=>p.n),max=Math.max(1,...vals);
    const last=vals.at(-1),prev=vals.length>1?vals.at(-2):0,delta=last-prev;
    const W=160,H=44,pad=5,single=vals.length<=1,st=single?0:(W-2*pad)/(vals.length-1);
    const pts=vals.map((v,i)=>[single?W/2:pad+i*st,H-pad-(v/max)*(H-2*pad-4)]);
    const svg=el("svg",{viewBox:`0 0 ${W} ${H}`,class:"spark"});
    if(pts.length>1){let dp="";pts.forEach((p,i)=>dp+=(i?"L":"M")+p[0]+" "+p[1]+" ");svg.appendChild(el("path",{d:dp,fill:"none",stroke:"#03BAB3","stroke-width":2}));}
    pts.forEach((p,i)=>{const last_=i===pts.length-1,c=el("circle",{cx:p[0],cy:p[1],r:last_?3.2:2,fill:last_?"#FAB718":"#03BAB3"});c.appendChild(el("title",null,`${ser[i].label}: ${vals[i]}`));svg.appendChild(c);});
    const arrow=delta>0?"▲":delta<0?"▼":"–",acl=delta>0?"up":delta<0?"down":"flat";
    const cell=document.createElement("div");cell.className="sparkcell";
    cell.innerHTML=`<div class="sname" title="${esc(rep.k)}">${esc(rep.k)}</div><div class="snums"><span class="slast">${last}</span><span class="sdelta ${acl}">${arrow}${delta?Math.abs(delta):""}</span></div>`;
    cell.appendChild(svg);
    const f=document.createElement("div");f.className="sfoot";f.textContent=ser[0].label+" → "+ser.at(-1).label;cell.appendChild(f);
    grid.appendChild(cell);});
  host.appendChild(grid);}
function effTable(id,rows){const host=document.getElementById(id);if(!host)return;
  if(!rows||!rows.length){host.innerHTML='<p class="note">No rep data yet.</p>';return;}
  let h='<table class="mini"><thead><tr><th>Rep</th><th class="num">Doctors</th><th class="num">Bottles</th><th class="num">Bottles / doctor</th></tr></thead><tbody>';
  rows.forEach(r=>h+=`<tr><td>${esc(r.k)}</td><td class="num">${r.docs}</td><td class="num">${fmt(r.bottles)}</td><td class="num">${r.per_doc}</td></tr>`);
  host.innerHTML=h+"</tbody></table>";}
function lapsedPanel(id,l){const host=document.getElementById(id);if(!host||!l)return;
  const pct=l.total?Math.round(l.lapsed/l.total*100):0;
  host.innerHTML=`<div class="bigstat"><div class="n">${fmt(l.lapsed)}</div><div class="l">doctors lapsed (${l.days}+ days since last request)</div><div class="s">${pct}% of ${fmt(l.total)} reached · re-engagement pool</div></div>
   <div class="segbar"><div class="seg act" style="flex:${Math.max(l.active,0.001)}" title="Active (≤${l.days}d): ${l.active}"></div><div class="seg mid" style="flex:${Math.max(l.mid,0.001)}" title="${l.days}–${2*l.days}d: ${l.mid}"></div><div class="seg deep" style="flex:${Math.max(l.deep,0.001)}" title=">${2*l.days}d: ${l.deep}"></div></div>
   <div class="seglegend"><span><i class="act"></i>Active ${l.active}</span><span><i class="mid"></i>${l.days}–${2*l.days}d ${l.mid}</span><span><i class="deep"></i>${2*l.days}d+ ${l.deep}</span></div>`;}
function dqPanel(id,d){const host=document.getElementById(id);if(!host||!d)return;
  // [label, count, sub, warn?] — no-referrer is informational (pre-launch, attributed to Krish), not a warning.
  const items=[["No referrer named",d.no_rep,`${fmt(d.no_rep_bottles)} bottles · pre-launch, credited to Krish`,false],
    ["Missing province",d.no_province,d.no_province?"can’t be placed on a territory":"all resolved via postal code",d.no_province>0],
    ["Missing licence #",d.no_license,"dedupe falls back to name",d.no_license>0]];
  host.innerHTML='<div class="dqgrid">'+items.map(it=>`<div class="dqcard${it[3]?' warn':' ok'}"><div class="n">${fmt(it[1])}</div><div class="l">${esc(it[0])}</div><div class="s">${esc(it[2])}</div></div>`).join("")+`</div><p class="note">Of ${fmt(d.total)} submissions all-time.</p>`;}

(function(){
  const k=DATA.consumption.kpis,t=DATA.team,c=DATA.current,run=DATA.consumption.run;
  const top=c.rows&&c.rows[0];
  const nobody=top&&NONREP.has(top.rep);
  const lead=!top?"—":(nobody?"Nobody":`${top.rep} (${top.points})`);
  const repsCount=(c.rows||[]).filter(r=>!NONREP.has(r.rep)).length;
  const cards=IS_PUBLIC?[
    [c.label,"Current quarter",`${c.days_left} days left`],
    [lead,"Current leader",nobody?"no rep is leading":"unique doctors signed"],
    [repsCount,"Reps competing","this quarter"],
    [fmt(t.clinics),"Doctors reached","all-time, nationwide"],
    [fmt(t.bottles),"Sample bottles","into clinics all-time"],
  ]:[
    [fmt(k.bottles),"Sample bottles",`${fmt(k.drops)} drops`],
    [k.requests,"Sample requests",`${k.unique_docs} unique doctors`],
    [k.avg_per_req,"Avg bottles / request","typical order size"],
    [k.avg_bottles_per_q,"Avg bottles / quarter",`${k.avg_req_per_q} requests/qtr`],
    [k.reorder_pct+"%","Reorder rate",(k.median_gap?`~${k.median_gap}d between`:"repeat doctors")],
  ];
  document.getElementById("kpis").innerHTML=cards.map(x=>`<div class="kpi"><div class="n">${esc(x[0])}</div><div class="l">${esc(x[1])}</div><div class="s">${esc(x[2])}</div></div>`).join("");
})();
document.getElementById("cqlabel").textContent=DATA.current.label+" standings";
document.getElementById("cqdays").textContent=DATA.current.days_left+" days left";
leaderboard(document.getElementById("leaderboard"),DATA.current);
pastQ(document.getElementById("past"),DATA.past);

{
  // Charts render in BOTH the public board and the management view (shared code path).
  const c=DATA.consumption;
  line("byMonth",c.by_month,"bottles");
  line("reqMonth",c.by_month,"requests","#06827b");
  line("avgMonth",c.by_month,"avg","#FAB718");
  line("cumulative",c.cumulative,"total");
  hbars("byProvince",c.by_province,"bottles");
  hbars("byRep",c.by_rep,"bottles");
  vbars("byQuarter",c.by_quarter,"bottles","label","growth");
  vbars("adoption",c.adoption,"n","k");
  vbars("orderMix",c.order_mix,"n","k");
  vbars("reach",c.reach,"new","label");
  stacked("newrep",c.newrep);
  // competition outlook (shared) + management-only analytics (containers gate rendering)
  projection("projection",c.projection);
  sparkrep("momentum",c.momentum);
  vbars("byRegion",c.by_region,"bottles","k");
  lapsedPanel("lapsed",c.lapsed);
  effTable("efficiency",c.efficiency);
  dqPanel("dataquality",c.dq);
  const rrEl=document.getElementById("runrate");
  if(rrEl){const rr=c.run,pct=rr.total?Math.round(rr.elapsed/rr.total*100):0;
    rrEl.innerHTML=`<div class="n">${fmt(rr.projected)}</div><div class="l">Projected bottles, ${esc(rr.label)}</div><div class="s">${fmt(rr.so_far)} so far · ${pct}% of quarter elapsed</div>`;}
}
if(typeof RECORDS!=="undefined"){
    const H=[["date","Date"],["clinic","Clinic"],["doctor","Doctor"],["province","Prov"],["phone","Phone"],["address","Address"],["samples","Bottles"],["license","Licence"],["rep","Referrer"]];
    document.getElementById("thead").innerHTML=H.map(h=>`<th data-k="${h[0]}">${esc(h[1])}</th>`).join("");
    let sk="date",sd=-1,flt="",rep="";const sel=document.getElementById("repf");
    sel.innerHTML='<option value="">All referrers</option>'+[...new Set(RECORDS.map(r=>r.rep))].sort().map(r=>`<option>${esc(r)}</option>`).join("");
    function draw(){let rows=RECORDS.filter(r=>(!rep||r.rep===rep)&&(!flt||(r.clinic+" "+r.doctor+" "+r.address+" "+r.rep+" "+r.license).toLowerCase().includes(flt)));
      rows.sort((x,y)=>{let a=x[sk],b=y[sk];if(sk==="samples")return(a-b)*sd;return String(a).localeCompare(String(b))*sd;});
      document.getElementById("tbody").innerHTML=rows.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.clinic)}</td><td>${esc(r.doctor)}</td><td>${esc(r.province)}</td><td>${esc(r.phone)}</td><td>${esc(r.address)}</td><td class="num">${r.samples}</td><td>${esc(r.license)}</td><td>${esc(r.rep)}</td></tr>`).join("");
      document.getElementById("rc").textContent=`${rows.length} of ${RECORDS.length}`;}
    document.querySelectorAll("#thead th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(k===sk)sd*=-1;else{sk=k;sd=(k==="samples"||k==="date")?-1:1;}draw();});
    document.getElementById("search").oninput=e=>{flt=e.target.value.toLowerCase().trim();draw();};sel.onchange=e=>{rep=e.target.value;draw();};draw();
  }
document.getElementById("stamp").textContent=DATA.stamp;
"""

def page(data, records, mode, cipher=None):
    is_public = (mode == "public")
    banner = ('<div class="banner pub">Public standings. Aggregate competition results only — no patient or physician details. Score = unique doctors signed per rep.</div>'
              if is_public else
              '<div class="banner priv"><strong>Confidential — management view.</strong> Sample-consumption analytics plus physician contact detail. Keep private.</div>')
    title = "Zimed Sampling Competition" + ("" if is_public else " — Management View")
    img = brand_img(is_public)
    img_tag = f'<img class="prod" src="{img}" alt="Zimed PF">' if img else ""
    charts = """
    <div class="sec">Volume &amp; trend</div>
    <div class="cols">
      <div class="card"><h2>Sample bottles by month</h2><p class="note">Total bottles requested each month (all-time).</p><div id="byMonth"></div></div>
      <div class="card"><h2>Cumulative bottles</h2><p class="note">Running total into clinics over time.</p><div id="cumulative"></div></div>
    </div>
    <div class="cols">
      <div class="card"><h2>Avg bottles per request over time</h2><p class="note">Order size trend (monthly).</p><div id="avgMonth"></div></div>
      <div class="card"><h2>Requests by month</h2><p class="note">Frequency — how many forms signed each month.</p><div id="reqMonth"></div></div>
    </div>
    <div class="sec">Growth &amp; geography</div>
    <div class="cols">
      <div class="card"><h2>Bottles by quarter</h2><p class="note">Quarterly volume with quarter-over-quarter growth %.</p><div id="byQuarter"></div></div>
      <div class="card"><h2>Bottles by province</h2><p class="note">Where the samples are going.</p><div id="byProvince"></div></div>
    </div>
    <div class="card"><h2>Bottles by referrer</h2><p class="note">Total volume credited to each name (everyone, incl. non-competitors).</p><div id="byRep"></div></div>
    <div class="sec">Adoption, reach &amp; cadence</div>
    <div class="cols">
      <div class="card"><h2>Sample adoption progression</h2><p class="note">Doctors grouped by how many times they've requested.</p><div id="adoption"></div></div>
      <div class="card"><h2>Order-size mix</h2><p class="note">Bottles requested per form.</p><div id="orderMix"></div></div>
    </div>
    <div class="cols">
      <div class="card"><h2>New doctors reached by quarter</h2><p class="note">First-time prescribers signing each quarter.</p><div id="reach"></div></div>
      <div class="card"><h2>New vs repeat volume by quarter</h2><p class="note"><span class="pill">teal = new</span> &nbsp; <span class="pill" style="background:#fff3d6;color:#9a6b00">gold = repeat</span></p><div id="newrep"></div></div>
    </div>
    <div class="sec">Competition outlook</div>
    <div class="cols">
      <div class="card"><h2>Current-quarter run-rate</h2><p class="note">Projected bottle volume if the current pace holds.</p><div class="kpi" id="runrate" style="margin-top:8px"></div></div>
      <div class="card"><h2>On pace to win</h2><p class="note">Unique doctors so far <span class="pmut">→ projected at the current rate</span> for each competing rep.</p><div id="projection"></div></div>
    </div>"""
    mgmt = "" if is_public else """
    <div class="sec">Management analytics <span class="mgmttag">internal</span></div>
    <div class="card"><h2>Rep momentum</h2><p class="note">Unique doctors signed each quarter, per competing rep. Latest quarter in gold; arrow shows change vs the prior quarter.</p><div id="momentum"></div></div>
    <div class="cols">
      <div class="card"><h2>Bottles by territory</h2><p class="note">Provinces rolled up West / Central / Atlantic.</p><div id="byRegion"></div></div>
      <div class="card"><h2>Lapsed reach</h2><p class="note">Doctors who haven’t requested recently — the re-engagement pool.</p><div id="lapsed"></div></div>
    </div>
    <div class="cols">
      <div class="card"><h2>Rep efficiency</h2><p class="note">Doctors reached and average order size per competing rep.</p><div id="efficiency"></div></div>
      <div class="card"><h2>Data-quality flags</h2><p class="note">Submissions missing fields that weaken attribution or geography.</p><div id="dataquality"></div></div>
    </div>"""
    table = "" if is_public else """
    <div class="sec">Full data</div>
    <div class="card"><h2>All submissions</h2><p class="note">Click a header to sort; type to filter.</p>
      <div class="toolbar"><input id="search" placeholder="Search clinic, doctor, address, referrer…"><select id="repf"></select><span class="note" id="rc"></span></div>
      <div class="tin"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div></div>"""
    if mode == "encrypted":
        boot = f"const IS_PUBLIC=false;\nconst CIPHER={dumps(cipher)};\n" + GATE_JS
    else:
        boot = f"const DATA={dumps(data)};\nconst IS_PUBLIC={'true' if is_public else 'false'};\n"
        if mode == "private":
            boot += f"const RECORDS={dumps(records)};\n"
        boot += "renderAll();\n"
    script = f"const NONREP=new Set({dumps(NONREP_NAMES)});\n" + "function renderAll(){\n" + JS + "\n}\n" + boot
    robots = '<meta name="robots" content="noindex">' if mode != "private" else ''
    foot = ('Source: live JotForm “Zimed PF Sample Request Form”. Competition score = unique doctors who signed and named each rep '
            '(a doctor signing again does not add a point); Krish and Aymeric are excluded from standings. '
            'Zimed PF (bimatoprost 0.03%) is a <span class="luvo">LUVO</span> brand distributed by Clarion Medical. '
            'Auto-refreshed about every 20 minutes · <span id="stamp"></span>.')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{robots}
<title>{title}</title><style>{CSS}</style></head><body>
<header>{DROP_SVG}<div><h1>{title}</h1>
<div class="sub">Live from JotForm · score = unique doctors signed per rep · Zimed PF by LUVO / Clarion</div></div>{img_tag}</header>
<div class="wrap">
{banner}
<div class="kpis" id="kpis"></div>
<div class="card"><h2><span id="cqlabel"></span><span class="countdown" id="cqdays"></span></h2>
<p class="note">Live competition. Top three win {' / '.join('$'+format(p,',') for p in PRIZES)}. Score = unique doctors signed.</p>
<div id="leaderboard"></div></div>
<div class="card"><h2>Past quarters</h2><p class="note">Final standings by quarter. 🏆 = winner.</p><div id="past"></div></div>
{charts}
{mgmt}
{table}
<div class="foot">{foot}</div></div>
<script>{script}</script></body></html>"""

GATE_JS = r"""
(function(){
  const u=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const ov=document.createElement("div");
  ov.style.cssText="position:fixed;inset:0;z-index:9999;background:#eef5f4;display:grid;place-items:center;font-family:-apple-system,Segoe UI,Roboto,sans-serif";
  ov.innerHTML='<div style="background:#fff;border:1px solid #dfeae8;border-radius:14px;padding:28px 30px;max-width:360px;width:90%;text-align:center;box-shadow:0 12px 34px rgba(0,0,0,.10)">'
   +'<div style="font-size:30px">🔒</div><h2 style="margin:8px 0 4px;color:#06827b">Zimed Management View</h2>'
   +'<p style="font-size:13px;color:#6a807e;margin:0 0 14px">Enter the passphrase shared with you.</p>'
   +'<input id="pw" type="password" placeholder="Passphrase" autocomplete="off" style="width:100%;padding:10px 12px;border:1px solid #dfeae8;border-radius:8px;font-size:14px;box-sizing:border-box"/>'
   +'<div id="gerr" style="color:#c0392b;font-size:12px;height:16px;margin-top:6px"></div>'
   +'<button id="go" style="margin-top:8px;width:100%;padding:10px;border:0;border-radius:8px;background:#03BAB3;color:#fff;font-weight:700;font-size:14px;cursor:pointer">Unlock</button></div>';
  document.body.appendChild(ov);
  async function unlock(pass){
    try{
      const enc=new TextEncoder(),dec=new TextDecoder();
      const km=await crypto.subtle.importKey("raw",enc.encode(pass),"PBKDF2",false,["deriveKey"]);
      const key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:u(CIPHER.s),iterations:200000,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
      const pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:u(CIPHER.iv)},key,u(CIPHER.ct));
      const obj=JSON.parse(dec.decode(pt));
      window.DATA=obj.DATA;window.RECORDS=obj.RECORDS;ov.remove();renderAll();
    }catch(e){document.getElementById("gerr").textContent="Wrong passphrase. Try again.";}
  }
  const go=()=>unlock(document.getElementById("pw").value);
  ov.querySelector("#go").onclick=go;
  ov.querySelector("#pw").addEventListener("keydown",e=>{if(e.key==="Enter")go();});
  ov.querySelector("#pw").focus();
})();
"""

def encrypt_payload(passphrase, obj):
    import hashlib
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pt = json.dumps(obj, ensure_ascii=False).encode()
    # Deterministic salt/IV derived from (passphrase, plaintext): identical data ->
    # identical ciphertext (so the scheduled commit is a no-op when data is unchanged),
    # while any data change yields a fresh salt+nonce. No nonce reuse across different
    # plaintexts because both derive from the plaintext hash.
    seed = hashlib.sha256(passphrase.encode() + b"|" + pt).digest()
    salt = seed[:16]
    iv = hashlib.sha256(b"iv|" + seed).digest()[:12]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000).derive(passphrase.encode())
    ct = AESGCM(key).encrypt(iv, pt, None)
    b = lambda x: base64.b64encode(x).decode()
    return {"s": b(salt), "iv": b(iv), "ct": b(ct)}

def pii_guard(html, records):
    return [v for r in records for f in ("email", "phone") if (v := r.get(f)) and v in html]

def main():
    args = set(sys.argv[1:])
    out_dir = sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else "."
    records = fetch_records()
    data = build(records)

    if "--encrypted" in args:
        pw = (os.environ.get("MGMT_PASSPHRASE") or "").strip()
        if not pw: sys.exit("MGMT_PASSPHRASE env not set; refusing to build management page.")
        cipher = encrypt_payload(pw, {"DATA": data, "RECORDS": records})
        html = page(data, records, "encrypted", cipher)
        leak = pii_guard(html, records)
        if leak: sys.exit(f"ABORT: {len(leak)} PII values leaked into the encrypted file (should be impossible).")
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "index.html"), "w").write(html)
        print(f"records={len(records)} -> {out_dir}/index.html (ENCRYPTED management view, no plaintext PII)")
        return

    pub = page(data, None, "public")
    leak = pii_guard(pub, records)
    if leak: sys.exit(f"ABORT: {len(leak)} PII values would leak into public file.")
    if "--public-only" in args:
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "index.html"), "w").write(pub)
        print(f"records={len(records)} reached={data['team']['clinics']} bottles={data['team']['bottles']} PII=OK -> {out_dir}/index.html")
    else:
        os.makedirs(os.path.join(OUT, "public"), exist_ok=True)
        os.makedirs(os.path.join(OUT, "private"), exist_ok=True)
        open(os.path.join(OUT, "public", "index.html"), "w").write(pub)
        open(os.path.join(OUT, "private", "index.html"), "w").write(page(data, records, "private"))
        print(f"records={len(records)} reached={data['team']['clinics']} bottles={data['team']['bottles']} PII=OK -> out/public + out/private")

if __name__ == "__main__":
    main()
