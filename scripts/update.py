#!/usr/bin/env python3
"""Automatic data refresh for Game Finder. Runs on GitHub every 15 minutes (no one has to ask).
Pulls from ESPN's core data feed (standard library only) and writes data.json (+ rosters.json daily):
  * every run: finished scores + records, AP ranks, spreads for the next ~8 days
  * about every 3 hours: schedule sync (new games, time changes, TV channels) for NFL, college, NASCAR, F1
  * racing results (winner + top 10) as races finish
  * once a day: rosters for every NFL team (Fantasy)
Anything it cannot match is skipped, never guessed."""
import json, re, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = "https://sports.core.api.espn.com/v2/sports/"
FB = CORE + "football/leagues/"
RC = CORE + "racing/leagues/"
GF = CORE + "golf/leagues/"
ALIAS = {"Massachusetts": "UMass", "Connecticut": "UConn", "Hawai'i": "Hawaii", "San José State": "San Jose State",
         "App State": "Appalachian State"}
NET = {"USA Net": "USA Network", "CBSSN": "CBS Sports Network", "FS1": "FS1", "BTN": "Big Ten Network",
       "SECN": "SEC Network", "ACCN": "ACC Network", "truTV": "TNT / truTV", "TNT": "TNT / truTV", "NFLN": "NFL Network",
       "NFL Net": "NFL Network", "Prime": "Prime Video", "CW": "The CW"}
ERRORS = []
_cache = {}

def get(url, quiet=False):
    if not url:
        return None
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
            if "404" in str(e):
                break
            time.sleep(1.5 * (attempt + 1))
    if not quiet:
        ERRORS.append(url.replace(CORE, "")[:90] + " -> " + str(err)[:70])
    return None

def ref(o): return (o or {}).get("$ref")
def pmap(fn, items, workers=12):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(fn, items))
def parse_dt(iso): return datetime.fromisoformat(iso.replace("Z", "+00:00"))
def et_date(iso): return parse_dt(iso).astimezone(ET).strftime("%Y-%m-%d")
def et_iso(iso): return parse_dt(iso).astimezone(ET).isoformat(timespec="seconds")
def split_teams(ev): return [t.strip() for t in re.split(r"\s+(?:at|vs\.)\s+", re.sub(r"\(.*?\)", "", ev))]
def tid_of(url): return re.search(r"/teams/(\d+)", url).group(1)

def team_name(url, lg, names):
    key = lg + ":" + tid_of(url)
    if key not in names:
        t = get(url) or {}
        n = (t.get("name") if lg == "nfl" else t.get("location")) or ""
        if n:
            names[key] = ALIAS.get(n, n)
    return names.get(key, "")

def network(comp, lg=""):
    """Best TV network string for a competition."""
    b = get(ref(comp.get("broadcasts"))) if ref(comp.get("broadcasts")) else None
    items = sorted((b or {}).get("items", []), key=lambda x: x.get("priority", 99))
    tv = [i for i in items if (i.get("type") or {}).get("shortName") == "TV" and (i.get("market") or {}).get("type", "National") == "National"]
    pick = tv or [i for i in items if (i.get("type") or {}).get("shortName") != "TV"][:1]
    names = []
    for i in pick[:2]:
        n = i.get("station") or (i.get("media") or {}).get("shortName") or ""
        n = NET.get(n, n)
        if n and n not in names:
            names.append(n)
    s = " / ".join(names)
    if lg == "nfl" and s == "NBC":
        s = "NBC / Peacock"
    return s, bool(tv)

def read_event(url, lg, names):
    ev = get(url)
    if not ev:
        return None
    try:
        c = ev["competitions"][0]
        comp = {x["homeAway"]: x for x in c["competitors"]}
        out = dict(lg=lg, iso=et_iso(ev["date"]), date=et_date(ev["date"]), neutral=bool(c.get("neutralSite")), c=c, comp=comp,
                   turl={k: ref(v["team"]) for k, v in comp.items()})
        out["away"] = team_name(out["turl"]["away"], lg, names)
        out["home"] = team_name(out["turl"]["home"], lg, names)
        return out
    except Exception as e:
        ERRORS.append("event parse " + url[-40:] + ": " + str(e)[:60])
        return None

def detail(e):
    c, comp = e["c"], e["comp"]
    st = get(ref(c.get("status"))) or {}
    done = bool((st.get("type") or {}).get("completed"))
    e["completed"], e["ot"] = done, int((st.get("period") or 4) > 4)
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
    if not done and c.get("odds") and ref(c["odds"]):
        o = None
        for n in range(3):  # the odds feed sometimes comes back empty on the first try
            o = get(ref(c["odds"]) + ("&" if "?" in ref(c["odds"]) else "?") + "r=%d" % n)
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
        addr = (c.get("venue") or {}).get("address") or {}
        note = "Neutral site: " + ", ".join(x for x in (addr.get("city"), addr.get("state") or addr.get("country")) if x)
    e["note"] = note
    return e

def ap_ranks(names):
    for wk in range(9, 0, -1):
        j = get(CORE + f"football/leagues/college-football/seasons/2026/types/2/weeks/{wk}/rankings/1", quiet=True)
        if j and j.get("ranks"):
            return {team_name(ref(r["team"]), "ncaa", names): r.get("current") for r in j["ranks"] if team_name(ref(r["team"]), "ncaa", names)}, (j.get("date") or "")[:10]
    return {}, ""

# ---------------------------------------------------------------- schedule sync
NATIONAL_OK = {"ABC", "CBS", "NBC / Peacock", "NBC", "FOX", "ESPN", "ESPN2", "ESPNU", "FS1", "FS2", "Big Ten Network", "SEC Network",
               "ACC Network", "CBS Sports Network", "The CW", "TNT / truTV", "USA Network", "ESPN / ABC"}

def placeholder(iso):
    """ESPN uses 00:00 / 23:59 ET when the real kickoff time is not set yet."""
    return iso[11:16] in ("00:00", "23:59", "00:01")

def sync_football(lg, slug, days, names, static_rows):
    today = datetime.now(ET).date()
    rng = f"{today:%Y%m%d}-{today + timedelta(days=days):%Y%m%d}"
    lst = get(FB + f"{slug}/events?dates={rng}&limit=600")
    urls = [ref(i) for i in (lst or {}).get("items", []) if ref(i)]
    evs = [x for x in pmap(lambda u: read_event(u, lg, names), urls) if x]
    out = []
    by_teams = {}
    for g in static_rows:
        if g[2] == lg:
            by_teams.setdefault(frozenset(split_teams(g[3])), []).append(g)
    have_day = {(t, parse_dt(g[0]).date()) for g in static_rows if g[2] == lg for t in split_teams(g[3])}
    def one(e):
        net, is_tv = network(e["c"], lg)
        teams = frozenset((e["away"], e["home"]))
        if "" in teams:
            return None
        cands = by_teams.get(teams, [])
        cands = [g for g in cands if abs((parse_dt(g[0]).date() - parse_dt(e["iso"]).date()).days) <= 10]
        if cands:
            g = min(cands, key=lambda g: abs((parse_dt(g[0]) - parse_dt(e["iso"])).total_seconds()))
            ph = placeholder(e["iso"])
            new_iso = g[0] if ph or parse_dt(g[0]) == parse_dt(e["iso"]) else e["iso"]
            new_net = net if (net and net != "TBD" and not ph and (is_tv or not g[5]) and net != g[5]) else g[5]
            if new_iso != g[0] or new_net != g[5]:
                return [g[0] + "|" + g[3], new_iso, g[1], lg, g[3], g[4], new_net, ""]
            return None
        if placeholder(e["iso"]):
            return None
        # a game the app does not have yet (skip it if a team already has a game that day under another spelling)
        if any((t, parse_dt(e["iso"]).date()) in have_day for t in teams):
            return None
        if lg == "ncaa" and not (is_tv and net in NATIONAL_OK):
            return None
        sep = " vs. " if e["neutral"] else " at "
        ev = e["away"] + sep + e["home"]
        note = ""
        return ["", e["iso"], 195 if lg == "nfl" else 210, lg, ev, note, net, ""]
    return [r for r in pmap(one, evs) if r]

def sync_racing(names, static_rows, xup):
    today = datetime.now(ET).date()
    rng = f"{today - timedelta(days=1):%Y%m%d}-{today + timedelta(days=75):%Y%m%d}"
    out, races = [], []
    for slug, lg, sub in (("nascar-premier", "nascar", "cup"), ("nascar-secondary", "nascar", "oreilly"),
                          ("nascar-truck", "nascar", "truck"), ("f1", "f1", "")):
        lst = get(RC + f"{slug}/events?dates={rng}&limit=100")
        for it in (lst or {}).get("items", []):
            ev = get(ref(it))
            if not ev:
                continue
            comps = ev.get("competitions") or []
            comp = next((c for c in comps if (c.get("type") or {}).get("abbreviation") == "Race"), None) or (comps[0] if comps else None)
            if not comp or not comp.get("date"):
                continue
            iso = et_iso(comp["date"])
            if placeholder(iso):
                continue
            net, is_tv = network(comp)
            # find the matching row the app already has (same series, same ET day +/- 1)
            pool = [r for r in (static_rows if sub in ("cup", "") else xup) if (r[2] == lg if sub in ("cup", "") else r[1] == sub)]
            tgt = None
            for r in pool:
                rd = parse_dt(r[0] if sub in ("cup", "") else r[0]).date()
                if abs((rd - parse_dt(iso).date()).days) <= 1:
                    tgt = r
            if tgt:
                cur_iso, cur_net = tgt[0], (tgt[5] if sub in ("cup", "") else tgt[4])
                new_net = net if net and is_tv else cur_net
                if parse_dt(cur_iso) != parse_dt(iso) or new_net != cur_net:
                    event = tgt[3] if sub in ("cup", "") else tgt[2]
                    note = tgt[4] if sub in ("cup", "") else tgt[3]
                    out.append([cur_iso + "|" + event, iso, 210 if lg == "nascar" else 120, lg, event, note, new_net, "" if sub in ("cup", "") else sub])
            elif parse_dt(iso) > datetime.now(ET):
                venue = (get(ref((ev.get("venues") or [{}])[0])) or {}).get("fullName", "")
                out.append(["", iso, 210 if lg == "nascar" else 120, lg, ev.get("name", "Race"), venue, net, sub if lg == "nascar" else ""])
    return out

NASCAR_FEED = "https://cf.nascar.com/"
NASCAR_SERIES = {"cup": 1, "oreilly": 2, "truck": 3}

def lap_gap(n):
    return "+%d lap%s" % (n, "" if n == 1 else "s")

def nascar_gaps(sub, date):
    """{car number: gap string} for a finished NASCAR race, from NASCAR's own results feed (ESPN has no gaps for NASCAR)."""
    sid = NASCAR_SERIES.get(sub)
    if not sid:
        return {}
    rl = get(f"{NASCAR_FEED}cacher/2026/{sid}/race_list_basic.json", quiet=True)
    race = next((r for r in (rl if isinstance(rl, list) else []) if (r.get("race_date") or "")[:10] == date), None)
    if not race:
        return {}
    lf = get(f"{NASCAR_FEED}live/feeds/series_{sid}/{race['race_id']}/live_feed.json", quiet=True) or {}
    veh = sorted([v for v in lf.get("vehicles", []) if v.get("running_position")], key=lambda v: v["running_position"])
    if not veh:
        return {}
    lead_laps = veh[0].get("laps_completed") or 0
    out = {}
    for v in veh[1:]:
        down = lead_laps - (v.get("laps_completed") or 0)
        d = v.get("delta")
        if down > 0:
            out[str(v.get("vehicle_number"))] = lap_gap(down)
        elif isinstance(d, (int, float)) and d > 0:
            out[str(v.get("vehicle_number"))] = "+%.3f" % d
    return out

def f1_gap(c):
    """Gap to the winner for one F1 classification row, from ESPN's stats."""
    st = get(ref(c.get("statistics")), quiet=True) or {}
    stats = {x.get("name"): x for cat in (st.get("splits") or {}).get("categories", []) for x in cat.get("stats", [])}
    laps = (stats.get("behindLaps") or {}).get("value") or 0
    if laps > 0:
        return lap_gap(int(laps))
    t = stats.get("behindTime") or {}
    if (t.get("value") or 0) > 0:
        dv = str(t.get("displayValue") or "")
        return dv if dv.startswith("+") else "+" + dv
    return ""

def racing_results(names, known):
    """Winner + top 10 for races that have finished and are not already in the app."""
    today = datetime.now(ET).date()
    rng = f"{today - timedelta(days=14):%Y%m%d}-{today:%Y%m%d}"
    res = []
    for slug, lg, sub in (("nascar-premier", "nascar", "cup"), ("nascar-secondary", "nascar", "oreilly"),
                          ("nascar-truck", "nascar", "truck"), ("f1", "f1", "")):
        lst = get(RC + f"{slug}/events?dates={rng}&limit=100")
        for it in (lst or {}).get("items", []):
            ev = get(ref(it))
            if not ev:
                continue
            comps = ev.get("competitions") or []
            comp = next((c for c in comps if (c.get("type") or {}).get("abbreviation") == "Race"), None) or (comps[0] if comps else None)
            if not comp:
                continue
            st = get(ref(comp.get("status"))) or {}
            if not (st.get("type") or {}).get("completed"):
                continue
            d = et_date(comp["date"])
            if (lg, sub, d) in known:
                continue
            cc = comp.get("competitors")
            if isinstance(cc, list) and cc:
                items = cc
            else:
                cl = get(ref(cc)) if ref(cc) else get(f"{RC}{slug}/events/{ev.get('id')}/competitions/{comp.get('id')}/competitors?limit=60")
                items = (cl or {}).get("items", [])
            rows = [c for c in items if c.get("order")]
            rows.sort(key=lambda c: c["order"])
            top = []
            gaps = nascar_gaps(sub, d) if lg == "nascar" else {}
            for c in rows[:10]:
                a = get(ref(c.get("athlete"))) or {}
                v = c.get("vehicle") or {}
                if lg == "nascar":
                    team = ("#" + str(v.get("number")) if v.get("number") else "") + (" " + v["team"] if v.get("team") else "") + (" " + v["manufacturer"] if v.get("manufacturer") else "")
                else:
                    team = v.get("manufacturer") or v.get("team") or ""
                if c.get("order") == 1:
                    gap = ""
                elif lg == "nascar":
                    gap = gaps.get(str(v.get("number")), "")
                else:
                    gap = f1_gap(c)
                top.append([a.get("fullName") or a.get("displayName") or "?", team.strip(), gap])
            if len(top) < 3:
                continue
            venue = (get(ref((ev.get("venues") or [{}])[0])) or {}).get("fullName", "")
            res.append([d, lg, sub, ev.get("name", "Race"), venue, top])
    return res


# ---------------------------------------------------------------- golf (PGA Tour and LIV)
def golf_net(comp):
    b = get(ref(comp.get("broadcasts"))) if ref(comp.get("broadcasts")) else None
    out = []
    for i in sorted((b or {}).get("items", []), key=lambda x: x.get("priority", 99)):
        if (i.get("market") or {}).get("type", "National") != "National":
            continue
        n = i.get("station") or (i.get("media") or {}).get("shortName") or ""
        n = NET.get(n, n)
        if n and n not in out:
            out.append(n)
    out.sort(key=lambda n: n == "ESPN+")  # streaming last
    return " / ".join(out[:3])

def golf_top(slug, ev, comp):
    cc = comp.get("competitors")
    if isinstance(cc, list) and cc:
        items = cc
    else:
        cl = get(ref(cc)) if ref(cc) else get(f"{GF}{slug}/events/{ev.get('id')}/competitions/{comp.get('id')}/competitors?limit=200")
        items = (cl or {}).get("items", [])
    rows = sorted([c for c in items if c.get("order")], key=lambda c: c["order"])[:10]
    def one(c):
        a = get(ref(c.get("athlete"))) or {}
        sc = get(ref(c.get("score"))) or {}
        return [c["order"], a.get("fullName") or a.get("displayName") or "?", str(sc.get("displayValue") or sc.get("value") or "")]
    return pmap(one, rows, 6)

def sync_golf(old_rows):
    """PGA Tour and LIV tournaments: dates, TV, and (once play starts) the top 10 / winner."""
    today = datetime.now(ET).date()
    rng = f"{today - timedelta(days=8):%Y%m%d}-{today + timedelta(days=150):%Y%m%d}"
    prev = {(r[0], r[3]): r for r in old_rows}
    out = []
    for slug, tour in (("pga", "pga"),):
        lst = get(GF + f"{slug}/events?dates={rng}&limit=100")
        for it in (lst or {}).get("items", []):
            ev = get(ref(it))
            if not ev:
                continue
            comps = ev.get("competitions") or []
            comp = comps[0] if comps else None
            if not comp or not ev.get("date"):
                continue
            # ESPN dates golf by day (midnight UTC-ish): take the calendar date as given
            start = parse_dt(ev["date"]).astimezone(ET).date()
            end = parse_dt(ev.get("endDate") or ev["date"]).astimezone(ET).date()
            name = ev.get("name") or ev.get("shortName") or "Tournament"
            if name.startswith("TBD"):
                continue
            old = prev.get((tour, name))
            st = get(ref(comp.get("status")), quiet=True) or {}
            ty = st.get("type") or {}
            # ESPN also reports "post" after each round, so only trust it once the last day is done
            if today > end or (today == end and (ty.get("completed") or ty.get("state") == "post")):
                state = "post"
            elif today >= start:
                state = "in"
            else:
                state = "pre"
            net = golf_net(comp)
            venue = ""
            vref = (ev.get("venues") or [{}])[0]
            if ref(vref):
                venue = (get(ref(vref), quiet=True) or {}).get("fullName", "")
            top = []
            if state == "post" and old and old[6] == "post" and old[7]:
                top = old[7]
            elif state in ("in", "post"):
                top = golf_top(slug, ev, comp)
            if top and sum(1 for t in top if t[1] == "?") > len(top) // 2:
                top = []  # team events (Presidents Cup) have no per-player leaderboard here
            if state == "post" and old and old[6] == "post" and old[7] and not top:
                top = old[7]
            if state == "post" and not top and today <= end:
                state = "in" if today <= end else "post"
            out.append([tour, start.isoformat(), end.isoformat(), name, venue or (old[4] if old else ""), net or (old[5] if old else ""), state, top])
    return out

# ---------------------------------------------------------------- rosters
POS = {"OT": "OL", "G": "OL", "C": "OL", "OG": "OL", "T": "OL", "DE": "DL", "DT": "DL", "NT": "DL", "OLB": "LB", "ILB": "LB", "MLB": "LB",
       "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "SAF": "DB", "PK": "K", "H": "LS"}

def roster(team_id):
    base = FB + f"nfl/seasons/2026/teams/{team_id}/athletes?limit=200"
    lst = get(base)
    urls = [ref(i) for i in (lst or {}).get("items", []) if ref(i)]
    def ath(u):
        a = get(u, quiet=True) or {}
        n = a.get("fullName") or a.get("displayName")
        p = ((a.get("position") or {}).get("abbreviation")) or ""
        return [n, POS.get(p, p)] if n else None
    return [x for x in pmap(ath, urls, 6) if x]

def refresh_rosters(names):
    lst = get(FB + "nfl/seasons/2026/teams?limit=40")
    out = {}
    for t in (lst or {}).get("items", []):
        n = team_name(ref(t), "nfl", names)
        if not n:
            continue
        r = roster(tid_of(ref(t)))
        if len(r) >= 40:
            out[n] = r
    return out

# ---------------------------------------------------------------- main
def main():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    G = json.loads(re.search(r"^const G=(.*);$", html, re.M).group(1))
    XUP = json.loads(re.search(r"^const XUP=(.*);$", html, re.M).group(1))
    static_races = set()
    for blk, shape in (("RACES", "g"), ("XRES", "x")):
        m = re.search(rf"^const {blk}=(.*);$", html, re.M)
        for r in (json.loads(m.group(1)) if m else []):
            static_races.add(("nascar" if shape == "x" else r[1], r[1] if shape == "x" else ("cup" if r[1] == "nascar" else ""), r[0]))
    path, npath, rpath = (os.path.join(ROOT, f) for f in ("data.json", "teams.json", "rosters.json"))
    def load(p, d):
        try: return json.load(open(p))
        except Exception: return d
    names, old = load(npath, {}), load(path, {})
    now = datetime.now(ET)
    stamp = lambda: now.strftime("%b %-d, %Y %-I:%M %p ET")
    older = lambda key, hrs: (not old.get(key)) or (now - datetime.fromisoformat(old[key])).total_seconds() > hrs * 3600

    sched = old.get("sched", [])
    sched_at = old.get("schedAt", "")
    if older("schedAt", 2.9):
        base_rows = G
        new = []
        new += sync_football("nfl", "nfl", 100, names, base_rows)
        new += sync_football("ncaa", "college-football", 45, names, base_rows)
        new += sync_racing(names, base_rows, XUP)
        if new or not sched:
            sched = new
        sched_at = now.isoformat()

    # effective game list (static + schedule changes) used to match scores and spreads
    changed = {r[0]: r for r in sched if r[0]}
    eff = []
    for g in G:
        r = changed.get(g[0] + "|" + g[3])
        eff.append([r[1], g[1], g[2], g[3], g[4], r[6]] if r else g)
    eff += [[r[1], r[2], r[3], r[4], r[5], r[6]] for r in sched if not r[0]]
    games = {}
    for g in eff:
        if g[2] in ("nfl", "ncaa"):
            t = split_teams(g[3])
            if len(t) == 2:
                games[(et_date(g[0]), frozenset(t))] = g

    today = now.date()
    rng = f"{today - timedelta(days=1):%Y%m%d}-{today + timedelta(days=8):%Y%m%d}"
    evs = []
    for lg, slug in (("nfl", "nfl"), ("ncaa", "college-football")):
        lst = get(FB + f"{slug}/events?dates={rng}&limit=600")
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
    cutoff = (today - timedelta(days=45)).strftime("%Y-%m-%d")
    finals = [f for f in finals if f[0] >= cutoff]

    races = old.get("races", [])
    redo_from = (today - timedelta(days=14)).strftime("%Y-%m-%d")
    races = [r for r in races if not (r[0] >= redo_from and r[5] and len(r[5][0]) < 3)]  # recent races saved before gaps existed: fetch again
    known = set(static_races) | {(r[1], r[2], r[0]) for r in races}
    races += racing_results(names, known)
    races = [r for r in races if r[0] >= cutoff]

    cutoff_g = (today - timedelta(days=21)).isoformat()
    try:
        golf = [r for r in sync_golf(old.get("golf", [])) if r[2] >= cutoff_g]
    except Exception as e:  # golf must never break the rest of the update
        ERRORS.append("golf: " + repr(e)[:80])
        golf = old.get("golf", [])

    data = dict(updated=stamp(), schedAt=sched_at, sched=sched, finals=finals, spreads=spreads, R=R, races=races, golf=golf,
                stats=dict(events=len(evs), matched=len(matched), finals=len(finals), spreads=len(spreads), sched=len(sched),
                           races=len(races), golf=len(golf), poll=poll), errors=ERRORS[:12])
    json.dump(names, open(npath, "w"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data["updatedAt"] = now.isoformat()
    cmp = lambda d: json.dumps({k: v for k, v in d.items() if k not in ("updated", "updatedAt", "stats", "errors", "schedAt")}, sort_keys=True)
    fresh = old.get("updatedAt") and (now - datetime.fromisoformat(old["updatedAt"])).total_seconds() < 50 * 60
    # nothing changed: skip the commit, but still stamp the file about once an hour so the app's "Updated" time is honest
    if not (old and old.get("stats", {}).get("events") and cmp(old) == cmp(data) and old.get("schedAt") == data["schedAt"] and fresh):
        json.dump(data, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
        print("wrote data.json", data["stats"], ERRORS[:5])
    else:
        print("no changes", data["stats"])

    # rosters: once a day
    rosters = load(rpath, {})
    if not rosters or (now - datetime.fromisoformat(rosters.get("_at", "2000-01-01T00:00:00-05:00"))).total_seconds() > 20 * 3600:
        r = refresh_rosters(names)
        if len(r) >= 30:
            r["_at"] = now.isoformat()
            json.dump(r, open(rpath, "w"), ensure_ascii=False, separators=(",", ":"))
            print("wrote rosters.json", len(r) - 1, "teams")
        else:
            print("roster refresh incomplete, kept old file", len(r), ERRORS[-3:])
            json.dump(names, open(npath, "w"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

if __name__ == "__main__":
    main()
