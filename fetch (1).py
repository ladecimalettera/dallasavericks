"""Scarica ogni mattina i dati della lega ESPN FandAndonio + box score NBA della notte.
Scrive data/latest.json (letto dal report di Claude alle 7).
Variabili d'ambiente (GitHub Secrets): ESPN_S2, SWID, LEAGUE_ID. Opzionale: TEAM_NAME, SEASON.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from espn_api.basketball import League

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
SEASON = int(os.environ.get("SEASON", "2027"))  # stagione 2026-27 = 2027 per ESPN
TEAM_NAME = os.environ.get("TEAM_NAME", "Dallas Savericks")
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


def fp(s):
    """Punteggio della lega."""
    v = (s["PTS"] + 2 * s["FGM"] - s["FGA"] + s["FTM"] - s["FTA"] + s["3PM"]
         + s["REB"] + 1.5 * s["OREB"] + 2 * s["AST"] + 4 * s["STL"] + 4 * s["BLK"] - 2 * s["TO"])
    doubles = sum(1 for k in ("PTS", "REB", "AST", "STL", "BLK") if s[k] >= 10)
    if doubles >= 3:
        v += 10
    return round(v, 1)


def game_date():
    """Data USA della notte appena giocata."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    now_et = datetime.now(ZoneInfo("America/New_York"))
    d = now_et.date() if now_et.hour >= 12 else now_et.date() - timedelta(days=1)
    return d.strftime("%Y%m%d")


def split(x):
    try:
        a, b = x.split("-")
        return int(a), int(b)
    except Exception:
        return 0, 0


def num(x):
    try:
        return int(x)
    except Exception:
        return 0


def nba_boxscores(date):
    sb = requests.get(f"{SITE}/scoreboard", params={"dates": date}, timeout=30).json()
    games, lines = [], []
    for ev in sb.get("events", []):
        status = ev["status"]["type"]
        games.append({"id": ev["id"], "name": ev["shortName"], "final": status.get("completed", False),
                      "status": status.get("shortDetail")})
        summ = requests.get(f"{SITE}/summary", params={"event": ev["id"]}, timeout=30).json()
        for team in summ.get("boxscore", {}).get("players", []):
            abbr = team["team"]["abbreviation"]
            for block in team.get("statistics", []):
                labels = block.get("labels", [])
                for a in block.get("athletes", []):
                    if a.get("didNotPlay") or not a.get("stats"):
                        continue
                    raw = dict(zip(labels, a["stats"]))
                    fgm, fga = split(raw.get("FG", "0-0"))
                    tpm, _ = split(raw.get("3PT", "0-0"))
                    ftm, fta = split(raw.get("FT", "0-0"))
                    s = {"MIN": num(raw.get("MIN")), "PTS": num(raw.get("PTS")), "FGM": fgm, "FGA": fga,
                         "3PM": tpm, "FTM": ftm, "FTA": fta, "REB": num(raw.get("REB")),
                         "OREB": num(raw.get("OREB")), "AST": num(raw.get("AST")), "STL": num(raw.get("STL")),
                         "BLK": num(raw.get("BLK")), "TO": num(raw.get("TO")), "PF": num(raw.get("PF"))}
                    s["FP"] = fp(s)
                    lines.append({"id": int(a["athlete"]["id"]), "name": a["athlete"]["displayName"],
                                  "nba_team": abbr, "starter": a.get("starter", False),
                                  "game_final": status.get("completed", False), **s})
    return games, lines


def pinfo(p):
    st = p.stats
    return {"id": p.playerId, "name": p.name, "pos": p.position, "nba_team": p.proTeam,
            "slots": [x for x in p.eligibleSlots if x in ("PG", "SG", "SF", "PF", "C")],
            "injury": p.injuryStatus, "lineup_slot": p.lineupSlot,
            "avg_fp_espn": p.avg_points,
            "avg_last7": st.get(f"{SEASON}_last_7", {}).get("applied_avg"),
            "avg_last15": st.get(f"{SEASON}_last_15", {}).get("applied_avg"),
            "gp_proxy_total": p.total_points}


def wait_for_finals(date, deadline_rome="06:50"):
    """Aspetta che tutte le partite della notte siano finite, al massimo fino alle 6:50 ora italiana."""
    rome = ZoneInfo("Europe/Rome")
    h, m = map(int, deadline_rome.split(":"))
    while True:
        try:
            sb = requests.get(f"{SITE}/scoreboard", params={"dates": date}, timeout=30).json()
            open_games = [e["shortName"] for e in sb.get("events", [])
                          if not e["status"]["type"].get("completed", False)]
        except Exception as e:
            print("scoreboard non raggiungibile:", e)
            return
        now = datetime.now(rome)
        if not open_games:
            print("tutte le partite finite", now.strftime("%H:%M"))
            return
        if (now.hour, now.minute) >= (h, m):
            print("scadenza raggiunta, partite ancora in corso:", open_games)
            return
        print(now.strftime("%H:%M"), "in corso:", open_games, "- riprovo tra 2 minuti")
        time.sleep(120)


def main():
    if os.environ.get("SCHEDULED") == "1":
        # Due orari UTC nel workflow (ora solare/legale): parte solo quello che cade alle 6 in Italia.
        if datetime.now(ZoneInfo("Europe/Rome")).hour < 6:
            print("Prima delle 6 in Italia: salto (l'altro orario copre l'ora legale/solare).")
            return
    date = game_date()
    wait_for_finals(date)
    out = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "nba_date": date, "errors": []}

    owner = {}
    try:
        lg = League(league_id=LEAGUE_ID, year=SEASON,
                    espn_s2=os.environ["ESPN_S2"], swid=os.environ["SWID"])
        out["teams"] = []
        for t in lg.teams:
            out["teams"].append({"name": t.team_name, "record": f"{t.wins}-{t.losses}",
                                 "acquisitions": t.acquisitions,
                                 "roster": [pinfo(p) for p in t.roster]})
            for p in t.roster:
                owner[p.playerId] = t.team_name
        mine = [t for t in out["teams"] if t["name"].strip().lower() == TEAM_NAME.lower()]
        out["my_team"] = mine[0] if mine else None
        if not mine:
            out["errors"].append(f"Squadra '{TEAM_NAME}' non trovata: nomi = {[t['name'] for t in out['teams']]}")
        out["free_agents"] = [pinfo(p) for p in lg.free_agents(size=150)]
    except Exception as e:  # cookie scaduti, ESPN giù, ecc.
        out["errors"].append(f"ESPN fantasy: {type(e).__name__}: {e}")

    try:
        games, lines = nba_boxscores(date)
        for l in lines:
            l["fantasy_owner"] = owner.get(l["id"], "LIBERO" if owner else "sconosciuto")
        lines.sort(key=lambda x: -x["FP"])
        out["games"] = games
        out["last_night"] = lines
    except Exception as e:
        out["errors"].append(f"Box score NBA: {type(e).__name__}: {e}")

    os.makedirs("data", exist_ok=True)
    with open("data/latest.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"OK {date}: {len(out.get('games', []))} partite, {len(out.get('last_night', []))} righe, "
          f"{len(out.get('free_agents', []))} liberi, errori: {out['errors']}")


if __name__ == "__main__":
    main()
