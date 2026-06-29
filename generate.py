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

# Contest periods. The contest normally runs per QUARTER, but Bryan + Krish set the first
# half of 2026 (Q1+Q2 2026) as a single combined payout period "H1 2026". That is a one-time
# exception — Q3 2026 onward go back to quarters. To add another combined period later, extend
# COMBINED with {(year, (q1,q2,...)): ("Label", (start_m,start_d), (end_m,end_d))}.
COMBINED = {(2026, (1, 2)): ("H1 2026", (1, 1), (6, 30))}
def period_of(year, q):
    """Map a (year, quarter) to its contest period -> (key, label, start_date, end_date)."""
    y = int(year)
    for (cy, qs), (label, sd, ed) in COMBINED.items():
        if y == cy and q in qs:
            return (f"{cy}-{label.split()[0]}", label, datetime.date(cy, *sd), datetime.date(cy, *ed))
    start = datetime.date(y, {1: 1, 2: 4, 3: 7, 4: 10}[q], 1)
    return (f"{y}-Q{q}", qlabel(y, q), start, quarter_end(y, q))

def build_competition(records, today, current_reps):
    buckets = {}  # period key -> rep -> set of unique doctors
    qdocs = {}    # period key -> set of unique doctors across ALL contest participants
    pmeta = {}    # period key -> (label, end_date)
    for r in records:
        pkey, plabel, _ps, pend = period_of(r["year"], r["q"])
        pmeta[pkey] = (plabel, pend)
        # Period-level unique-doctor total: every participant except Krish/Aymeric/blank
        # (blanks are remapped to Krish). Includes "Unsure"/"Another doctor" and departed reps.
        if r["rep"] not in EXCLUDE:
            qdocs.setdefault(pkey, set()).add(r["dkey"])
        # Per-rep scoring: excluded-free AND still a valid current dropdown option.
        if r["rep"] in EXCLUDE or r["rep"] not in current_reps: continue
        buckets.setdefault(pkey, {}).setdefault(r["rep"], set()).add(r["dkey"])
    def rows(key):
        reps = buckets.get(key, {})
        return [{"rep": rep, "points": n} for rep, n in
                sorted(((rep, len(ds)) for rep, ds in reps.items()), key=lambda x: -x[1])]
    udocs = lambda key: len(qdocs.get(key, ()))
    cy, cq = today.year, (today.month-1)//3+1
    ckey, clabel, _cs, cend = period_of(cy, cq)
    current = {"label": clabel, "days_left": max(0, (cend - today).days),
               "prizes": PRIZES, "rows": rows(ckey), "udocs": udocs(ckey)}
    past = []
    for pkey in sorted(pmeta, key=lambda k: pmeta[k][1], reverse=True):
        if pkey == ckey: continue
        rr = rows(pkey)
        if not rr and udocs(pkey) == 0: continue  # skip empty periods (e.g. all-blank quarters)
        past.append({"label": pmeta[pkey][0], "rows": rr, "winner": rr[0]["rep"] if rr else None,
                     "udocs": udocs(pkey)})
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
    # run-rate + projection for the current CONTEST PERIOD (a quarter, or H1 2026)
    cy, cq = today.year, (today.month-1)//3+1
    pkey, plabel, pstart, pend = period_of(cy, cq)
    period_recs = [r for r in records if period_of(r["year"], r["q"])[0] == pkey]
    cur_b = sum(r["samples"] for r in period_recs)
    elapsed = (today - pstart).days + 1; total_days = (pend - pstart).days + 1
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

    # competition projection: current-period unique doctors per rep, projected at pace
    proj = []
    for rep in eligible:
        now = len(set(r["dkey"] for r in period_recs if r["rep"] == rep))
        projected_docs = round(now/frac) if frac > 0 else now
        if now or projected_docs:
            proj.append({"k": rep, "now": now, "proj": max(projected_docs, now)})
    proj.sort(key=lambda x: (-x["proj"], -x["now"]))
    projection = {"label": plabel, "pct_elapsed": round(frac*100),
                  "days_left": max(0, (pend - today).days), "rows": proj}

    return {
        "kpis": {"bottles": bottles, "drops": bottles*DROPS_PER_BOTTLE, "requests": len(records),
                 "unique_docs": uniq, "avg_per_req": round(bottles/len(records),1) if records else 0,
                 "reorder_pct": round(100*reorder/uniq) if uniq else 0,
                 "avg_req_per_q": round(len(records)/nq,1), "avg_bottles_per_q": round(bottles/nq),
                 "median_gap": median_gap},
        "by_month": by_month, "cumulative": cum, "by_province": by_province, "by_rep": by_rep,
        "by_quarter": by_quarter, "adoption": adoption, "order_mix": order_mix,
        "reach": reach, "newrep": newrep,
        "run": {"label": plabel, "so_far": cur_b, "projected": projected,
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
:root{
 --ink:#0C2624;--abyss:#061A18;--abyss2:#0C302C;--teal:#03BAB3;--teal-d:#06827B;
 --teal-bright:#5BE9E0;--gold:#FAB718;--gold-d:#B07C0A;--paper:#EFF5F3;--surface:#FFFFFF;
 --line:#D8E7E3;--line2:#E7F0EE;--muted:#5E7672;--muted-dk:#88ADA7;--ink-dk:#EAF7F4;
 --warn:#C0562F;--warn-bg:#FBEEE7;
 --disp:'Archivo','Inter',system-ui,-apple-system,sans-serif;
 --body:'Inter',system-ui,-apple-system,"Segoe UI",sans-serif;
 --mono:'IBM Plex Mono',ui-monospace,"SF Mono",Menlo,monospace;}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--body);
 font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased;line-height:1.5}
/* --- mono eyebrow with teardrop bullet: the signature label voice --- */
.eyebrow{font-family:var(--mono);font-size:10.5px;font-weight:600;text-transform:uppercase;
 letter-spacing:.2em;color:var(--teal-d);display:flex;align-items:center;gap:9px}
.drip{width:7px;height:7px;background:var(--teal);border-radius:50% 50% 50% 0;
 transform:rotate(45deg);display:inline-block;flex:none}
/* ================= dark instrument hero ================= */
.hero{background:radial-gradient(120% 140% at 12% -10%,var(--abyss2),var(--abyss) 62%);
 color:var(--ink-dk);border-bottom:2px solid var(--gold);padding:26px 30px 30px;position:relative;overflow:hidden}
.hero:after{content:"";position:absolute;right:-90px;top:-120px;width:340px;height:340px;
 background:radial-gradient(circle,rgba(3,186,179,.20),transparent 70%);pointer-events:none}
.herobar{max-width:1180px;margin:0 auto;display:flex;align-items:center;gap:14px;position:relative;z-index:1}
.hero svg.mark{width:34px;height:34px;flex:none;filter:drop-shadow(0 3px 8px rgba(0,0,0,.4))}
.hero h1{margin:0;font-family:var(--disp);font-size:21px;font-weight:800;letter-spacing:-.01em;line-height:1.05}
.hero .org{margin-left:auto;font-family:var(--mono);font-size:10.5px;letter-spacing:.18em;
 text-transform:uppercase;color:var(--muted-dk);text-align:right;line-height:1.7}
.hero img.prod{height:74px;margin-left:18px;background:#fff;padding:5px;border-radius:8px;
 box-shadow:0 8px 22px rgba(0,0,0,.35)}
.marquee{max-width:1180px;margin:22px auto 0;display:grid;grid-template-columns:1.05fr 1.15fr 1fr;
 gap:1px;background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.10);border-radius:14px;
 overflow:hidden;position:relative;z-index:1}
.mq{background:linear-gradient(180deg,rgba(255,255,255,.03),transparent);padding:16px 20px 18px}
.mq .lab{font-family:var(--mono);font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--muted-dk)}
.mq .big{font-family:var(--disp);font-weight:800;font-size:30px;line-height:1.04;margin-top:7px;letter-spacing:-.01em}
.mq .big.gold{color:var(--gold)}
.mq .sub{font-size:12px;color:var(--muted-dk);margin-top:5px}
.mq.live .lab:after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;
 background:var(--teal-bright);margin-left:8px;box-shadow:0 0 0 0 rgba(91,233,224,.7);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(91,233,224,.6)}70%{box-shadow:0 0 0 7px rgba(91,233,224,0)}100%{box-shadow:0 0 0 0 rgba(91,233,224,0)}}
/* ================= layout ================= */
.wrap{max-width:1180px;margin:0 auto;padding:24px 22px 64px}
.sec{margin:34px 0 2px;font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;
 letter-spacing:.2em;color:var(--teal-d);display:flex;align-items:center;gap:9px;
 padding-bottom:10px;border-bottom:1px solid var(--line)}
.sec:before{content:"";width:7px;height:7px;background:var(--teal);border-radius:50% 50% 50% 0;transform:rotate(45deg);flex:none}
.banner{border-radius:10px;padding:11px 15px;font-size:12px;margin:18px 0 4px;border:1px solid}
.banner.pub{background:#E4F4F1;border-color:#BBE6E0;color:#075E58}
.banner.priv{background:var(--warn-bg);border-color:#F1C3AE;color:#9A3A1E}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin:18px 0 4px;
 background:var(--surface);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.kpi{padding:15px 18px;border-left:1px solid var(--line2);position:relative}
.kpi:first-child{border-left:0}
.kpi .n{font-family:var(--disp);font-size:25px;font-weight:800;line-height:1;letter-spacing:-.01em}
.kpi .l{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--teal-d);margin-top:9px}
.kpi .s{font-size:11px;color:var(--muted);margin-top:4px}
.rr{padding:2px 0 1px}
.rrhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.rrn{font-family:var(--disp);font-size:30px;font-weight:800;line-height:1;letter-spacing:-.01em}
.rru{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--teal-d)}
.rrtrack{background:var(--line2);border-radius:8px;height:22px;overflow:hidden;margin:15px 0 9px}
.rrfill{height:100%;background:linear-gradient(90deg,var(--teal-d),var(--teal));border-radius:8px;transition:width .9s cubic-bezier(.2,.8,.2,1)}
.rrleg{display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-size:12px;color:var(--muted)}
.rrleg b{color:var(--ink);font-size:13px}
.chleg{display:flex;flex-wrap:wrap;gap:7px 16px;margin:0 0 13px;font-size:11px;color:var(--muted)}
.chleg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.tip{position:relative;display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;margin-left:7px;
 border:1px solid var(--line);border-radius:50%;font-family:var(--body);font-weight:700;font-size:10px;font-style:italic;
 color:var(--teal-d);cursor:help;vertical-align:middle}
.tipc{display:none;position:absolute;z-index:60;left:0;top:150%;
 width:min(330px,86vw);background:var(--abyss2);color:#EAF7F4;border:1px solid rgba(255,255,255,.12);border-radius:11px;
 padding:13px 14px;font-family:var(--body);font-size:11.5px;font-weight:400;font-style:normal;line-height:1.6;
 letter-spacing:0;text-transform:none;text-align:left;box-shadow:0 18px 44px rgba(0,0,0,.4)}
.tipc b{color:#fff}
.tip:hover .tipc,.tip:focus .tipc,.tip:focus-within .tipc{display:block}
.card{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px 20px 16px;margin-top:14px;
 box-shadow:0 1px 2px rgba(12,38,36,.03)}
.card h2{margin:0 0 3px;font-family:var(--disp);font-size:16px;font-weight:700;letter-spacing:-.01em}
.card .note{font-size:11.5px;color:var(--muted);margin:0 0 13px;line-height:1.55}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}.cols .card{margin-top:0}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:10.5px;font-weight:700;background:#E1F4F1;color:#075E58}
.countdown{font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.08em;color:var(--gold-d);
 background:#FCF3DC;border:1px solid #F2DDA0;padding:3px 10px;border-radius:20px;margin-left:10px;vertical-align:middle}
/* ================= leaderboard ================= */
.lb{display:flex;flex-direction:column;gap:5px}
.lrow{display:grid;grid-template-columns:42px 1fr 160px 80px;align-items:center;gap:13px;padding:9px 10px;
 border-radius:10px;border:1px solid transparent;transition:background .2s}
.lrow.top{background:#F2FAF9}
.lrow.lead{background:linear-gradient(90deg,#FFF8E6,#FFFDF7);border-color:#F0DCA0}
.lrow .rank{text-align:center}.lrow .who{font-weight:600;font-size:13.5px}
.lrow .barwrap{background:var(--line2);border-radius:7px;height:20px;overflow:hidden}
.lrow .bar{height:100%;background:linear-gradient(90deg,var(--teal-d),var(--teal));border-radius:7px;transition:width .9s cubic-bezier(.2,.8,.2,1)}
.lrow.lead .bar{background:linear-gradient(90deg,var(--gold-d),var(--gold))}
.lrow .pts{text-align:right;font-family:var(--disp);font-weight:800;font-size:17px;font-variant-numeric:tabular-nums}
.lrow .prize{font-family:var(--mono);font-size:10.5px;color:var(--gold-d);font-weight:600;margin-top:2px}
.lrow .prize.tie{color:var(--warn);font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.lrow .rank .rbadge{display:inline-block;min-width:27px;text-align:center;font-family:var(--disp);font-weight:800;
 font-size:13px;color:#5A716E;background:var(--line2);border-radius:8px;padding:4px 0}
.lrow .rank .rbadge.t{color:#B9531F;background:#FBE7DA}
.lrow .stake{font-family:var(--mono);font-size:9px;color:var(--gold-d);margin-top:1px}
.lrow.non{opacity:.6}.lrow.non .who{font-style:italic;font-weight:500}
.lrow .rank .rbadge.dash{background:none;color:#9FB0AE}
.lrow .gap{font-size:10.5px;color:var(--muted);margin-top:1px}.medal{font-size:18px;line-height:1}
/* ================= past contest periods ================= */
.qpast{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:13px}
.qbox{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:linear-gradient(180deg,#FBFDFC,#fff)}
.qbox h3{margin:0 0 2px;font-family:var(--disp);font-size:15px;font-weight:700}
.qbox .qtot{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--teal-d);margin:0 0 10px}
.qbox .r{display:flex;justify-content:space-between;font-size:12.5px;padding:3px 0;border-top:1px solid var(--line2)}
.qbox .r:first-of-type{border-top:0}
.qbox .r span:last-child{font-family:var(--disp);font-weight:700;font-variant-numeric:tabular-nums}
.qbox .r.win{font-weight:700}.qbox .r.non{opacity:.5;font-style:italic}
.card svg{width:100%;height:auto;display:block;overflow:visible}
.gridline{stroke:#EAF2F0}.tick{fill:var(--muted);font-size:10px;font-family:'Inter'}
.vlab{fill:var(--ink);font-size:10px;font-weight:700;font-family:'Archivo'}
.hbars{display:flex;flex-direction:column;gap:7px}
.hbar{display:grid;grid-template-columns:118px 1fr 60px;align-items:center;gap:11px;font-size:12px}
.hbar .t{background:var(--line2);border-radius:6px;height:17px;overflow:hidden}
.hbar .f{height:100%;background:linear-gradient(90deg,var(--teal-d),var(--teal));border-radius:6px}
.hbar .v{text-align:right;font-family:var(--disp);font-weight:700;font-variant-numeric:tabular-nums}
.hbar .f.gold{background:linear-gradient(90deg,var(--gold-d),var(--gold))}
.hbar.sales{grid-template-columns:58px 1fr 116px}
.hbar.sales .v .vu{display:block;font-family:var(--mono);font-size:9px;font-weight:500;color:var(--muted)}
.hbar.cust{grid-template-columns:164px 1fr 78px}
.hbar.cust>div:first-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line2);vertical-align:top}
th{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--teal-d);
 white-space:nowrap;cursor:pointer;border-bottom:1px solid var(--line)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.tin{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:11px}
.toolbar{display:flex;gap:10px;align-items:center;margin:4px 0 14px;flex-wrap:wrap}
.toolbar input{flex:1;min-width:200px;padding:10px 13px;border:1px solid var(--line);border-radius:9px;font-size:13px;font-family:var(--body)}
.toolbar input:focus,.toolbar select:focus{outline:2px solid var(--teal);outline-offset:1px}
.toolbar select{padding:9px 11px;border:1px solid var(--line);border-radius:9px;background:#fff;font-family:var(--body)}
.foot{font-size:11px;color:var(--muted);margin-top:30px;line-height:1.6;border-top:1px solid var(--line);padding-top:16px}
.luvo{font-weight:800;letter-spacing:.12em;color:var(--teal-d)}
.empty{fill:#AEC4C0;font-size:13px;font-weight:600;font-family:'Inter'}
.pmut{color:var(--muted);font-weight:500}
.mgmttag{font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.1em;background:var(--warn-bg);
 color:#9A3A1E;border:1px solid #F1C3AE;border-radius:20px;padding:2px 9px;vertical-align:middle;margin-left:8px}
/* windowing toggle */
.winctl{display:flex;justify-content:flex-end;align-items:center;gap:9px;font-size:10.5px;color:var(--muted);margin:-2px 0 4px}
.winctl button{font-family:var(--mono);font-size:9.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
 color:var(--teal-d);background:#E1F4F1;border:1px solid #BBE6E0;border-radius:20px;padding:3px 11px;cursor:pointer}
.winctl button:hover{background:#D2EFEC}
/* competition projection */
.projbars{display:flex;flex-direction:column;gap:8px;margin-top:2px}
.projrow{display:grid;grid-template-columns:100px 1fr 76px;align-items:center;gap:11px;font-size:12px}
.projrow .pname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.ptrack{position:relative;background:var(--line2);border-radius:6px;height:18px;overflow:hidden}
.ptrack .ppace{position:absolute;inset:0 auto 0 0;height:100%;background:repeating-linear-gradient(135deg,#CDEEEB,#CDEEEB 5px,#DEF4F2 5px,#DEF4F2 10px)}
.ptrack .pnow{position:absolute;inset:0 auto 0 0;height:100%;background:linear-gradient(90deg,var(--teal-d),var(--teal));border-radius:6px}
.projrow .pval{text-align:right;font-family:var(--disp);font-weight:700;font-variant-numeric:tabular-nums}
/* rep momentum small-multiples */
.sparkgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(152px,1fr));gap:12px;margin-top:4px}
.sparkcell{border:1px solid var(--line);border-radius:11px;padding:10px 12px 9px;background:linear-gradient(180deg,#FBFDFC,#fff)}
.sparkcell .sname{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sparkcell .snums{display:flex;align-items:baseline;gap:7px;margin:2px 0 3px}
.sparkcell .slast{font-family:var(--disp);font-size:22px;font-weight:800;line-height:1}
.sparkcell .sdelta{font-family:var(--mono);font-size:10.5px;font-weight:600}
.sdelta.up{color:var(--teal-d)}.sdelta.down{color:var(--warn)}.sdelta.flat{color:var(--muted)}
.spark{width:100%;height:44px;display:block}
.sparkcell .sfoot{font-family:var(--mono);font-size:8.5px;letter-spacing:.05em;color:var(--muted);margin-top:4px}
/* mini efficiency table */
table.mini{font-size:12.5px}table.mini th{cursor:default}
/* lapsed reach */
.bigstat .n{font-family:var(--disp);font-size:34px;font-weight:800;line-height:1}
.bigstat .l{font-size:12.5px;margin-top:5px}.bigstat .s{font-size:11.5px;color:var(--muted);margin-top:3px}
.segbar{display:flex;height:18px;border-radius:7px;overflow:hidden;margin:14px 0 8px;background:var(--line2)}
.seg{min-width:2px}.seg.act{background:#8FD4CE}.seg.mid{background:var(--gold)}.seg.deep{background:#E07A52}
.seglegend{display:flex;gap:15px;flex-wrap:wrap;font-family:var(--mono);font-size:10px;letter-spacing:.04em;color:var(--muted)}
.seglegend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:middle}
.seglegend i.act{background:#8FD4CE}.seglegend i.mid{background:var(--gold)}.seglegend i.deep{background:#E07A52}
/* data-quality */
.dqgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px}
.dqcard{border:1px solid var(--line);border-radius:11px;padding:13px 14px;background:linear-gradient(180deg,#FBFDFC,#fff)}
.dqcard.warn{border-color:#F1C9B6;background:#FFF8F4}
.dqcard .n{font-family:var(--disp);font-size:23px;font-weight:800;line-height:1}
.dqcard.warn .n{color:var(--warn)}.dqcard.ok .n{color:var(--teal-d)}
.dqcard .l{font-size:11.5px;font-weight:600;margin-top:5px}.dqcard .s{font-size:10.5px;color:var(--muted);margin-top:2px}
:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
@media(max-width:900px){
 .marquee{grid-template-columns:1fr}.hero img.prod{display:none}.hero .org{display:none}
 .kpis{grid-template-columns:repeat(2,1fr)}.kpi:nth-child(odd){border-left:0}
 .cols{grid-template-columns:1fr}.dqgrid{grid-template-columns:1fr}
 .projrow{grid-template-columns:84px 1fr 66px}.lrow{grid-template-columns:36px 1fr 1.2fr 64px;gap:9px}}
/* phones: tighten padding, shrink fixed bar-label columns so nothing forces sideways scroll,
   stack the table toolbar, and float the info tooltip as a fixed bottom card so it can't overflow */
@media(max-width:560px){
 .hero{padding:20px 16px 22px}.hero h1{font-size:18px}.hero .herobar{gap:11px}
 .wrap{padding:16px 13px 48px}
 .card{padding:15px 14px 13px}.card h2{font-size:15px}
 .sec{font-size:10px}
 .hbar{grid-template-columns:84px 1fr 48px;gap:8px;font-size:11.5px}
 .hbar>div:first-child{overflow-wrap:anywhere;line-height:1.25}
 .hbar.sales{grid-template-columns:38px 1fr 82px}
 .hbar.cust{grid-template-columns:102px 1fr 60px}
 .qpast{grid-template-columns:1fr}
 .lrow{grid-template-columns:30px 1fr 1.1fr 52px;gap:7px}
 .toolbar{flex-direction:column;align-items:stretch}
 .toolbar input{min-width:0;width:100%}.toolbar select{width:100%}
 .tip{position:static}
 .tipc{position:fixed;left:50%;right:auto;top:auto;bottom:18px;transform:translateX(-50%);width:min(340px,92vw);z-index:200}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

DROP_SVG = ('<svg class="mark" width="34" height="34" viewBox="0 0 24 24">'
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
    const d=document.createElement("div");d.className="lrow"+(isRep&&!tied&&rank<=3?" top":"")+(isRep&&!tied&&rank===1?" lead":"")+(isRep?"":" non");
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
  past.forEach(q=>{const b=document.createElement("div");b.className="qbox";
    let h=`<h3>${esc(q.label)}</h3><div class="qtot">${fmt(q.udocs)} unique doctor${q.udocs===1?'':'s'} signed</div>`;
    const rows=q.rows||[];
    // 🥇🥈🥉 to the top three competing reps (non-rep answers get no medal)
    const comp=rows.filter(r=>!NONREP.has(r.rep));
    const medal={};comp.slice(0,3).forEach((r,i)=>medal[r.rep]=MEDAL[i]);
    rows.slice(0,5).forEach(r=>{const non=NONREP.has(r.rep),md=medal[r.rep];
      h+=`<div class="r${md===MEDAL[0]?' win':''}${non?' non':''}"><span>${md?md+' ':''}${esc(r.rep)}</span><span>${r.points}</span></div>`;});
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
function runrate(id,rr){const host=document.getElementById(id);if(!host||!rr)return;
  const proj=rr.projected||0, so=rr.so_far||0;
  const pct=rr.total?Math.round(rr.elapsed/rr.total*100):0;        // share of the period elapsed
  const fill=proj?Math.max(2,Math.min(100,Math.round(so/proj*100))):0;  // bottles so far as a share of the projected total
  host.innerHTML='<div class="rr">'
    +`<div class="rrhead"><span class="rrn">${fmt(proj)}</span><span class="rru">projected sample bottles · ${esc(rr.label)}</span></div>`
    +`<div class="rrtrack"><div class="rrfill" style="width:${fill}%"></div></div>`
    +`<div class="rrleg"><span><b>${fmt(so)}</b> delivered so far</span><span class="pmut">${pct}% of period elapsed · ${fmt(rr.elapsed)}/${fmt(rr.total)} days</span></div>`
    +'</div>';}
// ---- Zimed SALES renderers (management-only; DATA.sales only exists in the encrypted/private payload) ----
const money=n=>"$"+fmt(Math.round(+n));
function salesProvince(id,items){const host=document.getElementById(id);if(!host)return;
  if(!items||!items.length){host.innerHTML='<p class="note">No sales data.</p>';return;}
  const max=Math.max(1,...items.map(i=>i.revenue));host.innerHTML="";
  const w=document.createElement("div");w.className="hbars";
  items.forEach(it=>{const row=document.createElement("div");row.className="hbar sales";
    row.innerHTML=`<div title="${esc(it.k)}">${esc(it.k)}</div><div class="t"><div class="f gold" style="width:${Math.max(3,it.revenue/max*100)}%"></div></div><div class="v">${money(it.revenue)}<span class="vu">${fmt(it.units)} bottles</span></div>`;
    w.appendChild(row);});host.appendChild(w);}
const CHCOL={national:"#06827B",retail:"#C98A00",regional:"#8AA3A0",other:"#B9C9C6"};
const CHLAB={national:"National wholesaler",retail:"Retail chain (self-distributing)",regional:"Regional wholesaler",other:"Other / direct"};
function salesCustomers(id,items){const host=document.getElementById(id);if(!host)return;
  if(!items||!items.length){host.innerHTML='<p class="note">No sales data.</p>';return;}
  const max=Math.max(1,...items.map(i=>i.revenue));host.innerHTML="";
  // legend only shows the channel types actually present, in a stable order
  const order=["national","retail","regional","other"];
  const used=order.filter(t=>items.some(i=>(i.type||"other")===t));
  const leg=document.createElement("div");leg.className="chleg";
  leg.innerHTML=used.map(t=>`<span><i style="background:${CHCOL[t]}"></i>${esc(CHLAB[t])}</span>`).join("");
  host.appendChild(leg);
  const w=document.createElement("div");w.className="hbars";
  items.forEach(it=>{const t=it.type||"other";const col=CHCOL[t]||CHCOL.other;
    const tip=`${it.k} (${CHLAB[t]||t}). ${it.note||""}`.trim();
    const row=document.createElement("div");row.className="hbar cust";
    row.innerHTML=`<div title="${esc(tip)}">${esc(it.k)}</div><div class="t"><div class="f" style="width:${Math.max(3,it.revenue/max*100)}%;background:${col}"></div></div><div class="v" title="${esc(tip)}">${money(it.revenue)}</div>`;
    w.appendChild(row);});host.appendChild(w);}
function buildSalesCombo(items){
  const W=520,H=220,pL=40,pR=46,pT=18,pB=28,u=items.map(i=>i.units),rev=items.map(i=>i.revenue);
  const umax=Math.max(1,...u),rmax=Math.max(1,...rev),pw=W-pL-pR,ph=H-pT-pB;
  const s=el("svg",{viewBox:`0 0 ${W} ${H}`});if(!items.length)return EMPTY(s,W,H);
  for(let g=0;g<=4;g++){const yv=Math.round(umax*g/4),y=pT+ph-(yv/umax)*ph;
    s.appendChild(el("line",{x1:pL,y1:y,x2:W-pR,y2:y,class:"gridline"}));
    s.appendChild(el("text",{x:pL-6,y:y+4,"text-anchor":"end",class:"tick"},kfmt(yv)));
    s.appendChild(el("text",{x:W-pR+6,y:y+4,class:"tick",fill:"#B07C0A"},kfmt(Math.round(rmax*g/4))));}
  const step=pw/items.length,bw=Math.min(32,step*0.5),lstep=Math.ceil(items.length/12);
  items.forEach((it,i)=>{const x=pL+i*step+step/2,h=(it.units/umax)*ph,y=pT+ph-h;
    const rc=el("rect",{x:x-bw/2,y:y,width:bw,height:h,rx:3,fill:"#03BAB3"});
    rc.appendChild(el("title",null,`${it.label}: ${fmt(it.units)} bottles · ${money(it.revenue)}`));s.appendChild(rc);
    if(i%lstep===0||i===items.length-1)s.appendChild(el("text",{x:x,y:H-9,"text-anchor":"middle",class:"tick"},it.label));});
  const pts=rev.map((v,i)=>[pL+i*step+step/2,pT+ph-(v/rmax)*ph]);
  if(pts.length>1){let dp="";pts.forEach((p,i)=>dp+=(i?"L":"M")+p[0]+" "+p[1]+" ");
    s.appendChild(el("path",{d:dp,fill:"none",stroke:"#FAB718","stroke-width":2.5}));}
  pts.forEach(p=>s.appendChild(el("circle",{cx:p[0],cy:p[1],r:3,fill:"#fff",stroke:"#FAB718","stroke-width":2})));
  return s;}
function salesMonth(id,items){const host=document.getElementById(id);if(!host)return;
  windowed(host,items,(box,data)=>box.appendChild(buildSalesCombo(data)));}
function samplesVsSales(id,samples,sales){const host=document.getElementById(id);if(!host)return;
  const sM={},vM={};(samples||[]).forEach(p=>sM[p.k]=p.bottles);(sales||[]).forEach(p=>vM[p.k]=p.units);
  const provs=[...new Set([...Object.keys(vM),...Object.keys(sM)])].filter(k=>k&&k!=="—").sort((a,b)=>(vM[b]||0)-(vM[a]||0));
  if(!provs.length){host.innerHTML='<p class="note">No data.</p>';return;}
  const W=520,H=224,pL=40,pR=12,pT=16,pB=28,max=Math.max(1,...provs.map(p=>Math.max(sM[p]||0,vM[p]||0)));
  const pw=W-pL-pR,ph=H-pT-pB;const s=el("svg",{viewBox:`0 0 ${W} ${H}`});host.innerHTML="";
  for(let g=0;g<=4;g++){const yv=Math.round(max*g/4),y=pT+ph-(yv/max)*ph;
    s.appendChild(el("line",{x1:pL,y1:y,x2:W-pR,y2:y,class:"gridline"}));
    s.appendChild(el("text",{x:pL-6,y:y+4,"text-anchor":"end",class:"tick"},kfmt(yv)));}
  const step=pw/provs.length,bw=Math.min(15,step*0.32);
  provs.forEach((p,i)=>{const cx=pL+i*step+step/2,sb=sM[p]||0,vb=vM[p]||0,h1=(sb/max)*ph,h2=(vb/max)*ph;
    const r1=el("rect",{x:cx-bw-1,y:pT+ph-h1,width:bw,height:h1,rx:2,fill:"#03BAB3"});r1.appendChild(el("title",null,`${p} sampled: ${fmt(sb)} bottles`));s.appendChild(r1);
    const r2=el("rect",{x:cx+1,y:pT+ph-h2,width:bw,height:h2,rx:2,fill:"#FAB718"});r2.appendChild(el("title",null,`${p} sold: ${fmt(vb)} bottles`));s.appendChild(r2);
    s.appendChild(el("text",{x:cx,y:H-9,"text-anchor":"middle",class:"tick"},p));});
  host.appendChild(s);}

(function(){
  const k=DATA.consumption.kpis,t=DATA.team,c=DATA.current;
  const top=c.rows&&c.rows[0];
  const repList=(c.rows||[]).filter(r=>!NONREP.has(r.rep)),repsCount=repList.length;
  const topRep=repList[0],nobody=!topRep;
  // --- hero marquee: the thesis (live competition + drops into clinics) ---
  const set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
  set("heroPeriod",c.label);
  set("heroPeriodSub",`${c.days_left} day${c.days_left===1?'':'s'} left · ${fmt(c.udocs)} doctors signed`);
  set("heroLeader",nobody?"Nobody yet":topRep.rep);
  set("heroLeaderSub",nobody?"no rep is leading":`${topRep.points} unique doctor${topRep.points===1?'':'s'}`);
  set("heroDrops",fmt(k.drops));
  set("heroDropsSub",`${fmt(t.bottles)} sample bottles · ≈100 drops each`);
  // --- secondary readout strip (same on both views) ---
  const cards=[
    [fmt(t.bottles),"Sample bottles","into clinics all-time"],
    [fmt(t.clinics),"Doctors reached","unique, nationwide"],
    [repsCount,"Reps competing","this period"],
    [fmt(k.requests),"Sample requests","request forms, all-time"],
    [k.avg_per_req,"Avg bottles / request","typical order size"],
  ];
  document.getElementById("kpis").innerHTML=cards.map(x=>`<div class="kpi"><div class="n">${esc(x[0])}</div><div class="l">${esc(x[1])}</div><div class="s">${esc(x[2])}</div></div>`).join("");
})();
document.getElementById("cqlabel").textContent=DATA.current.label+" standings";
document.getElementById("cqdays").textContent=DATA.current.days_left+" days left";
{const ct=document.getElementById("cqtot"),ud=DATA.current.udocs;
 if(ct)ct.textContent=fmt(ud)+" unique doctor"+(ud===1?"":"s")+" signed";}
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
  runrate("runrate",c.run);
  // Zimed sales (only present in the encrypted/private payload)
  if(DATA.sales){const sl=DATA.sales,t=sl.totals,tp=(sl.by_province||[])[0],sampled=(DATA.team&&DATA.team.bottles)||0;
    salesProvince("salesProvince",sl.by_province);
    salesMonth("salesMonth",sl.by_month);
    salesCustomers("salesCustomers",sl.by_customer);
    samplesVsSales("samplesVsSales",DATA.consumption.by_province,sl.by_province);
    const ratio=sampled?(t.units/sampled):0;
    const sk=[
      [money(t.revenue),"Net revenue",`program to date · thru ${esc(sl.through)}`],
      [fmt(t.units),"Bottles sold",`net of returns · ${t.orders} invoices, ${t.returns} returns`],
      ["$"+(t.units?Math.round(t.revenue/t.units):0),"Avg $ / bottle","across all paid sales"],
      [tp?tp.k:"—","Top province by revenue",tp?money(tp.revenue)+" sold":""],
      [ratio?ratio.toFixed(1)+"×":"—","Sold per sampled","paid bottles per free sample · directional"],
    ];
    const ske=document.getElementById("salesKpis");
    if(ske)ske.innerHTML=sk.map(x=>`<div class="kpi"><div class="n">${esc(x[0])}</div><div class="l">${esc(x[1])}</div><div class="s">${esc(x[2])}</div></div>`).join("");}
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
    has_sales = (not is_public) and bool(data and data.get("sales"))
    banner = ('<div class="banner pub">Public standings. Aggregate competition results only — no patient or physician details. Score = unique doctors signed per rep.</div>'
              if is_public else
              '<div class="banner priv"><strong>Confidential — management view.</strong> Sample-consumption analytics plus physician contact detail. Keep private.</div>')
    title = "Zimed Sampling Competition" if is_public else "Zimed PF — Management Dashboard"
    eyebrow_txt = ("Zimed PF sampling contest · live from JotForm" if is_public
                   else "Zimed PF program · sampling, sales &amp; analytics · live")
    img = brand_img(is_public)
    img_tag = f'<img class="prod" src="{img}" alt="Zimed PF">' if img else ""
    sales_through = (data.get("sales") or {}).get("through", "") if has_sales else ""
    prizes_str = ' / '.join('$'+format(p, ',') for p in PRIZES)
    # ---- Layout helpers + per-chart card snippets (IDs are stable; renderers find them by id,
    # so cards can be placed in any order/section/mode safely). The page is assembled into TWO
    # bodies below: the public board stays contest-first; the management view leads with product
    # movement, per Krish (2026-06-27). "Samples" = free bottles into clinics; "sales" = paid units. ----
    def _sec(t): return f'<div class="sec">{t}</div>'
    def _cols(*cards): return '<div class="cols">' + ''.join(cards) + '</div>'

    KPI_STRIP       = '<div class="kpis" id="kpis"></div>'
    SALES_KPI_STRIP = '<div class="kpis" id="salesKpis"></div>'

    C_CURRENT = ('<div class="card"><h2><span id="cqlabel"></span><span class="countdown" id="cqdays"></span></h2>'
                 '<div class="qtot" id="cqtot"></div>'
                 f'<p class="note">Live competition. Top three win {prizes_str}. Score = unique doctors signed.</p>'
                 '<div id="leaderboard"></div></div>')
    C_PAST = ('<div class="card"><h2>Past contest periods</h2><p class="note">Final standings by period. 🥇🥈🥉 = top three. '
              'The doctor count is the total unique doctors signed that period across all contest participants, deduped, so it can run higher '
              'than the rep rows shown (those list only the current top reps). It includes requests where the doctor was unsure or came from '
              'another doctor, and excludes forms attributed to Krish and Aymeric.</p><div id="past"></div></div>')

    # Sampling volume & trend
    C_BYMONTH    = '<div class="card"><h2>Sample bottles by month</h2><p class="note">Free sample bottles requested each month (all-time).</p><div id="byMonth"></div></div>'
    C_CUMULATIVE = '<div class="card"><h2>Cumulative sample bottles</h2><p class="note">Running total of sample bottles into clinics over time.</p><div id="cumulative"></div></div>'
    C_AVGMONTH   = '<div class="card"><h2>Avg sample bottles per request</h2><p class="note">Typical order size over time (monthly).</p><div id="avgMonth"></div></div>'
    C_REQMONTH   = '<div class="card"><h2>Sample requests by month</h2><p class="note">How many request forms were signed each month.</p><div id="reqMonth"></div></div>'
    C_BYQUARTER  = '<div class="card"><h2>Sample bottles by quarter</h2><p class="note">Quarterly sample volume with quarter-over-quarter growth %.</p><div id="byQuarter"></div></div>'
    # Reach & adoption
    C_REACH    = '<div class="card"><h2>New doctors reached by quarter</h2><p class="note">First-time prescribers signing each quarter.</p><div id="reach"></div></div>'
    C_ADOPTION = "<div class=\"card\"><h2>Sample adoption progression</h2><p class=\"note\">Doctors grouped by how many times they've requested samples.</p><div id=\"adoption\"></div></div>"
    C_ORDERMIX = '<div class="card"><h2>Order-size mix</h2><p class="note">Sample bottles requested per form.</p><div id="orderMix"></div></div>'
    C_NEWREP   = '<div class="card"><h2>New vs repeat sample volume by quarter</h2><p class="note"><span class="pill">teal = new</span> &nbsp; <span class="pill" style="background:#fff3d6;color:#9a6b00">gold = repeat</span></p><div id="newrep"></div></div>'
    # Geography
    C_BYPROVINCE = '<div class="card"><h2>Sample bottles by province</h2><p class="note">Where the free samples are landing, all-time (clinic location).</p><div id="byProvince"></div></div>'
    C_BYREGION   = '<div class="card"><h2>Sample bottles by territory</h2><p class="note">All-time sample volume, provinces rolled up West / Central / Atlantic.</p><div id="byRegion"></div></div>'
    # Contest outlook + attribution
    C_RUNRATE    = '<div class="card"><h2>Current-period run-rate</h2><p class="note">Projected sample bottle volume for the period if the current pace holds.</p><div id="runrate"></div></div>'
    C_PROJECTION = '<div class="card"><h2>On pace to win</h2><p class="note">Unique doctors so far <span class="pmut">→ projected at the current rate</span> for each competing rep.</p><div id="projection"></div></div>'
    C_BYREP      = '<div class="card"><h2>Sample bottles by referrer</h2><p class="note">Total sample volume, all-time, credited to each name (everyone, incl. non-competitors).</p><div id="byRep"></div></div>'
    # Management-only: contest momentum + operations
    C_MOMENTUM   = '<div class="card"><h2>Rep momentum</h2><p class="note">Unique doctors signed each quarter, per competing rep. Latest quarter in gold; arrow shows change vs the prior quarter.</p><div id="momentum"></div></div>'
    C_LAPSED     = "<div class=\"card\"><h2>Lapsed reach</h2><p class=\"note\">Doctors who've gone quiet since their last sample request, the re-engagement pool. The bands below group them by how long it's been.</p><div id=\"lapsed\"></div></div>"
    C_EFFICIENCY = '<div class="card"><h2>Rep efficiency</h2><p class="note">Doctors reached and average order size per competing rep.</p><div id="efficiency"></div></div>'
    C_DQ         = '<div class="card"><h2>Data-quality flags</h2><p class="note">Submissions missing fields that weaken attribution or geography.</p><div id="dataquality"></div></div>'
    # Paid sales (management-only, confidential)
    SALES_NOTE = (f'<div class="card" style="background:linear-gradient(180deg,#FBFDFC,#fff)"><p class="note" style="margin:0">'
                  '<strong>Samples vs sales.</strong> Samples are free bottles placed into clinics (the JotForm activity above). '
                  'Sales are paid units bought by wholesalers, from Clarion Finance invoices through '
                  f'<strong>{sales_through}</strong>. Confidential — this lives only inside this encrypted page, never on the public board.</p></div>')
    C_SALES_MONTH    = '<div class="card"><h2>Sales by month</h2><p class="note"><span class="pill">teal bars = units sold</span> &nbsp;<span class="pill" style="background:#FCF3DC;color:#9a6b00">gold line = revenue $</span></p><div id="salesMonth"></div></div>'
    C_SALES_PROVINCE = '<div class="card"><h2>Sales by province</h2><p class="note">All-time net revenue and paid units sold per province (wholesaler ship-to location).</p><div id="salesProvince"></div></div>'
    C_SALES_CUSTOMERS= ('<div class="card"><h2>Top distribution customers'
        '<span class="tip" tabindex="0">i<span class="tipc">'
        '<b>How Zimed reaches patients.</b> Most of our product does not go straight to pharmacies. '
        '<b>National wholesalers</b> (McKesson, Kohl &amp; Frisch) are middlemen that re-sell and deliver Zimed to thousands of independent and banner pharmacies we never see on the invoice. '
        '<b>Retail chains</b> (Shoppers, Jean Coutu, Familiprix) buy centrally and stock their own stores, so one listing decision reaches hundreds of locations. '
        '<b>Regional wholesalers</b> (LPG, Imperial, Unipharm, Nu-Quest) cover provinces the big two serve thinly, for example Nu-Quest is the main route into Newfoundland. '
        'Demand is created upstream by the eye doctors who prescribe and at the pharmacy counter, so with wholesalers the goal is staying listed and in stock, while chains and prescribers are where direct selling moves the needle.'
        '</span></span></h2>'
        '<p class="note">Distributors buying Zimed by net revenue (all-time). Bars are coloured by channel type; hover a name to see what each one is.</p>'
        '<div id="salesCustomers"></div></div>')
    C_SAMPLES_VS_SALES = ('<div class="card"><h2>Samples vs sales by province</h2><p class="note"><span class="pill">teal = sampled (free)</span> &nbsp;'
                          '<span class="pill" style="background:#FCF3DC;color:#9a6b00">gold = sold (paid)</span> &nbsp; '
                          'Directional — sample province is the clinic, sales province is the wholesaler depot.</p><div id="samplesVsSales"></div></div>')
    TABLE_CARD = ('<div class="card"><h2>All submissions</h2><p class="note">Click a header to sort; type to filter.</p>'
                  '<div class="toolbar"><input id="search" placeholder="Search clinic, doctor, address, referrer…"><select id="repf"></select><span class="note" id="rc"></span></div>'
                  '<div class="tin"><table><thead><tr id="thead"></tr></thead><tbody id="tbody"></tbody></table></div></div>')

    if is_public:
        # Public board: contest-first, no sales, no internal ops. Same charts, cleaned-up copy.
        body = (
            KPI_STRIP + C_CURRENT + C_PAST
            + _sec("Volume &amp; trend") + _cols(C_BYMONTH, C_CUMULATIVE) + _cols(C_AVGMONTH, C_REQMONTH)
            + _sec("Growth &amp; geography") + _cols(C_BYQUARTER, C_BYPROVINCE) + C_BYREP
            + _sec("Adoption, reach &amp; cadence") + _cols(C_ADOPTION, C_ORDERMIX) + _cols(C_REACH, C_NEWREP)
            + _sec("Competition outlook") + _cols(C_RUNRATE, C_PROJECTION)
        )
    else:
        # Management view: product-first narrative (revenue/volume → reach → geography → paid sales →
        # samples→sales bridge → the contest → internal ops → raw data).
        body = ""
        if has_sales:
            sales_tip = (f'<span class="tip" tabindex="0">i<span class="tipc">'
                '<b>Sales at a glance.</b> These are <b>paid</b> Zimed units bought by wholesalers, not free samples. '
                f'<b>Net revenue</b> is wholesaler invoice revenue minus returns, added up since the program began (cumulative, through {sales_through}). '
                'It is not a calendar-year or month figure. <b>Sold per sampled</b> compares total paid bottles to total free sample bottles placed, '
                'a rough read on how sampling is converting to sales.</span></span>')
            body += _sec(f'Sales at a glance <span class="mgmttag">confidential</span>{sales_tip}') + SALES_KPI_STRIP
        body += (
            _sec("Sampling volume &amp; trend") + KPI_STRIP
            + _cols(C_BYMONTH, C_CUMULATIVE) + _cols(C_REQMONTH, C_AVGMONTH) + C_BYQUARTER
            + _sec("Reach &amp; adoption") + _cols(C_REACH, C_ADOPTION) + _cols(C_ORDERMIX, C_NEWREP)
            + _sec("Geography") + _cols(C_BYPROVINCE, C_BYREGION)
        )
        if has_sales:
            body += (
                _sec('Paid sales <span class="mgmttag">confidential</span>') + SALES_NOTE
                + _cols(C_SALES_MONTH, C_SALES_PROVINCE) + C_SALES_CUSTOMERS
                + _sec("Samples &rarr; sales") + C_SAMPLES_VS_SALES
            )
        body += (
            _sec("The contest") + C_CURRENT + C_PAST + _cols(C_RUNRATE, C_PROJECTION) + C_MOMENTUM + C_BYREP
            + _sec('Operations <span class="mgmttag">internal</span>') + _cols(C_LAPSED, C_EFFICIENCY) + C_DQ
            + _sec("Full data") + TABLE_CARD
        )
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
    fonts = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&'
             'family=IBM+Plex+Mono:wght@500;600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">')
    org = ("Management dashboard" if not is_public else "Public board") + " · LUVO / Clarion"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{robots}
<title>{title}</title>{fonts}<style>{CSS}</style></head><body>
<header class="hero">
  <div class="herobar">{DROP_SVG}
    <div><h1>{title}</h1>
      <div class="eyebrow" style="margin-top:7px;color:var(--muted-dk)"><span class="drip"></span>{eyebrow_txt}</div></div>
    <div class="org">{org}<br>Auto-refreshed ~20 min</div>{img_tag}</div>
  <div class="marquee">
    <div class="mq live"><div class="lab">Contest period</div><div class="big" id="heroPeriod">—</div><div class="sub" id="heroPeriodSub"></div></div>
    <div class="mq"><div class="lab">Current leader</div><div class="big gold" id="heroLeader">—</div><div class="sub" id="heroLeaderSub"></div></div>
    <div class="mq"><div class="lab">Eye-drops placed (est.)</div><div class="big" id="heroDrops">—</div><div class="sub" id="heroDropsSub"></div></div>
  </div>
</header>
<div class="wrap">
{banner}
{body}
<div class="foot">{foot}</div></div>
<script>{script}</script></body></html>"""

GATE_JS = r"""
(function(){
  const u=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const ov=document.createElement("div");
  ov.style.cssText="position:fixed;inset:0;z-index:9999;background:radial-gradient(120% 130% at 15% -10%,#0C302C,#061A18 62%);display:grid;place-items:center;font-family:'Inter',-apple-system,Segoe UI,Roboto,sans-serif;color:#EAF7F4";
  ov.innerHTML='<div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:30px 30px 26px;max-width:370px;width:90%;text-align:center;box-shadow:0 22px 60px rgba(0,0,0,.45)">'
   +'<svg width="40" height="40" viewBox="0 0 24 24" style="filter:drop-shadow(0 3px 8px rgba(0,0,0,.4))"><path d="M12 2C12 2 4 11 4 16a8 8 0 0016 0c0-5-8-14-8-14z" fill="#fff"/><circle cx="12" cy="15.5" r="3.4" fill="#03BAB3"/></svg>'
   +'<div style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:#88ADA7;margin-top:12px">Confidential · Management view</div>'
   +'<h2 style="margin:6px 0 4px;font-family:\'Archivo\',sans-serif;font-weight:800;font-size:21px;color:#fff">Zimed PF — Management Dashboard</h2>'
   +'<p style="font-size:13px;color:#88ADA7;margin:0 0 16px">Enter the passphrase shared with you.</p>'
   +'<input id="pw" type="password" placeholder="Passphrase" autocomplete="off" style="width:100%;padding:11px 13px;border:1px solid rgba(255,255,255,.18);border-radius:9px;font-size:14px;box-sizing:border-box;background:rgba(0,0,0,.25);color:#fff"/>'
   +'<div id="gerr" style="color:#F0A07F;font-size:12px;height:16px;margin-top:7px"></div>'
   +'<button id="go" style="margin-top:8px;width:100%;padding:11px;border:0;border-radius:9px;background:linear-gradient(90deg,#06827B,#03BAB3);color:#062019;font-weight:800;font-size:14px;cursor:pointer;font-family:\'Archivo\',sans-serif">Unlock</button></div>';
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

def load_sales():
    """Confidential Zimed sales aggregates. From the SALES_DATA env (the Actions secret) in the
    cloud, or local sales_data.json for local builds. Returns None if absent/unparseable."""
    raw = (os.environ.get("SALES_DATA") or "").strip()
    if not raw:
        p = os.path.join(SELF, "sales_data.json")
        if os.path.exists(p): raw = open(p, encoding="utf-8").read()
    if not raw: return None
    try:
        s = json.loads(raw)
        return s if s.get("by_province") else None
    except Exception as e:
        print(f"WARN: SALES_DATA present but unparseable ({e}); sales charts skipped.", file=sys.stderr)
        return None

def sales_guard(html, sales):
    """Defence-in-depth: confirm no sales figures landed in a PUBLIC build."""
    if not sales: return []
    needles = [str(sales.get("totals", {}).get("revenue", "x"))]
    needles += [c.get("k", "") for c in sales.get("by_customer", [])[:3]]
    return [n for n in needles if n and n in html]

def main():
    args = set(sys.argv[1:])
    out_dir = sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else "."
    records = fetch_records()
    data = build(records)
    sales = load_sales()
    # management payload carries sales; the public payload NEVER does.
    mdata = {**data, "sales": sales} if sales else data

    if "--encrypted" in args:
        pw = (os.environ.get("MGMT_PASSPHRASE") or "").strip()
        if not pw: sys.exit("MGMT_PASSPHRASE env not set; refusing to build management page.")
        cipher = encrypt_payload(pw, {"DATA": mdata, "RECORDS": records})
        html = page(mdata, records, "encrypted", cipher)
        leak = pii_guard(html, records)
        if leak: sys.exit(f"ABORT: {len(leak)} PII values leaked into the encrypted file (should be impossible).")
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "index.html"), "w").write(html)
        print(f"records={len(records)} sales={'yes' if sales else 'no'} -> {out_dir}/index.html (ENCRYPTED management view)")
        return

    pub = page(data, None, "public")   # plain `data` — sales can never reach the public build
    leak = pii_guard(pub, records)
    if leak: sys.exit(f"ABORT: {len(leak)} PII values would leak into public file.")
    sleak = sales_guard(pub, sales)
    if sleak: sys.exit(f"ABORT: confidential sales data leaked into public file: {sleak}")
    if "--public-only" in args:
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "index.html"), "w").write(pub)
        print(f"records={len(records)} reached={data['team']['clinics']} bottles={data['team']['bottles']} PII=OK SALES=excluded -> {out_dir}/index.html")
    else:
        os.makedirs(os.path.join(OUT, "public"), exist_ok=True)
        os.makedirs(os.path.join(OUT, "private"), exist_ok=True)
        open(os.path.join(OUT, "public", "index.html"), "w").write(pub)
        open(os.path.join(OUT, "private", "index.html"), "w").write(page(mdata, records, "private"))
        print(f"records={len(records)} reached={data['team']['clinics']} bottles={data['team']['bottles']} sales={'yes' if sales else 'no'} -> out/public + out/private")

if __name__ == "__main__":
    main()
