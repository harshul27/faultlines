"""Data pipeline for outside match data: noisy in, clean and accountable out.

    ingest -> standardise -> validate -> clean -> impute -> quality gate -> store -> report

Practices it follows:
* Nothing changes silently: every repair is logged with its reason, a count and examples.
* Provenance: every value the pipeline fills in is flagged on the event ("imputed"), with the source row it came from.
* Quarantine, not deletion: rows that cannot be repaired are set aside with the reason and returned to the user.
  Rows outside the scope (fouls, substitutions...) are counted as out of scope, not treated as errors.
* A quality gate stops data too broken to analyse, and the quality score says how far to trust what passes.
* Reproducible: pipeline version + SHA-256 of the input in the manifest; same input, same output.
* The user gets everything back: clean data, quarantine, quality report, manifest and a data dictionary.

Handles: column aliases, CSV dialects, decimal commas, booleans in many spellings, times as seconds / minutes /
"mm:ss" / minute+second / per-half clocks, coordinate scales (0-1, 0-100, metres 105x68, 120x80), fixed-frame data
(teams switching ends at half time), duplicate and near-duplicate rows, out-of-order rows, inconsistent name
spellings, markup in names, missing roles (inferred from where players played), missing pass outcome / receiver /
end location (inferred from the next event, as providers do), missing xG (location model).
"""
import csv
import difflib
import hashlib
import io
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict

import numpy as np

from match import BASE, M_X, M_Y

VERSION = "1.0"
ROLE_CODES = set(BASE)
NAME_OK = re.compile(r"^[\w .'’-]{1,60}$")


class DataError(ValueError):
    """The data cannot be analysed; .report says why (the quality report so far)."""
    def __init__(self, msg, report=None):
        super().__init__(msg)
        self.report = report


# ---------- standardise ----------
COLUMNS = {  # canonical field -> accepted header spellings (normalised: lowercase, non-alphanumerics -> _)
    "t": ["t", "time", "seconds", "secs", "time_s", "time_seconds", "timestamp", "ts", "clock", "match_time", "event_time"],
    "minute": ["minute", "min", "mins", "match_minute"], "second": ["second", "sec", "seconds_in_minute"],
    "period": ["period", "half", "period_id", "match_period"],
    "team": ["team", "side", "team_side", "club", "team_name", "squad"],
    "type": ["type", "event", "event_type", "action", "action_type", "kind", "type_name"],
    "player": ["player", "player_name", "actor", "passer", "shooter", "from_player"],
    "player_role": ["player_role", "role", "position", "pos", "player_position"],
    "recipient": ["recipient", "receiver", "to", "to_player", "pass_recipient", "recipient_name", "receiver_name"],
    "recipient_role": ["recipient_role", "receiver_role", "receiver_position", "recipient_position"],
    "x": ["x", "start_x", "x_start", "location_x", "loc_x", "x1", "from_x"],
    "y": ["y", "start_y", "y_start", "location_y", "loc_y", "y1", "from_y"],
    "end_x": ["end_x", "x_end", "endx", "pass_end_x", "x2", "to_x", "end_location_x"],
    "end_y": ["end_y", "y_end", "endy", "pass_end_y", "y2", "to_y", "end_location_y"],
    "success": ["success", "outcome", "completed", "complete", "successful", "accurate", "is_success", "result"],
    "pressure": ["pressure", "under_pressure", "pressured", "is_under_pressure"],
    "xg": ["xg", "x_g", "expected_goals", "statsbomb_xg", "shot_xg"],
    "goal": ["goal", "is_goal", "scored"],
}
TYPES = {"pass": "pass", "passes": "pass", "p": "pass", "pass_completed": "pass", "pass_attempt": "pass", "cross": "pass",
         "shot": "shot", "shots": "shot", "s": "shot", "attempt": "shot", "shot_on_target": "shot", "shot_off_target": "shot",
         "penalty": "shot", "goal": "goal", "own_goal": "own_goal", "owngoal": "own_goal", "og": "own_goal", "own_goal_for": "own_goal"}
TRUE = {"1", "1.0", "true", "t", "yes", "y", "complete", "completed", "successful", "success", "accurate", "ok", "on_target"}
FALSE = {"0", "0.0", "false", "f", "no", "n", "incomplete", "failed", "unsuccessful", "fail", "out", "inaccurate",
         "blocked", "intercepted", "lost", "wayward", "off_target"}
ROLES = {
    "gk": "GK", "g": "GK", "goalkeeper": "GK", "keeper": "GK",
    "rb": "RB", "rwb": "RB", "right_back": "RB", "right_wing_back": "RB", "lb": "LB", "lwb": "LB", "left_back": "LB", "left_wing_back": "LB",
    "rcb": "RCB", "right_centre_back": "RCB", "right_center_back": "RCB", "lcb": "LCB", "left_centre_back": "LCB", "left_center_back": "LCB",
    "cb": "CB", "d": "CB", "centre_back": "CB", "center_back": "CB", "defender": "CB",
    "cdm": "CDM", "dm": "CDM", "dmf": "CDM", "defensive_midfield": "CDM", "defensive_midfielder": "CDM", "holding_midfielder": "CDM",
    "center_defensive_midfield": "CDM", "right_defensive_midfield": "CDM", "left_defensive_midfield": "CDM", "anchor": "CDM",
    "rcm": "RCM", "right_center_midfield": "RCM", "right_centre_midfield": "RCM", "lcm": "LCM", "left_center_midfield": "LCM",
    "left_centre_midfield": "LCM", "cm": "CM", "m": "CM", "mf": "CM", "midfielder": "CM", "central_midfielder": "CM",
    "center_midfield": "CM", "centre_midfield": "CM", "am": "CM", "cam": "CM", "attacking_midfielder": "CM",
    "center_attacking_midfield": "CM", "right_attacking_midfield": "RCM", "left_attacking_midfield": "LCM",
    "rw": "RW", "rm": "RW", "right_wing": "RW", "right_winger": "RW", "right_midfield": "RW", "right_midfielder": "RW",
    "lw": "LW", "lm": "LW", "left_wing": "LW", "left_winger": "LW", "left_midfield": "LW", "left_midfielder": "LW",
    "st": "ST", "cf": "ST", "fw": "ST", "f": "ST", "striker": "ST", "forward": "ST", "centre_forward": "ST", "center_forward": "ST",
    "ss": "ST", "secondary_striker": "ST", "right_center_forward": "ST", "left_center_forward": "ST",
}
SCALES = {"unit": (1, 1), "percent": (100, 100), "metres": (105, 68), "statsbomb": (120, 80)}


def _key(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def _fold(name):
    """Comparison key for names: no accents, case, dots or extra spaces."""
    s = unicodedata.normalize("NFKD", name)
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).lower().replace(".", " ").split())


def clean_name(v):
    """Markup and disallowed characters removed, whitespace collapsed, NFC. Returns (name or None, changed)."""
    raw = "" if v is None else str(v)
    s = unicodedata.normalize("NFC", re.sub(r"<[^>]*>", " ", raw))
    s = " ".join(re.sub(r"[^\w .'’-]", " ", s).split())[:60]
    return (s or None), s != " ".join(raw.split())


def _num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if math.isnan(v) or math.isinf(v) else float(v)
    s = str(v).strip().replace(" ", "")
    if re.fullmatch(r"-?\d+,\d+", s):  # decimal comma
        s = s.replace(",", ".")
    try:
        f = float(s)
    except ValueError:
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _flag(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    k = _key(v)
    return True if k in TRUE else False if k in FALSE else None


def _time(v):
    """Seconds from a number or a clock string ("mm:ss", "hh:mm:ss", "45+2:10" -> 47:10)."""
    if isinstance(v, str) and ":" in v:
        m = re.fullmatch(r"\s*(\d+)(?:\+(\d+))?:(\d{1,2}(?:\.\d+)?)(?::(\d{1,2}(?:\.\d+)?))?\s*", v)
        if not m:
            return None
        a, extra, b, c = m.groups()
        if c is not None:  # hh:mm:ss
            return int(a) * 3600 + float(b) * 60 + float(c)
        return (int(a) + int(extra or 0)) * 60 + float(b)
    return _num(v)


class Log:
    """The quality report under construction: fixes, out-of-scope rows, quarantine, notes."""
    def __init__(self):
        self.fixes, self.quarantine, self.out_of_scope, self.notes = defaultdict(lambda: {"count": 0, "examples": []}), [], Counter(), []

    def fix(self, stage, what, example=None):
        f = self.fixes[(stage, what)]
        f["count"] += 1
        if example is not None and len(f["examples"]) < 3:
            f["examples"].append(example)

    def drop(self, row, reason):
        self.quarantine.append({"source_row": row.get("source_row"), "reason": reason,
                                "raw": {k: v for k, v in (row.get("raw") or {}).items() if k != "source_row"}})


def standardise(raw_rows, log):
    """Map column names and values to the canonical model. Returns rows with parsed fields + raw copy."""
    if not raw_rows:
        raise DataError("no rows found")
    headers = {_key(k) for r in raw_rows[:50] if isinstance(r, dict) for k in r}
    mapping = {}
    for field, names in COLUMNS.items():
        hit = next((n for n in names if n in headers), None)
        if hit:
            mapping[hit] = field
    renamed = [f"{h} -> {f}" for h, f in mapping.items() if h != f]
    if renamed:
        log.notes.append("columns mapped to the standard names: " + ", ".join(renamed))
    missing = [f for f in ("team", "type", "player") if f not in mapping.values()]
    if missing or not ({"t", "minute"} & set(mapping.values())):
        raise DataError(f"missing columns: {', '.join(missing + ([] if {'t', 'minute'} & set(mapping.values()) else ['t (time)']))}")
    out = []
    for i, r in enumerate(raw_rows):
        if not isinstance(r, dict):
            log.drop({"source_row": i + 1, "raw": {"value": str(r)[:80]}}, "row is not a record")
            continue
        row = {"source_row": i + 1, "raw": {k: r[k] for k in r}}
        for k, v in r.items():
            f = mapping.get(_key(k))
            if f:
                row[f] = v.strip() if isinstance(v, str) else v
        out.append(row)
    return out


# ---------- the pipeline ----------
LABELS = {"home": "home", "h": "home", "1": "home", "home_team": "home", "away": "away", "a": "away", "2": "away", "away_team": "away"}


def _parse_row(r, log):
    """One standardised row -> parsed fields, or None (out of scope / quarantined). Shared by files and live feeds."""
    kind = TYPES.get(_key(r.get("type", "")))
    if kind is None:
        log.out_of_scope[str(r.get("type") or "(empty)")[:30]] += 1
        return None
    if kind == "goal":
        kind, r["goal"] = "shot", True
        log.fix("standardise", "event type 'goal' read as a scoring shot")
    r["kind"] = kind
    p = _num(r.get("period"))
    r["period"] = int(p) if p and 1 <= p <= 5 else None
    if r["period"] == 5:
        log.out_of_scope["penalty shoot-out"] += 1
        return None
    if r.get("t") not in (None, ""):
        r["secs"] = _time(r["t"])
        if isinstance(r["t"], str) and ":" in r["t"]:
            log.fix("standardise", "clock time (mm:ss) converted to seconds")
    else:
        m, s = _num(r.get("minute")), _num(r.get("second")) or 0.0
        r["secs"] = None if m is None else m * 60 + s
    if r["secs"] is None or r["secs"] < 0 or r["secs"] > 130 * 60 * 60:
        log.drop(r, "time missing or not readable")
        return None
    for f in ("x", "y", "end_x", "end_y", "xg"):
        raw_v, r[f] = r.get(f), _num(r.get(f))
        if raw_v not in (None, "") and r[f] is None:
            log.fix("standardise", f"unreadable {f} treated as missing", str(raw_v)[:20])
        elif isinstance(raw_v, str) and re.fullmatch(r"\s*-?\d+,\d+\s*", raw_v):
            log.fix("standardise", "decimal comma read as a decimal point")
    for f in ("success", "pressure", "goal"):
        raw_v, r[f] = r.get(f), _flag(r.get(f))
        if raw_v not in (None, "") and r[f] is None:
            log.fix("standardise", f"unrecognised {f} value treated as missing", str(raw_v)[:20])
    for f in ("player", "recipient"):
        if r.get(f) not in (None, ""):
            name, changed = clean_name(r[f])
            if changed:
                log.fix("standardise", "names cleaned (markup, symbols or spacing removed)", str(r[f])[:40])
            r[f] = name
        else:
            r[f] = None
    for f in ("player_role", "recipient_role"):
        r[f] = ROLES.get(_key(r[f])) if r.get(f) not in (None, "") else None
    if kind != "own_goal" and not r["player"]:
        log.drop(r, "no player name")
        return None
    return r


def _same_person(a, b):
    """Spelling variants of one name: case/accents/dots, initial + surname, or a small typo."""
    fa, fb = _fold(a), _fold(b)
    if fa == fb:
        return True
    x, y = fa.split(), fb.split()
    if len(x) >= 2 and len(y) >= 2 and x[-1] == y[-1] and x[0][0] == y[0][0] and (len(x[0]) == 1 or len(y[0]) == 1):
        return True
    return difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.9


def _flip(r):
    for fx, fy in (("x", "y"), ("end_x", "end_y")):
        if r[fx] is not None:
            r[fx], r[fy] = 100 - r[fx], 100 - (r[fy] if r[fy] is not None else 50)


def _scale(r, scale, log):
    """Convert to 0-100; clamp small overshoots; False if far off the pitch."""
    sx, sy = SCALES[scale]
    for f, s in (("x", sx), ("end_x", sx), ("y", sy), ("end_y", sy)):
        if r[f] is None:
            continue
        v = r[f] * 100 / s
        if -5 <= v < 0 or 100 < v <= 105:
            log.fix("clean", "coordinates slightly off the pitch clamped to the touchline/goal line")
            v = min(100.0, max(0.0, v))
        elif not 0 <= v <= 100:
            return False
        r[f] = v
    return True


def _impute(r, nxt, prev, roles, log):
    """Fill what providers usually derive from neighbouring events; flag it. Returns the clean event or None."""
    same = nxt is not None and nxt["side"] == r["side"]
    imp = []
    if r["kind"] == "pass":
        if r["recipient"] and r["recipient"] not in roles[r["side"]]:
            other = "away" if r["side"] == "home" else "home"
            if r["recipient"] in roles[other]:
                r["recipient"], r["success"] = None, False
                log.fix("clean", "pass recorded to an opponent turned into a lost pass")
            else:
                log.drop(r, f"receiver {r['recipient']} is not in the {r['side']} line-up")
                return None
        if r["success"] is None:
            r["success"] = bool(same and nxt["player"] != r["player"]) or (r["recipient"] is not None and r["recipient"] != r["player"])
            imp.append("success")
        if r["success"] and not r["recipient"]:
            if same and nxt["player"] != r["player"]:
                r["recipient"] = nxt["player"]
                imp.append("recipient")
            else:
                log.drop(r, "completed pass with no receiver, and the next event does not show one")
                return None
        if r["success"] and r["recipient"] == r["player"]:
            r["success"], r["recipient"] = False, None
            log.fix("clean", "pass to self treated as not completed")
        if r["end_x"] is None or r["end_y"] is None:
            if nxt is not None and nxt["x"] is not None:
                ny = nxt["y"] if nxt["y"] is not None else 50
                r["end_x"], r["end_y"] = (nxt["x"], ny) if same else (100 - nxt["x"], 100 - ny)
                imp.append("end_location")
            else:
                log.drop(r, "pass with no end location, and the next event does not show one")
                return None
    if r["x"] is None or r["y"] is None:
        if prev and prev["team"] == r["side"] and prev["recipient"] == r["player"]:
            r["x"], r["y"] = prev["end_x"], prev["end_y"]
            imp.append("location")
        elif r["kind"] == "own_goal":
            r["x"], r["y"] = 50.0, 50.0
        else:
            log.drop(r, "no location, and the previous pass does not show one")
            return None
    if r["kind"] == "shot":
        if r["xg"] is None or not 0 <= r["xg"] <= 1:
            r["xg"] = round(_xg(r["x"], r["y"]), 3)
            imp.append("xg")
        if r["goal"] is None:
            r["goal"] = False
            imp.append("goal")
    for f in imp:
        log.fix("impute", f"{f.replace('_', ' ')} inferred" + (" from the next event" if f in ("success", "recipient", "end_location")
                                                             else " from the previous pass" if f == "location"
                                                             else " from shot location" if f == "xg" else " as no goal"))
    shot, og = r["kind"] == "shot", r["kind"] == "own_goal"
    return dict(t=round(r["secs"], 1), minute=int(r["secs"] // 60), team=r["side"], type=r["kind"],
                player=r["player"] or "Own goal", recipient=r["recipient"] if r["kind"] == "pass" and r["success"] else None,
                x=round(r["x"], 1), y=round(r["y"], 1),
                end_x=round(r["end_x"], 1) if r["kind"] == "pass" else 100.0, end_y=round(r["end_y"], 1) if r["kind"] == "pass" else 50.0,
                success=bool(r["success"]) if r["kind"] == "pass" else bool(r["goal"]) if shot else False,
                pressure=bool(r["pressure"]), xg=r["xg"] if shot else (0.0 if og else None),
                goal=bool(r["goal"]) if shot else og, source_row=r.get("source_row"), imputed=imp)


def run(raw_rows, teams=None, *, fmt="csv", home=None, away=None, scale="auto", direction="auto",
        input_text="", encoding="utf-8"):
    """raw_rows: list of dicts (any spelling of the columns). teams: optional line-ups {side: {name, players:[{name, role}]}}.
    scale: auto|unit|percent|metres|statsbomb. direction: auto|attacking|fixed_home_right|fixed_home_left.
    Returns (lineups, events, quarantine, report). Raises DataError (with .report) when the quality gate fails."""
    t0, log = time.time(), Log()
    rows = standardise(raw_rows, log)
    parsed = [p for p in (_parse_row(r, log) for r in rows) if p]
    lineup_players = {s: [p for p in (((teams or {}).get(s) or {}).get("players") or []) if isinstance(p, dict)] for s in ("home", "away")}

    # Teams: labels (home/away, h/a, 1/2) or club names -> sides
    names = {s: clean_name(home if s == "home" else away)[0] or clean_name(((teams or {}).get(s) or {}).get("name"))[0] for s in ("home", "away")}
    aliases = dict(LABELS)
    for s, n in names.items():
        if n:
            aliases[_key(n)] = s
    labels = list(dict.fromkeys(_key(r.get("team", "")) for r in parsed))
    free = [s for s in ("home", "away") if s not in {aliases[v] for v in labels if v in aliases}]
    for v in [v for v in labels if v and v not in aliases][:len(free)]:  # club names as labels: first seen takes the first free side
        side = free.pop(0)
        aliases[v] = side
        names[side] = names[side] or str(next(r["team"] for r in parsed if _key(r.get("team", "")) == v))
        log.fix("standardise", f"team label read as {side}", v)
    kept = []
    for r in parsed:
        r["side"] = aliases.get(_key(r.get("team", "")))
        if r["side"] is None:
            log.drop(r, f"team '{str(r.get('team'))[:30]}' is neither of the two teams")
        else:
            kept.append(r)
    parsed = kept
    names = {s: clean_name(names[s])[0] or s.title() for s in ("home", "away")}
    if names["home"] == names["away"]:
        names["away"] += " (away)"

    # Time: minutes given as t, per-half clocks, file order
    if len(parsed) > 200 and max(r["secs"] for r in parsed) <= 130 and sum(r["secs"] % 1 == 0 for r in parsed) / len(parsed) > 0.9:
        for r in parsed:
            r["secs"] *= 60
        log.fix("clean", "time column looked like minutes; converted to seconds")
    offsets = {1: 0, 2: 45 * 60, 3: 90 * 60, 4: 105 * 60}
    by_period = defaultdict(list)
    for r in parsed:
        if r["period"]:
            by_period[r["period"]].append(r["secs"])
    for p, ts in by_period.items():
        if p > 1 and min(ts) < offsets[p] - 10 * 60:  # this period's clock restarted at 0
            for r in parsed:
                if r["period"] == p:
                    r["secs"] += offsets[p]
            log.fix("clean", f"period {p} clock restarted at 0; offset by {offsets[p] // 60} minutes")
    if not by_period:  # no period column: a big backwards jump in file order means the clock restarted
        shift, prev = 0.0, None
        for r in parsed:
            if prev is not None and r["secs"] + shift < prev - 20 * 60 and not shift:
                shift = 45 * 60
                log.fix("clean", "clock restarted mid-file (second half from 0); offset by 45 minutes")
            r["secs"] += shift
            prev = r["secs"]
    for r in parsed:
        r["period"] = r["period"] or (1 if r["secs"] < 45 * 60 else 2)
    inversions = sum(b["secs"] < a["secs"] for a, b in zip(parsed, parsed[1:]))
    if inversions:
        log.fix("clean", "rows out of time order; sorted by time", f"{inversions} inversions")
    parsed.sort(key=lambda r: (r["secs"], r["source_row"]))

    # Duplicates: exact, then near (same player and action within 0.5 s at the same spot)
    seen, kept, last = set(), [], {}
    for r in parsed:
        k = (round(r["secs"], 1), r["side"], r["kind"], r["player"], r["x"], r["y"])
        lk = (r["side"], r["kind"], r["player"])
        prev = last.get(lk)
        if k in seen:
            log.fix("clean", "exact duplicate rows removed")
        elif prev and r["secs"] - prev["secs"] <= 0.5 and None not in (r["x"], prev["x"]) and abs(r["x"] - prev["x"]) <= 1.5 \
                and abs((r["y"] or 0) - (prev["y"] or 0)) <= 1.5:
            log.fix("clean", "near-duplicate rows removed (same player, action and spot within 0.5 s)")
        else:
            seen.add(k)
            last[lk] = r
            kept.append(r)
    parsed = kept

    # Coordinates: detect the scale (or use the declared one), convert to 0-100
    if scale == "auto":
        xs = [v for r in parsed for v in (r["x"], r["end_x"]) if v is not None]
        ys = [v for r in parsed for v in (r["y"], r["end_y"]) if v is not None]
        px, py = (float(np.percentile(xs, 99.5)) if xs else 100.0), (float(np.percentile(ys, 99.5)) if ys else 100.0)
        scale = ("unit" if px <= 1.01 and py <= 1.01 else "statsbomb" if 106 < px <= 125 and py <= 82
                 else "metres" if px <= 106 and (px > 101 or py <= 70) else "percent")
        scale_conf = "high" if scale in ("unit", "statsbomb") or (scale == "metres" and px > 101) or (scale == "percent" and py > 85) else "medium"
        log.notes.append(f"coordinate scale detected: {scale} ({SCALES[scale][0]} x {SCALES[scale][1]}), confidence {scale_conf}")
    else:
        log.notes.append(f"coordinate scale declared: {scale}")
    kept = []
    for r in parsed:
        if _scale(r, scale, log):
            kept.append(r)
        else:
            log.drop(r, "coordinates far off the pitch")
    parsed = kept

    # Attack direction per team and period: data in a fixed frame (teams switch ends) is turned into each team's frame
    keepers = {s: {clean_name(p.get("name"))[0] for p in lineup_players[s] if ROLES.get(_key(p.get("role", ""))) == "GK"} for s in ("home", "away")}
    frame_conf = "declared" if direction != "auto" else "high"
    for side in ("home", "away"):
        for period in sorted({r["period"] for r in parsed}):
            mine = [r for r in parsed if r["side"] == side and r["period"] == period]
            if not mine or direction == "attacking":
                continue
            if direction == "auto":
                shots = [r["x"] for r in mine if r["kind"] == "shot" and r["x"] is not None]
                keeper = [r["x"] for r in mine if (r.get("player_role") == "GK" or r["player"] in keepers[side]) and r["x"] is not None]
                votes = ([float(np.median(shots)) < 50] * min(3, len(shots)) if shots else []) +                         ([float(np.mean(keeper)) > 50] * 2 if len(keeper) >= 3 else [])
                if not votes:
                    frame_conf = "low"
                    log.notes.append(f"{side}, period {period}: no shots or keeper actions to confirm the attack direction; assumed already in the team's frame")
                    continue
                if 0 < sum(votes) < len(votes):
                    frame_conf = "medium" if frame_conf == "high" else frame_conf
                flip = sum(votes) > len(votes) / 2
            else:  # declared fixed frame
                attacks_right = (side == "home") == (direction == "fixed_home_right")
                flip = attacks_right == (period % 2 == 0)
            if flip:
                for r in mine:
                    _flip(r)
                log.fix("clean", "attack direction turned into the acting team's frame", f"{side}, period {period}")

    # Names: merge spelling variants within a team ("L. Ferrand", "luca ferrand", "Luca Ferand"); line-up spelling wins
    counts = {s: Counter() for s in ("home", "away")}
    for r in parsed:
        for f in ("player", "recipient"):
            if r[f] and r["kind"] != "own_goal":
                counts[r["side"]][r[f]] += 1
    canon = {}
    for side in ("home", "away"):
        listed = [n for n in (clean_name(p.get("name"))[0] for p in lineup_players[side]) if n]
        groups = []
        for n in dict.fromkeys(listed + [n for n, _ in counts[side].most_common()]):
            g = next((g for g in groups if _same_person(n, g[0])), None)
            g.append(n) if g else groups.append([n])
        for g in groups:
            main = next((n for n in g if n in listed), None) or max(g, key=lambda n: (counts[side][n], len(n)))
            for n in g:
                canon[(side, n)] = main
                if n != main and counts[side][n]:
                    log.fix("clean", "name spelling variants merged", f"{n} -> {main}")
    for r in parsed:
        for f in ("player", "recipient"):
            if r[f] and r["kind"] != "own_goal":
                r[f] = canon.get((r["side"], r[f]), r[f])
    tally = {s: Counter(r["player"] for r in parsed if r["side"] == s and r["kind"] != "own_goal") for s in ("home", "away")}
    both = set(tally["home"]) & set(tally["away"])
    if both:  # one player recorded for both teams: the minority side's rows are suspect
        owner = {n: "home" if tally["home"][n] >= tally["away"][n] else "away" for n in both}
        kept = []
        for r in parsed:
            if r["kind"] != "own_goal" and owner.get(r["player"], r["side"]) != r["side"]:
                log.drop(r, f"{r['player']} is recorded for both teams; this row is on the minority side")
            else:
                kept.append(r)
        parsed = kept

    # Roles: line-up > the role most often given in the rows > inferred from where the player was on the ball
    roles = {s: {} for s in ("home", "away")}
    for side in ("home", "away"):
        for p in lineup_players[side]:
            n = canon.get((side, clean_name(p.get("name"))[0]), clean_name(p.get("name"))[0])
            if n:
                roles[side][n] = ROLES.get(_key(p.get("role", "")))
        votes = defaultdict(Counter)
        for r in parsed:
            if r["side"] == side:
                for f, rf in (("player", "player_role"), ("recipient", "recipient_role")):
                    if r[f] and r.get(rf):
                        votes[r[f]][r[rf]] += 1
        for n, c in votes.items():
            roles[side][n] = roles[side].get(n) or c.most_common(1)[0][0]
        where = defaultdict(list)
        for r in parsed:
            if r["side"] == side and r["kind"] != "own_goal":
                if r["x"] is not None:
                    where[r["player"]].append((r["x"], r["y"] if r["y"] is not None else 50))
                if r["recipient"] and r["end_x"] is not None:
                    where[r["recipient"]].append((r["end_x"], r["end_y"] if r["end_y"] is not None else 50))
        for n in set(roles[side]) | set(where):
            role, pts = roles[side].get(n), where.get(n) or [(50, 50)]
            mx, my = float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))
            if role in ("CB", "CM"):  # a generic centre role: the side comes from where they played (y < 50 = left)
                role = ("LCB" if my < 50 else "RCB") if role == "CB" else ("LCM" if my < 50 else "RCM")
            if role not in ROLE_CODES:
                cands = [k for k in BASE if k != "GK"] if mx > 20 or len(pts) < 3 else list(BASE)
                role = min(cands, key=lambda k: math.hypot(BASE[k][0] - mx, BASE[k][1] - my))
                log.fix("impute", "role inferred from where the player was on the ball", f"{n} -> {role}")
            roles[side][n] = role

    # Impute from neighbouring events; quarantine what cannot be derived
    events = []
    for i, r in enumerate(parsed):
        nxt = next((q for q in parsed[i + 1:i + 4] if q["kind"] != "own_goal"), None)
        e = _impute(r, nxt, events[-1] if events else None, roles, log)
        if e:
            events.append(e)

    # Line-ups: everyone who took part, most involved first
    lineups = {"id": hashlib.sha256((input_text or json.dumps(raw_rows, default=str)[:200000]).encode("utf-8", "replace")).hexdigest()[:12]}
    for side in ("home", "away"):
        used = Counter([e["player"] for e in events if e["team"] == side and e["type"] != "own_goal"] +
                       [e["recipient"] for e in events if e["team"] == side and e["recipient"]])
        players = [n for n, _ in used.most_common()][:40]
        lineups[side] = {"name": names[side], "players": players, "roles": {n: roles[side][n] for n in players}}

    # Quality: score, gate, caveats
    report = quality(len(raw_rows), rows, events, log, frame_conf, scale, names, fmt, encoding, t0)
    passes_ok = sum(e["type"] == "pass" and e["success"] for e in events)
    reason = ("both teams need events" if not lineups["home"]["players"] or not lineups["away"]["players"]
              else "fewer than 30 completed passes" if passes_ok < 30
              else "too many rows could not be repaired" if report["dimensions"]["validity"] < 0.7 else None)
    report["gate"] = {"passed": reason is None, **({"reason": reason} if reason else {})}
    if reason:
        raise DataError(f"data quality gate: {reason} (quality {report['score']}/100; see the report)", report)
    return lineups, events, log.quarantine, report


class LiveCleaner:
    """The same rules for a live feed, one batch at a time: standardise, validate, de-duplicate, keep time order, and
    infer receiver / outcome / end location from the next event (a one-event look-ahead, as live providers do)."""
    def __init__(self, lineups, scale="percent", direction="attacking"):
        self.lineups, self.scale, self.direction = lineups, scale, direction
        self.roles = {s: lineups[s]["roles"] for s in ("home", "away")}
        self.aliases = {**LABELS, **{_key(lineups[s]["name"]): s for s in ("home", "away")}}
        self.log, self.pending, self.prev, self.last_t, self.recent, self.received, self.emitted = Log(), None, None, 0.0, [], 0, 0

    def _name(self, side, n):
        if n is None or n in self.roles[side]:
            return n
        return next((m for m in self.roles[side] if _same_person(n, m)), None)

    def push(self, raw_rows):
        """Clean a batch; returns the events ready for analysis (a pass waiting on the next event is held back)."""
        out = []
        for i, raw in enumerate(raw_rows):
            self.received += 1
            if not isinstance(raw, dict):
                self.log.drop({"source_row": self.received, "raw": {"value": str(raw)[:80]}}, "event is not an object")
                continue
            row = {"source_row": self.received, "raw": dict(raw), **{f: raw.get(f) for f in COLUMNS}}
            r = _parse_row(row, self.log)
            if r is None:
                continue
            r["side"] = self.aliases.get(_key(r.get("team", "")))
            if r["side"] is None:
                self.log.drop(r, "team must be home/away or one of the two club names")
                continue
            for f in ("player", "recipient"):
                if r[f] and r["kind"] != "own_goal":
                    m = self._name(r["side"], r[f])
                    if m is None and f == "player":
                        self.log.drop(r, f"{r[f]} is not in the {r['side']} line-up")
                        break
                    if m and m != r[f]:
                        self.log.fix("clean", "name spelling variants merged", f"{r[f]} -> {m}")
                    r[f] = m if m else r[f]
            else:
                if r["secs"] < self.last_t:
                    self.log.drop(r, f"arrived out of time order (after {self.last_t:.1f} s)")
                    continue
                k = (round(r["secs"], 1), r["side"], r["kind"], r["player"], r["x"], r["y"])
                if k in self.recent:
                    self.log.fix("clean", "exact duplicate rows removed")
                    continue
                self.recent = (self.recent + [k])[-50:]
                if not _scale(r, self.scale, self.log):
                    self.log.drop(r, "coordinates far off the pitch")
                    continue
                r["period"] = r["period"] or (1 if r["secs"] < 45 * 60 else 2)
                if self.direction.startswith("fixed"):
                    attacks_right = (r["side"] == "home") == (self.direction == "fixed_home_right")
                    if attacks_right == (r["period"] % 2 == 0):
                        _flip(r)
                self.last_t = r["secs"]
                out += self._settle(r)
        return out

    def _settle(self, r):
        """Resolve the held pass with this event as its 'next'; hold this one if it needs the next event."""
        ready = []
        if self.pending is not None:
            e = _impute(self.pending, r if r["kind"] != "own_goal" else None, self.prev, self.roles, self.log)
            self.pending = None
            if e:
                ready.append(e)
                self.prev = e
        needs_next = r["kind"] == "pass" and (r["success"] is None or (r["success"] and not r["recipient"]) or r["end_x"] is None or r["end_y"] is None)
        if needs_next:
            self.pending = r
        else:
            e = _impute(r, None, self.prev, self.roles, self.log)
            if e:
                ready.append(e)
                self.prev = e
        self.emitted += len(ready)
        return ready

    def report(self):
        fixes = [{"stage": k[0], "what": k[1], **v} for k, v in self.log.fixes.items()]
        in_scope = max(1, self.received - sum(self.log.out_of_scope.values()))
        return {"pipeline_version": VERSION, "received": self.received, "clean_events": self.emitted,
                "held_for_next_event": int(self.pending is not None), "quarantined": len(self.log.quarantine),
                "validity": round(1 - len(self.log.quarantine) / in_scope, 3), "fixes": fixes,
                "quarantine_reasons": dict(Counter(q["reason"] for q in self.log.quarantine).most_common(10)),
                "recent_quarantine": self.log.quarantine[-5:], "out_of_scope": dict(self.log.out_of_scope)}


def _xg(x, y):
    """Location-only expected goals: distance and angle to goal (a simple, documented stand-in for provider xG)."""
    dx, dy = (100 - x) * M_X, (50 - y) * M_Y
    dist = math.hypot(dx, dy)
    angle = math.atan2(7.32 * dx, dx * dx + dy * dy - (7.32 / 2) ** 2) if dist else math.pi
    angle = angle if angle >= 0 else angle + math.pi
    return min(0.75, max(0.01, 1 / (1 + math.exp(-(-1.1 + 1.6 * angle - 0.09 * dist)))))


def quality(rows_in, rows, events, log, frame_conf, scale, names, fmt, encoding, t0):
    imputed_events = sum(bool(e["imputed"]) for e in events)
    in_scope = max(1, len(rows) - sum(log.out_of_scope.values()))
    fixes = [{"stage": k[0], "what": k[1], **v} for k, v in log.fixes.items()]
    dup = sum(f["count"] for f in fixes if "duplicate" in f["what"])
    merged = sum(f["count"] for f in fixes if "variants merged" in f["what"] or "both teams" in f["what"])
    fields = [(e, f) for e in events for f in (("success", "recipient", "end_location") if e["type"] == "pass" else ("xg", "goal") if e["type"] == "shot" else ())]
    missing = sum(f in e["imputed"] for e, f in fields)
    dims = {
        "validity": round(1 - len(log.quarantine) / in_scope, 3),
        "completeness": round(1 - missing / max(1, len(fields)), 3),
        "uniqueness": round(1 - dup / in_scope, 3),
        "consistency": round(1 - min(1, merged / in_scope), 3),
        "frame_confidence": {"declared": 1.0, "high": 1.0, "medium": 0.7, "low": 0.4}[frame_conf],
    }
    score = round(100 * (0.35 * dims["validity"] + 0.25 * dims["completeness"] + 0.15 * dims["uniqueness"]
                         + 0.15 * dims["consistency"] + 0.10 * dims["frame_confidence"]))
    imputed = Counter(f for e in events for f in e["imputed"])
    passes = [e for e in events if e["type"] == "pass" and e["success"]]
    caveats = []
    if passes and imputed["recipient"] / len(passes) > 0.05:
        caveats.append(f"{imputed['recipient']} receivers ({round(100 * imputed['recipient'] / len(passes))}% of completed passes) were "
                       "inferred from the next event: the passing network, fault lines and dependence are a little less certain.")
    if imputed["xg"]:
        caveats.append(f"{imputed['xg']} shots had no usable xG and use a location model: expected points added is rougher.")
    inferred_roles = sum(f["count"] for f in fixes if f["what"].startswith("role inferred"))
    if inferred_roles:
        caveats.append(f"{inferred_roles} positions were inferred from where players were on the ball: plan fit compares them with that position's norms.")
    if frame_conf in ("medium", "low"):
        caveats.append("The attack direction could not be fully confirmed: check that zones and progression look the right way round.")
    if len(log.quarantine) / in_scope > 0.05:
        caveats.append(f"{len(log.quarantine)} rows ({round(100 * len(log.quarantine) / in_scope)}%) were set aside: see quarantine.csv.")
    return {
        "pipeline_version": VERSION, "format": fmt, "encoding": encoding, "seconds": round(time.time() - t0, 2),
        "score": score, "rating": "good" if score >= 90 else "usable" if score >= 75 else "poor",
        "dimensions": dims,
        "rows": {"received": rows_in, "out_of_scope": sum(log.out_of_scope.values()), "quarantined": len(log.quarantine),
                 "clean_events": len(events), "events_with_imputed_values": imputed_events},
        "out_of_scope": dict(log.out_of_scope.most_common(10)),
        "fixes": sorted(fixes, key=lambda f: (("clean", "impute", "standardise").index(f["stage"]), -f["count"])),
        "imputed": dict(imputed), "notes": log.notes, "caveats": caveats,
        "quarantine_reasons": dict(Counter(q["reason"] for q in log.quarantine).most_common(10)),
        "teams": names, "coordinate_scale": scale,
    }


# ---------- store: the package the user gets back ----------
DICTIONARY = """# Data dictionary (Fault Lines clean events)

| Field | Meaning |
|---|---|
| t | seconds since kick-off (second half continues from 45:00) |
| minute | match minute (t // 60) |
| team | home or away |
| type | pass, shot or own_goal |
| player, recipient | who acted; who received a completed pass |
| x, y, end_x, end_y | 0-100 in the acting team's attacking direction: x = 0 its own goal line, y = 0 its left touchline |
| success | pass completed (shots: scored) |
| pressure | under pressure (false when the source did not say) |
| xg, goal | shots: expected goals and whether it went in |
| source_row | the row in your file this event came from |
| imputed | fields the pipeline filled in (see the quality report for how) |

Roles: GK, RB, RCB, LCB, LB, CDM, RCM, LCM, RW, ST, LW.
quarantine.csv lists every row that could not be used, with the reason. Nothing was dropped without a trace.
"""


def report_markdown(report):
    d = report["dimensions"]
    out = ["# Data quality report", "", f"**Quality {report['score']}/100 ({report['rating']})** · pipeline {report['pipeline_version']} · "
           f"{report['format']} · {report['rows']['received']} rows received, {report['rows']['clean_events']} clean events, "
           f"{report['rows']['quarantined']} quarantined, {report['rows']['out_of_scope']} out of scope", "",
           "| Dimension | Value |", "|---|---|"] + [f"| {k.replace('_', ' ')} | {v:.0%} |" for k, v in d.items()]
    out += ["", "## What the pipeline did", ""] + [f"* **{f['stage']}**: {f['what']} ({f['count']})" +
                                                   (f" e.g. {', '.join(map(str, f['examples']))}" if f["examples"] else "") for f in report["fixes"]]
    out += [f"* note: {n}" for n in report["notes"]]
    if report["quarantine_reasons"]:
        out += ["", "## Set aside (quarantine.csv)", ""] + [f"* {r}: {c}" for r, c in report["quarantine_reasons"].items()]
    if report["out_of_scope"]:
        out += ["", "## Out of scope (not errors)", ""] + [f"* {t}: {c}" for t, c in report["out_of_scope"].items()]
    if report["caveats"]:
        out += ["", "## What this means for the analysis", ""] + [f"* {c}" for c in report["caveats"]]
    return "\n".join(out) + "\n"


def to_csv(events, roles):
    cols = ["t", "minute", "team", "type", "player", "player_role", "recipient", "recipient_role", "x", "y", "end_x", "end_y",
            "success", "pressure", "xg", "goal", "source_row", "imputed"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for e in events:
        w.writerow({**e, "player_role": roles[e["team"]].get(e["player"], ""), "imputed": "|".join(e.get("imputed", [])),
                    "recipient_role": roles[e["team"]].get(e["recipient"], "") if e["recipient"] else "",
                    "xg": "" if e["xg"] is None else e["xg"]})
    return buf.getvalue()


def quarantine_csv(rows):
    keys = sorted({k for q in rows for k in q["raw"]})
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["source_row", "reason"] + keys)
    for q in rows:
        w.writerow([q["source_row"], q["reason"]] + [q["raw"].get(k, "") for k in keys])
    return buf.getvalue()


def package(lineups, events, quarantine, report, input_info, extra=None):
    """Zip for the user: clean data (CSV + JSON), line-ups, quarantine, quality report (JSON + Markdown), manifest,
    data dictionary, and any analysis outputs passed in extra ({filename: text})."""
    import zipfile
    roles = {s: lineups[s]["roles"] for s in ("home", "away")}
    manifest = {"pipeline_version": VERSION, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "input_sha256": input_info.get("sha256"), "input_characters": input_info.get("characters"),
                "format": report["format"], "encoding": report["encoding"], "rows": report["rows"], "quality_score": report["score"],
                "match_id": lineups["id"], "files": []}
    files = {
        "clean_events.csv": to_csv(events, roles),
        "clean_events.json": json.dumps({"teams": {s: {"name": lineups[s]["name"], "players": [{"name": n, "role": r} for n, r in roles[s].items()]}
                                                   for s in ("home", "away")}, "events": events}, indent=0),
        "quarantine.csv": quarantine_csv(quarantine),
        "quality_report.json": json.dumps(report, indent=1),
        "quality_report.md": report_markdown(report),
        "DATA_DICTIONARY.md": DICTIONARY,
        **(extra or {}),
    }
    manifest["files"] = [{"name": n, "sha256": hashlib.sha256(t.encode()).hexdigest(), "bytes": len(t.encode())} for n, t in files.items()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, t in files.items():
            z.writestr(n, t)
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return buf.getvalue()


def fingerprint(text):
    """What the manifest records about the input: enough to prove which file was used, without keeping it."""
    return {"sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest(), "characters": len(text)}
