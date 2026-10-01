#!/usr/bin/env python3
"""Refresh for Game Finder v2. Pulls scores, records, AP ranks and spreads from ESPN's core data feed
and writes data.json. Standard library only. Anything it cannot match to the app's own games is skipped."""
import json, re, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/"
ALIAS = {"Massachusetts": "UMass", "Connecticut": "UConn", "Hawai'i": "Hawaii", "San José State": "San Jose State",
         "App State": "Appalachian State"}
ERRORS = []
_cache = {}

def get(url):
    url = url.replace("http://", "https://")
    if url in _cache:
        return _cache[url]
    err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 game-finder"})
            with urllib.request.urlopen(req, timeout=30) as r:
                _cache[url] = json.load(r)
                return _cache[url]
        except Exception as e:
            err = e
            time.sleep(1.5 * (attempt + 1))
    ERRORS.append(url.replace(CORE, "")[:90] + " -> " + str(err)[:80])
    return None

def ref(o):
    return (o or {}).get("$ref")

def pmap(fn, items, workers=12):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))

def et_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")

def split_teams(ev):
    return [t.strip() for t in re.split(r"\s+(?:at|vs\.)\s+", re.sub(r"\(.*?\)", "", ev))]

def team_name(url, lg, names):
    tid = re.search(r"/teams/(\d+)", url).group(1)
    key = lg + ":" + tid
    if key not in names:
        t = get(url) or {}
        n = (t.get("name") if lg == "nfl" else t.get("location")) or ""
        if n:
            names[key] = ALIAS.get(n, n)
    return names.get(key, "")

def read_event(url, lg, names):
    ev = get(url)
    if not ev:
        return None
    try:
        c = ev["competitions"][0]
        comp = {x["homeAway"]: x for x in c["competitors"]}
        out = dict(lg=lg, date=et_date(ev["date"]), neutral=bool(c.get("neutralSite")), c=c, comp=comp,
                   tid={k: re.search(r"/teams/(\d+)", ref(v["team"])).group(1) for k, v in comp.items()},
                   turl={k: ref(v["team"]) for k, v in comp.items()})
        out["away"] = team_name(out["turl"]["away"], lg, names)
        out["home"] = team_name(out["turl"]["home"], lg, names)
        return out
    except Exception as e:
        ERRORS.append("event parse " + url[-40:] + ": " + str(e)[:60])
        return None

def detail(e):
    """Fetch score/record/status/odds for one matched event."""
    c, comp = e["c"], e["comp"]
    st = get(ref(c.get("status"))) or {}
    done = bool((st.get("type") or {}).get("completed"))
    e["completed"] = done
    e["ot"] = int((st.get("period") or 4) > 4)
    def sc(side):
        j = get(ref(comp[side].get("score"))) or {}
        return int(float(j.get("value") or 0))
    def rc(side):
        j = get(ref(comp[side].get("record"))) or {}
        for it in j.get("items", []):
            if it.get("name") == "overall" or it.get("type") == "total":
                return it.get("summary") or ""
        return ""
    e["ascore"], e["hscore"] = (sc("away"), sc("home")) if done else (0, 0)
    e["arec"], e["hrec"] = rc("away"), rc("home")
    e["spread"] = None
    if not done and c.get("odds"):
        o = None
        for _ in range(3):  # the odds feed sometimes comes back empty on the first try
            o = get(ref(c["odds"]) + ("&" if "?" in ref(c["odds"]) else "?") + "r=%d" % _) if ref(c["odds"]) else None
            if o and o.get("items"):
                break
        items = (o or {}).get("items") or []
        if items:
            d = (items[0].get("details") or "").strip()
            m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", d)
            if d.upper() in ("EVEN", "PK", "PICK"):
                e["spread"] = "PK"
            elif m:
                line = re.sub(r"\.0+$", "", "-" + m.group(1).lstrip("+-"))
                fav = "home" if (items[0].get("homeTeamOdds") or {}).get("favorite") else "away" if (items[0].get("awayTeamOdds") or {}).get("favorite") else None
                if fav:
                    e["spread"] = e[fav] + " " + line
    note = ""
    if e["neutral"]:
        addr = (e["c"].get("venue") or {}).get("address") or {}
        note = "Neutral site: " + ", ".join(x for x in (addr.get("city"), addr.get("state") or addr.get("country")) if x)
    e["note"] = note
    return e

def ap_ranks(names):
    """Latest AP poll as {school: rank}."""
    for wk in range(8, 0, -1):
        j = get(CORE + f"college-football/seasons/2026/types/2/weeks/{wk}/rankings/1")
        if j and j.get("ranks"):
            out = {}
            for r in j["ranks"]:
                n = team_name(ref(r["team"]), "ncaa", names)
                if n:
                    out[n] = r.get("current")
            return out, j.get("date", "")[:10]
    return {}, ""

def main():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    G = json.loads(re.search(r"^const G=(.*);$", html, re.M).group(1))
    games = {}
    for g in G:
        if g[2] in ("nfl", "ncaa"):
            t = split_teams(g[3])
            if len(t) == 2:
                games[(et_date(g[0]), frozenset(t))] = g
    today = datetime.now(ET).date()
    rng = f"{today - timedelta(days=1):%Y%m%d}-{today + timedelta(days=8):%Y%m%d}"

    npath, path = os.path.join(ROOT, "teams.json"), os.path.join(ROOT, "data.json")
    try:
        names = json.load(open(npath))
    except Exception:
        names = {}
    try:
        old = json.load(open(path))
    except Exception:
        old = {}

    evs = []
    for lg, slug in (("nfl", "nfl"), ("ncaa", "college-football")):
        lst = get(CORE + f"{slug}/events?dates={rng}&limit=400")
        urls = [ref(i) for i in (lst or {}).get("items", []) if ref(i)]
        evs += [x for x in pmap(lambda u: read_event(u, lg, names), urls) if x]
    matched = [e for e in evs if (e["date"], frozenset((e["away"], e["home"]))) in games]
    pmap(detail, matched)

    finals = old.get("finals", [])
    seen = {(f[0], frozenset((f[2], f[4]))) for f in finals}
    spreads, R = {}, {"nfl": {}, "ncaa": {}}
    for e in matched:
        key = (e["date"], frozenset((e["away"], e["home"])))
        g = games[key]
        for team, rec in ((e["away"], e["arec"]), (e["home"], e["hrec"])):
            if rec:
                R[e["lg"]][team] = [rec, None]
        if e["completed"]:
            if key not in seen:
                finals.append([e["date"], e["lg"], e["away"], e["ascore"], e["home"], e["hscore"], e["ot"], e["note"], e["arec"], e["hrec"]])
                seen.add(key)
        elif e["spread"]:
            spreads[g[0] + "|" + g[3]] = e["spread"]
    ranks, poll = ap_ranks(names)
    for team, v in R["ncaa"].items():
        v[1] = ranks.get(team)
    for team, rk in ranks.items():  # ranked teams that are not in the matched window keep their rank
        R["ncaa"].setdefault(team, None)
    R["ncaa"] = {k: v for k, v in R["ncaa"].items() if v}
    cutoff = (today - timedelta(days=45)).strftime("%Y-%m-%d")
    finals = [f for f in finals if f[0] >= cutoff]
    data = dict(updated=datetime.now(ET).strftime("%b %-d, %Y %-I:%M %p ET"), finals=finals, spreads=spreads, R=R,
                stats=dict(events=len(evs), matched=len(matched), finals=len(finals), spreads=len(spreads), poll=poll),
                errors=ERRORS[:12])
    json.dump(names, open(npath, "w"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    cmp = lambda d: json.dumps({k: v for k, v in d.items() if k not in ("updated", "stats", "errors")}, sort_keys=True)
    if old and old.get("stats", {}).get("events") and cmp(old) == cmp(data):
        print("no changes", data["stats"]); return
    json.dump(data, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    print("wrote data.json", data["stats"], ERRORS[:5])

if __name__ == "__main__":
    main()
