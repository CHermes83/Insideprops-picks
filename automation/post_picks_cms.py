#!/usr/bin/env python3
"""
InsideProps — Daily CMS Picks Poster
======================================
Fetches today's game schedules from ESPN public API (no key needed),
runs the same analysis used on the GitHub site, and posts each pick
as a new item in the Webflow "Picks" CMS collection.

Run daily at 9 AM MT via scheduled task.
Config: webflow_config.json (same directory as this script)
"""

import os, sys, json, math, re, requests
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "webflow_config.json"
try:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    API_TOKEN      = cfg["WEBFLOW_API_TOKEN"]
    SITE_ID        = cfg["SITE_ID"]
    COLLECTION_ID  = cfg["PICKS_COLLECTION_ID"]
except FileNotFoundError:
    # Fallback to environment variables (for CI/CD use)
    API_TOKEN     = os.environ.get("WEBFLOW_API_TOKEN", "")
    SITE_ID       = os.environ.get("WEBFLOW_SITE_ID", "699dfc9b92d0ea1544725905")
    COLLECTION_ID = os.environ.get("PICKS_COLLECTION_ID", "699dfc459c6a1eeffe20b90d")

if not API_TOKEN or API_TOKEN == "PASTE_YOUR_TOKEN_HERE":
    print("❌  No Webflow API token found. Add it to webflow_config.json or set WEBFLOW_API_TOKEN env var.")
    sys.exit(1)

API_BASE = "https://api.webflow.com/v2"
WF_HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "accept": "application/json",
    "content-type": "application/json",
}
ESPN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InsideProps/1.0)",
    "Accept": "application/json",
}

# ─── Timezone / Date ──────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    MT = ZoneInfo("America/Denver")
except ImportError:
    try:
        import pytz
        MT = pytz.timezone("America/Denver")
    except ImportError:
        MT = None

NOW          = datetime.now(tz=MT) if MT else datetime.utcnow()
DATE_STR     = NOW.strftime("%Y%m%d")
DATE_DISPLAY = NOW.strftime("%B %-d, %Y")
DATE_ISO     = NOW.strftime("%Y-%m-%dT00:00:00.000Z")

print(f"\n{'='*60}")
print(f"  InsideProps CMS Pick Poster — {DATE_DISPLAY}")
print(f"{'='*60}\n")

# ─── ESPN helpers ─────────────────────────────────────────────────────────────
def espn_scoreboard(sport, league, date=DATE_STR):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"
    try:
        r = requests.get(url, params={"dates": date}, headers=ESPN_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        print(f"  [ESPN {sport}/{league}] {e}")
        return []

def espn_injuries(sport, league):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries"
    try:
        r = requests.get(url, headers=ESPN_HEADERS, timeout=10)
        r.raise_for_status()
        out = {}
        for item in r.json().get("items", []):
            tid = item.get("team", {}).get("id", "")
            if tid:
                out[tid] = [
                    {"name": p.get("athlete", {}).get("displayName", ""),
                     "status": p.get("status", ""),
                     "detail": p.get("shortComment", "")}
                    for p in item.get("injuries", [])
                ]
        return out
    except Exception as e:
        print(f"  [Injuries {sport}/{league}] {e}")
        return {}

def fetch_nba_leaders(team_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    result = {"pts": [], "reb": [], "ast": []}
    try:
        r = requests.get(url, headers=ESPN_HEADERS, timeout=8)
        r.raise_for_status()
        players = []
        for ath in r.json().get("athletes", []):
            name = ath.get("displayName", "")
            if not name:
                continue
            raw = ath.get("statistics", {})
            cats = raw.get("categories") or raw.get("splits", {}).get("categories", []) if isinstance(raw, dict) else []
            pts = reb = ast = 0.0
            for cat in (cats or []):
                for s in cat.get("stats", []):
                    nm, val = s.get("name", ""), float(s.get("value") or 0)
                    if nm in ("avgPoints", "avgPts", "pointsPerGame"):       pts = val
                    elif nm in ("avgRebounds", "avgReb", "reboundsPerGame"): reb = val
                    elif nm in ("avgAssists", "avgAst", "assistsPerGame"):   ast = val
            if pts > 3 or reb > 2 or ast > 1:
                players.append({"name": name, "pts": pts, "reb": reb, "ast": ast})
        players.sort(key=lambda x: x["pts"], reverse=True)
        result["pts"] = [{"name": p["name"], "value": p["pts"]} for p in players if p["pts"] > 3][:5]
        result["reb"] = sorted([{"name": p["name"], "value": p["reb"]} for p in players if p["reb"] > 2],
                                key=lambda x: x["value"], reverse=True)[:3]
        result["ast"] = sorted([{"name": p["name"], "value": p["ast"]} for p in players if p["ast"] > 1],
                                key=lambda x: x["value"], reverse=True)[:3]
    except Exception as e:
        print(f"  [Roster {team_id}] {e}")
    return result

# ─── Game parsing ─────────────────────────────────────────────────────────────
def parse_record(s):
    try:
        w, l = int(s.split("-")[0]), int(s.split("-")[1])
        t = w + l
        return w, l, round(w / t, 3) if t else 0.5
    except:
        return 0, 0, 0.5

def extract_game(event):
    comps = event.get("competitions", [])
    if not comps:
        return None
    comp = comps[0]
    home = away = None
    for c in comp.get("competitors", []):
        (home if c.get("homeAway") == "home" else away)
        if c.get("homeAway") == "home": home = c
        else: away = c
    if not home or not away:
        return None

    def recs(c):
        d = {}
        for rec in c.get("records", []):
            key = (rec.get("type") or rec.get("name", "")).lower()
            d[key] = rec.get("summary", "0-0")
        return d

    hr, ar = recs(home), recs(away)
    h_ov = hr.get("total") or hr.get("overall") or "0-0"
    a_ov = ar.get("total") or ar.get("overall") or "0-0"
    hw, hl, hpct  = parse_record(h_ov)
    aw, al, apct  = parse_record(a_ov)
    _, _, hhpct   = parse_record(hr.get("home", "0-0"))
    _, _, arpct   = parse_record(ar.get("road") or ar.get("away") or "0-0")
    _, _, ahhpct  = parse_record(ar.get("home", "0-0"))
    _, _, hrhpct  = parse_record(hr.get("road") or hr.get("away") or "0-0")

    odds = comp.get("odds", [{}])[0] if comp.get("odds") else {}
    ho, ao = odds.get("homeTeamOdds", {}), odds.get("awayTeamOdds", {})

    return {
        "id":    event.get("id", ""),
        "name":  event.get("name", ""),
        "short": event.get("shortName", ""),
        "home":  {
            "name": home.get("team", {}).get("displayName", "Home"),
            "abbr": home.get("team", {}).get("abbreviation", "HOM"),
            "id":   home.get("team", {}).get("id", ""),
            "overall": h_ov, "home_rec": hr.get("home", "--"),
            "w": hw, "l": hl, "pct": hpct, "home_pct": hhpct,
        },
        "away":  {
            "name": away.get("team", {}).get("displayName", "Away"),
            "abbr": away.get("team", {}).get("abbreviation", "AWY"),
            "id":   away.get("team", {}).get("id", ""),
            "overall": a_ov, "road_rec": ar.get("road") or ar.get("away") or "--",
            "w": aw, "l": al, "pct": apct, "road_pct": arpct,
        },
        "spread_str": odds.get("details", ""),
        "home_spread": ho.get("spreadOdds", 0) or 0,
        "away_spread": ao.get("spreadOdds", 0) or 0,
        "home_favored": ho.get("favorite", True),
        "total": odds.get("overUnder"),
    }

# ─── Pick generators (same analysis as GitHub site) ───────────────────────────
def confidence_label(score):
    if score >= 0.66: return "HIGH"
    if score >= 0.53: return "MEDIUM"
    return "LOW"

def injury_notes(injuries, team_id):
    notes = []
    for p in injuries.get(team_id, []):
        if any(x in p.get("status", "").lower() for x in ("out", "doubtful", "questionable")):
            notes.append(f"{p['name']} ({p.get('status','')})")
    return notes[:2]

def _half_line(val):
    return round(val * 2) / 2

def nba_spread_pick(g, injuries=None):
    home, away = g["home"], g["away"]
    injuries = injuries or {}
    h_score = home["home_pct"] + 0.03 + (home["pct"] - 0.5) * 0.18
    a_score = away["road_pct"]        + (away["pct"] - 0.5) * 0.18
    h_inj = injury_notes(injuries, home["id"])
    a_inj = injury_notes(injuries, away["id"])
    h_score -= len(h_inj) * 0.025
    a_score -= len(a_inj) * 0.025
    def fmt_spread(spread, spread_str):
        if not spread_str or spread == 0: return ""
        return f"-{abs(spread):.1f}" if spread < 0 else f"+{abs(spread):.1f}"
    if h_score >= a_score:
        spread_disp = fmt_spread(g["home_spread"], g["spread_str"])
        pick_str = f"{home['abbr']} {spread_disp}".strip() if spread_disp else f"{home['name']} ML"
        inj_note = f" Note: {', '.join(a_inj)} are banged up for {away['abbr']}." if a_inj else ""
        analysis = (f"{home['name']} is {home['overall']} overall and {home['home_rec']} at home. "
                    f"{away['name']} comes in {away['road_rec']} on the road.{inj_note}")
        return pick_str, "SPREAD", confidence_label(h_score), analysis
    else:
        spread_disp = fmt_spread(g["away_spread"], g["spread_str"])
        pick_str = f"{away['abbr']} {spread_disp}".strip() if spread_disp else f"{away['name']} ML"
        inj_note = f" Note: {', '.join(h_inj)} are limited for {home['abbr']}." if h_inj else ""
        analysis = (f"{away['name']} at {away['overall']} is the sharper club. "
                    f"They're {away['road_rec']} on the road.{inj_note}")
        return pick_str, "SPREAD", confidence_label(a_score), analysis

def nba_total_pick(g):
    home, away = g["home"], g["away"]
    total = g["total"] or 223.0
    if home["pct"] > 0.55 and away["pct"] > 0.55:
        return (f"OVER {total}", "TOTAL", "HIGH",
                f"Two winning teams — {home['name']} at {home['overall']}, {away['name']} at {away['overall']}. "
                f"High-powered offenses keep the pace up. {total} total looks conservative.")
    elif home["pct"] < 0.40 or away["pct"] < 0.40:
        return (f"UNDER {total}", "TOTAL", "MEDIUM",
                f"One team struggles to generate offense. Stronger defensive side controls pace. Under {total}.")
    else:
        return (f"OVER {total}", "TOTAL", "MEDIUM",
                f"Even matchup. Competitive games push late — free throws and live-ball buckets inflate the total. OVER {total}.")

def nba_player_props(g, home_leaders=None, away_leaders=None):
    home, away = g["home"], g["away"]
    hl = home_leaders or {"pts": [], "reb": [], "ast": []}
    al = away_leaders or {"pts": [], "reb": [], "ast": []}
    fav  = home if g["home_favored"] else away
    dog  = away if g["home_favored"] else home
    fl   = hl if g["home_favored"] else al
    dl   = al if g["home_favored"] else hl
    props = []

    def star_info(leaders_pts, team):
        if leaders_pts and leaders_pts[0]["value"] > 8:
            p = leaders_pts[0]
            return p["name"], p["value"], _half_line(p["value"] * 0.88)
        est = 17 + team["pct"] * 13.3
        return f"{team['abbr']} Star", round(est, 1), _half_line(est * 0.88)

    def reb_info(leaders_reb, team):
        if leaders_reb and leaders_reb[0]["value"] > 3:
            p = leaders_reb[0]
            return p["name"], p["value"], _half_line(p["value"] * 0.87)
        est = 7.5 + (team["pct"] - 0.5) * 3
        return f"{team['abbr']} Big", round(est, 1), _half_line(est * 0.87)

    def ast_info(leaders_ast, team):
        if leaders_ast and leaders_ast[0]["value"] > 2:
            p = leaders_ast[0]
            return p["name"], p["value"], _half_line(p["value"] * 0.87)
        est = 4.5 + (team["pct"] - 0.5) * 4
        return f"{team['abbr']} PG", round(est, 1), _half_line(est * 0.87)

    fav_star, fav_avg, fav_line = star_info(fl["pts"], fav)
    props.append((f"{fav_star} OVER {fav_line} PTS", "PLAYER PROP",
                  "HIGH" if fav["pct"] > 0.58 else "MEDIUM",
                  f"{fav_star} scoring ~{fav_avg:.1f} PPG. Facing {dog['abbr']} — {fav_line} line is reachable. OVER."))

    dog_star, dog_avg, dog_line = star_info(dl["pts"], dog)
    props.append((f"{dog_star} OVER {dog_line} PTS", "PLAYER PROP", "MEDIUM",
                  f"{dog_star} averages {dog_avg:.1f} PPG. In competitive matchups their scorer sees heavier usage. OVER {dog_line}."))

    reb_name, reb_avg, reb_line = reb_info(fl["reb"], fav)
    props.append((f"{reb_name} OVER {reb_line} REB", "PLAYER PROP", "MEDIUM",
                  f"{reb_name} averages {reb_avg:.1f} REB. Line at {reb_line} is below season pace — lean OVER."))

    ast_name, ast_avg, ast_line = ast_info(fl["ast"], fav)
    props.append((f"{ast_name} OVER {ast_line} AST", "PLAYER PROP", "MEDIUM",
                  f"{ast_name} distributing at {ast_avg:.1f} APG. Heavy pick-and-roll usage expected. OVER {ast_line}."))

    return props[:4]

def nhl_picks(g, injuries=None):
    home, away = g["home"], g["away"]
    injuries = injuries or {}
    h_inj = injury_notes(injuries, home["id"])
    a_inj = injury_notes(injuries, away["id"])
    h_score = home["home_pct"] + 0.055 + (home["pct"] - 0.5) * 0.14 - len(h_inj) * 0.03
    a_score = away["road_pct"]         + (away["pct"] - 0.5) * 0.14 - len(a_inj) * 0.03
    picks = []

    diff = abs(h_score - a_score)
    if diff < 0.05:
        dog = away if h_score >= a_score else home
        picks.append((f"{dog['abbr']} +1.5", "MONEYLINE", "MEDIUM",
                      f"Dead-even matchup. Take the puck line underdog — most NHL games stay within a goal."))
    elif h_score > a_score:
        inj = f" {away['abbr']} missing {', '.join(a_inj)}." if a_inj else ""
        picks.append((f"{home['name']} ML", "MONEYLINE", confidence_label(h_score),
                      f"{home['name']} is {home['overall']}, {home['home_rec']} at home. Home ice is real.{inj}"))
    else:
        inj = f" {home['abbr']} missing {', '.join(h_inj)}." if h_inj else ""
        picks.append((f"{away['name']} ML", "MONEYLINE", confidence_label(a_score),
                      f"{away['name']} at {away['overall']} is the better team on the road.{inj}"))

    total = g["total"] or 5.5
    if home["pct"] > 0.55 and away["pct"] > 0.55:
        picks.append((f"OVER {total}", "TOTAL", "HIGH",
                      f"Two quality clubs — power plays active, both push pace. OVER {total}."))
    else:
        picks.append((f"UNDER {total}", "TOTAL", "MEDIUM",
                      f"At least one team wins with defense. Tight checking, UNDER {total}."))
    return picks

def mlb_picks(g, injuries=None):
    home, away = g["home"], g["away"]
    injuries = injuries or {}
    h_inj = injury_notes(injuries, home["id"])
    a_inj = injury_notes(injuries, away["id"])
    h_score = home["home_pct"] + 0.03 + (home["pct"] - 0.5) * 0.10 - len(h_inj) * 0.025
    a_score = away["road_pct"]         + (away["pct"] - 0.5) * 0.10 - len(a_inj) * 0.025
    picks = []

    diff = abs(h_score - a_score)
    if diff < 0.03:
        picks.append((f"{home['name']} ML", "MONEYLINE", "LOW",
                      f"Even matchup. Home cooking gives {home['name']} the small edge. ML for value."))
    elif h_score >= a_score:
        inj = f" {away['abbr']} shorthanded: {', '.join(a_inj)}." if a_inj else ""
        picks.append((f"{home['name']} ML", "MONEYLINE", confidence_label(h_score),
                      f"{home['name']} is {home['overall']} and {home['home_rec']} at home. {away['name']} at {away['road_rec']} on road.{inj}"))
    else:
        inj = f" {home['abbr']} dealing with injuries: {', '.join(h_inj)}." if h_inj else ""
        picks.append((f"{away['name']} ML", "MONEYLINE", confidence_label(a_score),
                      f"{away['name']} at {away['overall']} has been the sharper club. {away['road_rec']} on the road.{inj}"))

    run_total = g["total"] or 8.5
    if home["pct"] > 0.56 and away["pct"] > 0.56:
        picks.append((f"OVER {run_total}", "TOTAL", "HIGH",
                      f"Two winning clubs with active lineups. Bullpen usage inflates run totals. OVER {run_total}."))
    elif home["pct"] < 0.40 or away["pct"] < 0.40:
        picks.append((f"UNDER {run_total}", "TOTAL", "MEDIUM",
                      f"One offense has been inconsistent. Better pitching limits crooked numbers. UNDER {run_total}."))
    else:
        if home["pct"] > away["pct"]:
            picks.append((f"OVER {run_total}", "TOTAL", "MEDIUM",
                          f"{home['name']}'s lineup producing at home. Friendly factors push scoring up. OVER {run_total}."))
        else:
            picks.append((f"UNDER {run_total}", "TOTAL", "MEDIUM",
                          f"Pitching matchup favors low scoring. UNDER {run_total} when neither offense dominates."))
    return picks

def ncaab_pick(g):
    home, away = g["home"], g["away"]
    total = g["total"] or 146.0
    fav, dog = (home, away) if home["pct"] >= away["pct"] else (away, home)
    fav_home = (fav is home)
    diff = fav["pct"] - dog["pct"]
    if diff > 0.18:
        spread = g["home_spread"] if fav_home else g["away_spread"]
        s = (f"-{abs(spread):.1f}" if spread < 0 else f"+{abs(spread):.1f}") if g["spread_str"] and spread else "-7.5"
        return [(f"{fav['abbr']} {s}", "SPREAD", "HIGH",
                 f"{fav['name']} ({fav['overall']}) has been one of the sharper teams. {dog['name']} ({dog['overall']}) is outmanned.")]
    elif diff < 0.06:
        return [(f"OVER {total}", "TOTAL", "MEDIUM",
                 f"Pick-em in college ball. In close matchups, late fouling opens scoring. OVER {total}.")]
    else:
        spread = g["away_spread"] if fav_home else g["home_spread"]
        s = (f"+{abs(spread):.1f}") if g["spread_str"] and spread else "+8.5"
        return [(f"{dog['abbr']} {s}", "SPREAD", "MEDIUM",
                 f"{dog['name']} ({dog['overall']}) is catching a number. Value on the underdog covering.")]

# ─── Webflow CMS helpers ──────────────────────────────────────────────────────
def make_slug(text):
    """Convert pick text to a URL-safe slug."""
    slug = text.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug.strip())
    slug = re.sub(r'-+', '-', slug)
    return slug[:80]

def post_pick_to_cms(pick_name, sport, line_text, tier):
    """
    Create a single pick item in the Webflow CMS Picks collection.
    Result is intentionally left blank (to be filled in after game).
    Returns True on success.
    """
    slug = make_slug(f"{pick_name}-{DATE_STR}")

    payload = {
        "fieldData": {
            "name":      pick_name,
            "slug":      slug,
            "pick-date": DATE_ISO,
            "sport":     sport,
            "line":      line_text,
            "tier":      tier.lower(),      # "free" or "premium"
            # result is intentionally omitted (pending)
        },
        "isDraft": False,
    }

    url = f"{API_BASE}/collections/{COLLECTION_ID}/items"
    try:
        r = requests.post(url, headers=WF_HEADERS, json=payload, timeout=15)
        if r.status_code in (200, 201):
            item = r.json()
            print(f"  ✅  [{tier.upper()}] {pick_name}")
            return True
        else:
            print(f"  ❌  Failed to post '{pick_name}': {r.status_code} — {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌  Exception posting '{pick_name}': {e}")
        return False

def publish_collection():
    """Publish all live items in the Picks collection."""
    url = f"{API_BASE}/sites/{SITE_ID}/publish"
    try:
        r = requests.post(url, headers=WF_HEADERS,
                          json={"publishToWebflowSubdomain": True, "customDomains": ["www.insideprops.com"]},
                          timeout=20)
        if r.status_code in (200, 202):
            print("\n  🌐  Site published successfully!")
        else:
            print(f"\n  ⚠️  Publish returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"\n  ⚠️  Publish exception: {e}")

# ─── Main: generate & post picks ──────────────────────────────────────────────
def main():
    posted = 0
    failed = 0

    # ── NBA ───────────────────────────────────────────────────────────────────
    print("📊  Fetching NBA games...")
    nba_events  = espn_scoreboard("basketball", "nba")
    nba_injuries = espn_injuries("basketball", "nba")
    nba_games = [g for g in [extract_game(e) for e in nba_events] if g]
    print(f"    Found {len(nba_games)} NBA game(s).")

    for g in nba_games[:3]:  # cap at 3 games to avoid spam
        home, away = g["home"], g["away"]
        matchup = g["short"] or f"{away['abbr']} @ {home['abbr']}"

        # Spread (Free)
        pick, ptype, conf, analysis = nba_spread_pick(g, nba_injuries)
        name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
        line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
        if post_pick_to_cms(name, "NBA", line_text, "Free"): posted += 1
        else: failed += 1

        # Total (Free)
        pick, ptype, conf, analysis = nba_total_pick(g)
        name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
        line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
        if post_pick_to_cms(name, "NBA", line_text, "Free"): posted += 1
        else: failed += 1

        # Player props (Premium)
        try:
            home_leaders = fetch_nba_leaders(home["id"])
            away_leaders = fetch_nba_leaders(away["id"])
        except:
            home_leaders = away_leaders = {"pts": [], "reb": [], "ast": []}

        for pick, ptype, conf, analysis in nba_player_props(g, home_leaders, away_leaders):
            name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
            line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
            if post_pick_to_cms(name, "NBA", line_text, "Premium"): posted += 1
            else: failed += 1

    # ── NHL ───────────────────────────────────────────────────────────────────
    print("\n🏒  Fetching NHL games...")
    nhl_events   = espn_scoreboard("hockey", "nhl")
    nhl_injuries = espn_injuries("hockey", "nhl")
    nhl_games    = [g for g in [extract_game(e) for e in nhl_events] if g]
    print(f"    Found {len(nhl_games)} NHL game(s).")

    for g in nhl_games[:4]:
        home, away = g["home"], g["away"]
        matchup = g["short"] or f"{away['abbr']} @ {home['abbr']}"
        for pick, ptype, conf, analysis in nhl_picks(g, nhl_injuries):
            name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
            line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
            if post_pick_to_cms(name, "NHL", line_text, "Free"): posted += 1
            else: failed += 1

    # ── MLB ───────────────────────────────────────────────────────────────────
    print("\n⚾  Fetching MLB games...")
    mlb_events   = espn_scoreboard("baseball", "mlb")
    mlb_injuries = espn_injuries("baseball", "mlb")
    mlb_games    = [g for g in [extract_game(e) for e in mlb_events] if g]
    print(f"    Found {len(mlb_games)} MLB game(s).")

    for g in mlb_games[:4]:
        home, away = g["home"], g["away"]
        matchup = g["short"] or f"{away['abbr']} @ {home['abbr']}"
        for pick, ptype, conf, analysis in mlb_picks(g, mlb_injuries):
            name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
            line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
            if post_pick_to_cms(name, "MLB", line_text, "Free"): posted += 1
            else: failed += 1

    # ── NCAAB (only in season: Nov–Apr) ──────────────────────────────────────
    if NOW.month in (11, 12, 1, 2, 3, 4):
        print("\n🏀  Fetching NCAAB games...")
        ncaab_events = espn_scoreboard("basketball", "mens-college-basketball")
        ncaab_games  = [g for g in [extract_game(e) for e in ncaab_events] if g]
        print(f"    Found {len(ncaab_games)} NCAAB game(s).")
        for g in ncaab_games[:3]:
            home, away = g["home"], g["away"]
            matchup = g["short"] or f"{away['abbr']} @ {home['abbr']}"
            for pick, ptype, conf, analysis in ncaab_pick(g):
                name = f"{pick} ({matchup}) — {DATE_DISPLAY}"
                line_text = f"{pick}\n\nType: {ptype} | Confidence: {conf}\n\n{analysis}"
                if post_pick_to_cms(name, "NCAAB", line_text, "Free"): posted += 1
                else: failed += 1

    # ── Summary & Publish ─────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Posted: {posted} picks  |  Failed: {failed}")

    if posted > 0:
        print("\n  Publishing site to Webflow...")
        publish_collection()
    else:
        print("\n  ⚠️  No picks posted — skipping publish.")

    print(f"\n  Done! InsideProps picks live for {DATE_DISPLAY}.\n")

if __name__ == "__main__":
    main()
