"""Bring your own match: readers for each format, in front of the cleaning pipeline (pipeline.py).

    Fault Lines JSON   {"teams": {"home": {"name", "players": [{"name", "role"}]}, "away": ...}, "events": [...]}
    Fault Lines CSV    t,team,type,player,player_role,recipient,recipient_role,x,y,end_x,end_y,success,pressure,xg,goal
                       (column names, delimiters, value spellings, scales and directions vary: the pipeline adapts)
    StatsBomb events   the JSON array of one match's events; first team listed = home

The canonical event: t (seconds from kick-off), team (home/away), type (pass/shot/own_goal), player, recipient,
x, y, end_x, end_y in the acting team's attacking frame (0-100: x=0 own goal line, y=0 its left touchline),
success, pressure, xg, goal. Roles: GK, RB, RCB, LCB, LB, CDM, RCM, LCM, RW, ST, LW.
"""
import csv
import io
import json
import uuid

import pipeline
from pipeline import ROLES, DataError, _key, clean_name  # noqa: F401  (DataError re-exported for callers)

MAX_BYTES, MAX_ROWS, MAX_PLAYERS = 8_000_000, 20_000, 40


def read_csv(text):
    """Rows from CSV text, whatever the delimiter (comma, semicolon, tab, pipe)."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return list(csv.DictReader(io.StringIO(text), dialect=dialect))


def lineup(teams):
    """Validated line-ups for a live feed: names cleaned, role spellings mapped, generic centre roles given a side."""
    lineups = {"id": uuid.uuid4().hex[:12]}
    for side in ("home", "away"):
        t = (teams or {}).get(side) or {}
        players = t.get("players") if isinstance(t, dict) else None
        if not isinstance(players, list) or not 1 <= len(players) <= MAX_PLAYERS:
            raise DataError(f"{side} team needs 1-{MAX_PLAYERS} players as [{{name, role}}]")
        roles, centre = {}, {"CB": iter(["RCB", "LCB"] * 20), "CM": iter(["RCM", "LCM"] * 20)}
        for p in players:
            name = clean_name(p.get("name") if isinstance(p, dict) else None)[0]
            role = ROLES.get(_key((p or {}).get("role", ""))) if isinstance(p, dict) else None
            if not name or not role:
                raise DataError(f"{side}: every player needs a name and a role (GK, RB, RCB, LCB, LB, CDM, RCM, LCM, RW, ST, LW)")
            roles[name] = next(centre[role]) if role in centre else role
        lineups[side] = {"name": clean_name(t.get("name"))[0] or side.title(), "players": list(roles), "roles": roles}
    if lineups["home"]["name"] == lineups["away"]["name"]:
        lineups["away"]["name"] += " (away)"
    return lineups


SB_ROLE = {  # StatsBomb position -> Fault Lines role
    "Goalkeeper": "GK", "Right Back": "RB", "Right Wing Back": "RB", "Left Back": "LB", "Left Wing Back": "LB",
    "Right Center Back": "RCB", "Center Back": "RCB", "Left Center Back": "LCB",
    "Right Defensive Midfield": "CDM", "Center Defensive Midfield": "CDM", "Left Defensive Midfield": "CDM",
    "Right Center Midfield": "RCM", "Center Midfield": "RCM", "Left Center Midfield": "LCM",
    "Right Midfield": "RW", "Left Midfield": "LW", "Right Wing": "RW", "Left Wing": "LW",
    "Right Attacking Midfield": "RCM", "Center Attacking Midfield": "RCM", "Left Attacking Midfield": "LCM",
    "Right Center Forward": "ST", "Striker": "ST", "Center Forward": "ST", "Left Center Forward": "ST",
    "Secondary Striker": "ST",
}


def from_statsbomb(evs):
    """StatsBomb event JSON -> (rows, teams). Locations are already in each team's attacking direction (120 x 80)."""
    if not isinstance(evs, list) or not evs or not isinstance(evs[0], dict) or "type" not in evs[0]:
        raise DataError("not a StatsBomb events array")
    nm = lambda d: (d or {}).get("name") if isinstance(d, dict) else None
    xi = [e for e in evs if nm(e.get("type")) == "Starting XI"]
    if len(xi) < 2:
        raise DataError("StatsBomb file needs both 'Starting XI' events (line-ups)")
    side = {nm(xi[0]["team"]): "home", nm(xi[1]["team"]): "away"}
    teams = {s: {"name": t, "players": []} for t, s in side.items()}
    role = {}
    for e in xi[:2]:
        for p in e.get("tactics", {}).get("lineup", []):
            role[nm(p.get("player"))] = SB_ROLE.get(nm(p.get("position")), "RCM")
            teams[side[nm(e["team"])]]["players"].append({"name": nm(p.get("player")), "role": role[nm(p.get("player"))]})
    for e in evs:
        if nm(e.get("type")) == "Substitution" and nm(e.get("team")) in side:
            on = nm((e.get("substitution") or {}).get("replacement"))
            if on and on not in role:
                role[on] = role.get(nm(e.get("player")), "RCM")  # the replacement takes the replaced player's role
                teams[side[nm(e["team"])]]["players"].append({"name": on, "role": role[on]})
    rows = []
    for e in evs:
        kind, team = nm(e.get("type")), side.get(nm(e.get("team")))
        if team is None or kind not in ("Pass", "Shot", "Own Goal For"):
            if team is not None and kind not in ("Starting XI", None):
                rows.append({"type": kind, "team": team, "t": e.get("minute", 0) * 60 + e.get("second", 0), "player": nm(e.get("player"))})
            continue
        loc = e.get("location") or [None, None]
        base = {"t": e.get("minute", 0) * 60 + e.get("second", 0), "period": e.get("period"), "team": team,
                "player": nm(e.get("player")), "x": loc[0] / 1.2 if loc[0] is not None else None,
                "y": loc[1] / 0.8 if loc[1] is not None else None, "pressure": bool(e.get("under_pressure"))}
        if kind == "Pass":
            p = e.get("pass") or {}
            end = p.get("end_location") or [None, None]
            ok = p.get("outcome") is None
            rows.append({**base, "type": "pass", "end_x": end[0] / 1.2 if end[0] is not None else None,
                         "end_y": end[1] / 0.8 if end[1] is not None else None, "success": ok,
                         "recipient": nm(p.get("recipient")) if ok else None})
        elif kind == "Shot":
            s = e.get("shot") or {}
            rows.append({**base, "type": "shot", "xg": s.get("statsbomb_xg"), "goal": nm(s.get("outcome")) == "Goal"})
        else:
            rows.append({**base, "type": "own_goal", "player": "Own goal"})
    return rows, teams


def parse(text, fmt="auto", home=None, away=None, scale="auto", direction="auto", encoding="utf-8"):
    """Any supported format -> pipeline -> (lineups, events, quarantine, report)."""
    if len(text) > MAX_BYTES:
        raise DataError("file too large (8 MB limit)")
    if fmt == "csv" or (fmt == "auto" and not text.lstrip("﻿ \n\r\t").startswith(("{", "["))):
        rows, teams, fmt = read_csv(text), None, "csv"
    else:
        try:
            obj = json.loads(text.lstrip("﻿"))
        except json.JSONDecodeError as e:
            raise DataError(f"not valid JSON ({e.msg} at line {e.lineno})") from None
        if fmt == "statsbomb" or (fmt == "auto" and isinstance(obj, list)):
            (rows, teams), fmt = from_statsbomb(obj), "statsbomb"
            scale, direction = "percent", "attacking"  # converted above, in StatsBomb's attacking frame
        elif isinstance(obj, dict) and isinstance(obj.get("events"), list):
            rows, teams, fmt = obj["events"], obj.get("teams"), "native"
        else:
            raise DataError("Fault Lines JSON needs 'events' (and ideally 'teams')")
    if len(rows) > MAX_ROWS:
        raise DataError(f"too many rows ({len(rows)}); the limit is {MAX_ROWS} (one match)")
    return pipeline.run(rows, teams, fmt=fmt, home=home, away=away, scale=scale, direction=direction,
                        input_text=text, encoding=encoding)


def to_native(events, teams):
    """A match in Fault Lines JSON (used for the downloadable sample)."""
    keys = ("t", "team", "type", "player", "recipient", "x", "y", "end_x", "end_y", "success", "pressure", "xg", "goal")
    return {"teams": {s: {"name": teams[s]["name"], "players": [{"name": p, "role": r} for p, r in teams[s]["roles"].items()]}
                      for s in ("home", "away")},
            "events": [{k: e[k] for k in keys} for e in events]}


def to_csv(events, teams):
    return pipeline.to_csv(events, {s: teams[s]["roles"] for s in ("home", "away")})
