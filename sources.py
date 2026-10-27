"""Bring your own match: adapters from a user's data to the event model every analysis uses.

    Fault Lines JSON   {"teams": {"home": {"name", "players": [{"name", "role"}]}, "away": ...}, "events": [...]}
    Fault Lines CSV    t,team,type,player,player_role,recipient,recipient_role,x,y,end_x,end_y,success,pressure,xg,goal
    StatsBomb events   the JSON array of one match's events (open-data / API format); first team listed = home

Event fields: t (seconds from kick-off), team (home/away), type (pass/shot/own_goal), player, recipient (completed
passes), x, y, end_x, end_y in the acting team's attacking frame (0-100: x=0 own goal line, y=0 its left touchline),
success, pressure, xg (shots), goal. Roles: GK, RB, RCB, LCB, LB, CDM, RCM, LCM, RW, ST, LW.

User data is untrusted: everything is validated here (names, roles, sizes, coordinates) and nothing is stored on disk.
"""
import csv
import io
import json
import math
import re
import uuid

from match import BASE, M_X, M_Y

ROLE_CODES = set(BASE)
NAME = re.compile(r"^[\w .'’-]{1,60}$")  # letters (any script), digits, spaces, . ' - : no markup
MAX_EVENTS, MAX_PLAYERS, MAX_T = 8000, 30, 130 * 60
TYPES = {"pass", "shot", "own_goal"}


class DataError(ValueError):
    """A problem with the user's data, worded for the user."""


def _name(v, what):
    v = " ".join(str(v or "").split())
    if not NAME.match(v):
        raise DataError(f"{what} {v[:40]!r} is not a valid name (letters, digits, spaces, . ' - ; up to 60 characters)")
    return v


def _num(v, what, lo, hi, default=None):
    if v in (None, ""):
        if default is None:
            raise DataError(f"missing {what}")
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DataError(f"{what} must be a number, got {str(v)[:20]!r}") from None
    if math.isnan(f):
        raise DataError(f"{what} is not a number")
    return min(hi, max(lo, f))


def _bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def lineup(teams):
    """Validated line-ups: {"id", side: {"name", "players", "roles"}}. teams: {side: {"name", "players": [{"name","role"}]}}."""
    lineups = {"id": uuid.uuid4().hex[:12]}
    for side in ("home", "away"):
        t = (teams or {}).get(side) or {}
        players = t.get("players") or []
        if not isinstance(players, list) or not 1 <= len(players) <= MAX_PLAYERS:
            raise DataError(f"{side} team needs 1-{MAX_PLAYERS} players")
        roles = {}
        for p in players:
            if not isinstance(p, dict):
                raise DataError(f"{side} players must be objects with name and role")
            name, role = _name(p.get("name"), "player name"), str(p.get("role", "")).upper()
            if role not in ROLE_CODES:
                raise DataError(f"{name}: role {role!r} is not one of {', '.join(sorted(ROLE_CODES))}")
            roles[name] = role
        lineups[side] = {"name": _name(t.get("name") or side.title(), "team name"), "players": list(roles), "roles": roles}
    if lineups["home"]["name"] == lineups["away"]["name"]:
        lineups["away"]["name"] += " (away)"
    return lineups


def event(r, lineups, warnings):
    """One validated event in the internal model."""
    if not isinstance(r, dict):
        raise DataError("each event must be an object")
    side, kind = str(r.get("team", "")).lower(), str(r.get("type", "")).lower()
    if side not in ("home", "away") or kind not in TYPES:
        raise DataError("team must be home/away and type pass/shot/own_goal")
    roles = lineups[side]["roles"]
    player = _name(r.get("player") or "Own goal", "player") if kind == "own_goal" else _name(r.get("player"), "player")
    if kind != "own_goal" and player not in roles:
        raise DataError(f"{player} is not in the {side} line-up")
    t = _num(r.get("t"), "t", 0, MAX_T)
    x, y = _num(r.get("x"), "x", 0, 100, 50.0), _num(r.get("y"), "y", 0, 100, 50.0)
    e = dict(t=round(t, 1), minute=int(t // 60), team=side, type=kind, player=player, recipient=None,
             x=round(x, 1), y=round(y, 1), end_x=100.0, end_y=50.0, success=False,
             pressure=_bool(r.get("pressure", False)), xg=None, goal=False)
    if kind == "pass":
        e["end_x"], e["end_y"] = round(_num(r.get("end_x"), "end_x", 0, 100), 1), round(_num(r.get("end_y"), "end_y", 0, 100), 1)
        e["success"] = _bool(r.get("success"))
        if e["success"]:
            rec = _name(r.get("recipient"), "recipient")
            if rec not in roles:
                raise DataError(f"recipient {rec} is not in the {side} line-up")
            e["recipient"] = rec if rec != player else None
            e["success"] = e["recipient"] is not None
    elif kind == "shot":
        xg = r.get("xg")
        if xg in (None, ""):  # no provider xG: distance-only estimate, flagged to the user
            xg = round(min(0.7, 0.7 * math.exp(-math.hypot((100 - x) * M_X, (50 - y) * M_Y) / 11)), 3)
            note = "some shots had no xG: estimated from distance only (provider xG is better)"
            if note not in warnings:
                warnings.append(note)
        e["xg"] = round(_num(xg, "xg", 0, 1), 3)
        e["goal"] = e["success"] = _bool(r.get("goal"))
    else:
        e["goal"], e["xg"] = True, 0.0
    return e


def finish(teams, raw_events, warnings=()):
    """Validate a whole match: (lineups, events, summary)."""
    warnings = list(warnings)
    lineups = lineup(teams)
    if not isinstance(raw_events, list) or not raw_events:
        raise DataError("no events found")
    if len(raw_events) > MAX_EVENTS:
        raise DataError(f"too many events ({len(raw_events)}); the limit is {MAX_EVENTS} (one match)")
    events = []
    for i, r in enumerate(raw_events):
        try:
            events.append(event(r, lineups, warnings))
        except DataError as err:
            raise DataError(f"event {i + 1}: {err}") from None
    events.sort(key=lambda e: e["t"])
    if not any(e["type"] == "pass" and e["success"] for e in events):
        raise DataError("no completed passes: the passing network needs them")
    summary = {"events": len(events), "passes": sum(e["type"] == "pass" for e in events),
               "shots": sum(e["type"] == "shot" for e in events), "minutes": int(events[-1]["t"] // 60) + 1,
               "score": {s: sum(e["goal"] and e["team"] == s for e in events) for s in ("home", "away")},
               "warnings": warnings}
    return lineups, events, summary


def from_native(obj):
    if not isinstance(obj, dict) or "events" not in obj:
        raise DataError("Fault Lines JSON needs 'teams' and 'events'")
    return finish(obj.get("teams"), obj["events"])


def from_csv(text, home="Home", away="Away"):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not {"t", "team", "type", "player"} <= set(rows[0]):
        raise DataError("CSV needs a header with at least t, team, type, player (see the sample file)")
    teams = {"home": {"name": home, "players": {}}, "away": {"name": away, "players": {}}}
    for r in rows:
        side = str(r.get("team", "")).lower()
        if side in teams:
            for who, role in ((r.get("player"), r.get("player_role")), (r.get("recipient"), r.get("recipient_role"))):
                if who and role:
                    teams[side]["players"].setdefault(" ".join(who.split()), role)
    missing = {(r.get("team"), " ".join(str(r.get(k) or "").split())) for r in rows for k in ("player", "recipient")
               if r.get(k) and str(r.get("team", "")).lower() in teams and r.get("type") != "own_goal"
               and " ".join(r[k].split()) not in teams[str(r["team"]).lower()]["players"]}
    if missing:
        raise DataError("no role for: " + ", ".join(sorted(f"{n} ({t})" for t, n in missing))[:300]
                        + " - add player_role / recipient_role on at least one row each")
    for t in teams.values():
        t["players"] = [{"name": n, "role": r} for n, r in t["players"].items()]
    return finish(teams, rows)


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
    """StatsBomb event JSON (locations already in each team's attacking direction, 120 x 80)."""
    if not isinstance(evs, list) or not evs or not isinstance(evs[0], dict) or "type" not in evs[0]:
        raise DataError("not a StatsBomb events array")
    nm = lambda d: (d or {}).get("name")
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
            on = nm(e.get("substitution", {}).get("replacement"))
            if on and on not in role:
                role[on] = role.get(nm(e.get("player")), "RCM")  # the replacement takes the replaced player's role
                teams[side[nm(e["team"])]]["players"].append({"name": on, "role": role[on]})
    out = []
    for e in evs:
        kind, team = nm(e.get("type")), side.get(nm(e.get("team")))
        if team is None or e.get("period", 1) > 4 or kind not in ("Pass", "Shot", "Own Goal For"):
            continue
        loc = e.get("location") or [60, 40]
        base = {"t": e.get("minute", 0) * 60 + e.get("second", 0), "team": team, "player": nm(e.get("player")),
                "x": loc[0] / 1.2, "y": loc[1] / 0.8, "pressure": bool(e.get("under_pressure"))}
        if kind == "Pass":
            p = e.get("pass", {})
            end = p.get("end_location") or loc
            ok = p.get("outcome") is None and nm(p.get("recipient")) is not None
            out.append({**base, "type": "pass", "end_x": end[0] / 1.2, "end_y": end[1] / 0.8, "success": ok,
                        "recipient": nm(p.get("recipient")) if ok else None})
        elif kind == "Shot":
            s = e.get("shot", {})
            out.append({**base, "type": "shot", "xg": s.get("statsbomb_xg"), "goal": nm(s.get("outcome")) == "Goal"})
        else:
            out.append({**base, "type": "own_goal", "player": "Own goal"})
    return finish(teams, out, ["first team in the file is treated as home"])


def parse(text, fmt="auto", home="Home", away="Away"):
    """Any supported format -> (lineups, events, summary)."""
    if len(text) > 8_000_000:
        raise DataError("file too large (8 MB limit)")
    if fmt == "csv" or (fmt == "auto" and not text.lstrip().startswith(("{", "["))):
        return from_csv(text, home, away)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise DataError(f"not valid JSON ({e.msg} at line {e.lineno})") from None
    if fmt == "statsbomb" or (fmt == "auto" and isinstance(obj, list)):
        return from_statsbomb(obj)
    return from_native(obj)


def to_native(events, teams):
    """A match in Fault Lines JSON (used for the downloadable sample)."""
    keys = ("t", "team", "type", "player", "recipient", "x", "y", "end_x", "end_y", "success", "pressure", "xg", "goal")
    return {"teams": {s: {"name": teams[s]["name"], "players": [{"name": p, "role": r} for p, r in teams[s]["roles"].items()]}
                      for s in ("home", "away")},
            "events": [{k: e[k] for k in keys} for e in events]}


def to_csv(events, teams):
    cols = ["t", "team", "type", "player", "player_role", "recipient", "recipient_role", "x", "y", "end_x", "end_y",
            "success", "pressure", "xg", "goal"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, cols, lineterminator="\n")
    w.writeheader()
    for e in events:
        roles = teams[e["team"]]["roles"]
        w.writerow({**{k: e.get(k) for k in cols if k in e}, "player_role": roles.get(e["player"], ""),
                    "recipient_role": roles.get(e["recipient"], "") if e["recipient"] else "",
                    "xg": "" if e["xg"] is None else e["xg"]})
    return buf.getvalue()
