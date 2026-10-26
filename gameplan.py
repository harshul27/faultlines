"""Game plan -> weighted player assessment: what a coach asks during a match. Who is doing their job, who is
carrying the team, is the plan working, where are we losing the ball, who comes off.

1. parse(): the coach's tactics (paragraphs, bullet points or a pasted sheet) become a plan of team principles and
   player/position duties. Each one is tied to measurable metrics with weights, and each player gets a relevance to
   the plan (1-5). Foundry (gpt-4.1-mini, JSON mode) when configured, keyword rules otherwise. normalise() validates
   either, and anything the browser sends back.
2. assess(): every player is rated on the metrics their role and the plan weight, against role norms from 40
   simulated league matches. The z-scores are scaled to the minutes played, so a 20th-minute read is not
   over-confident. Load (share of the team's on-ball actions) is set against the share the plan's relevance implies.
"""
import json
import math
import re
from functools import lru_cache

import numpy as np

from match import M_X, M_Y, TEAMS, simulate, squad
from network import fragility, pass_graph

METRICS = {
    "involvement": "completed passes made and received",
    "completion": "pass completion",
    "progressive": "completed passes moving the ball 10 m+ forward",
    "final_third": "completed passes into the final third",
    "box_entries": "completed passes into the opposition box",
    "key_passes": "passes that set up a shot",
    "xa": "expected assists (xG of the shots they set up)",
    "xg": "expected goals from their own shots",
    "shots": "shots",
    "press_resistance": "pass completion under pressure",
    "recoveries": "ball recoveries",
    "high_recoveries": "ball recoveries in the opposition half (pressing)",
    "long_passes": "passes longer than 30 m",
    "network_share": "share of teammates' passing connectivity that runs through them",
}
COUNTS = ("involvement", "progressive", "final_third", "box_entries", "key_passes", "xa", "xg", "shots",
          "recoveries", "high_recoveries", "long_passes")
RATIOS = {"completion": ("passes", "passes_ok"), "press_resistance": ("pressured", "pressured_ok")}
GROUPS = {"GK": ["GK"], "CB": ["RCB", "LCB"], "FB": ["RB", "LB"], "DM": ["CDM"], "CM": ["RCM", "LCM"],
          "W": ["RW", "LW"], "ST": ["ST"]}
GROUP_OF = {r: g for g, rs in GROUPS.items() for r in rs}
OUTFIELD = [r for r in GROUP_OF if r != "GK"]
# What each position is judged on before any game plan: the job description
ROLE_WEIGHTS = {
    "GK": {"completion": 2, "long_passes": 1, "press_resistance": 1},
    "CB": {"completion": 2, "progressive": 2, "recoveries": 2, "press_resistance": 1},
    "FB": {"progressive": 2, "final_third": 2, "box_entries": 1, "recoveries": 1, "completion": 1},
    "DM": {"completion": 2, "network_share": 2, "recoveries": 2, "press_resistance": 2, "progressive": 1},
    "CM": {"progressive": 2, "final_third": 2, "key_passes": 1, "press_resistance": 1, "recoveries": 1, "involvement": 1},
    "W": {"box_entries": 2, "key_passes": 2, "xa": 1, "xg": 1, "shots": 1, "high_recoveries": 1},
    "ST": {"xg": 3, "shots": 2, "key_passes": 1, "box_entries": 1, "high_recoveries": 1},
}


# ---------- metrics ----------
def raw_metrics(events, team, lo, hi):
    """Per-player counts for one team in [lo, hi) seconds. Events are in the acting team's attacking frame."""
    keys = COUNTS + ("passes", "passes_ok", "pressured", "pressured_ok")
    m = {p: dict.fromkeys(keys, 0.0) for p in TEAMS[team]["players"]}
    prev = None
    for i, e in enumerate(events):
        if e["t"] >= hi:
            break
        if e["t"] >= lo and e["team"] == team:
            r = m[e["player"]]
            if e["type"] == "shot":
                r["shots"] += 1
                r["xg"] += e["xg"]
            else:
                ok = e["success"]
                r["passes"] += 1
                r["passes_ok"] += ok
                if e["pressure"]:
                    r["pressured"] += 1
                    r["pressured_ok"] += ok
                r["long_passes"] += math.hypot((e["end_x"] - e["x"]) * M_X, (e["end_y"] - e["y"]) * M_Y) > 30
                if ok:
                    r["involvement"] += 1
                    m[e["recipient"]]["involvement"] += 1
                    r["progressive"] += e["end_x"] - e["x"] > 10
                    r["final_third"] += e["x"] < 66.7 <= e["end_x"]
                    in_box = lambda x, y: x >= 83 and 20.5 <= y <= 79.5
                    r["box_entries"] += in_box(e["end_x"], e["end_y"]) and not in_box(e["x"], e["y"])
                    nxt = events[i + 1] if i + 1 < len(events) else None
                    if nxt and nxt["type"] == "shot" and nxt["team"] == team and nxt["player"] == e["recipient"]:
                        r["key_passes"] += 1
                        r["xa"] += nxt["xg"]
            if prev and prev["team"] != team and prev["type"] == "pass" and not prev["success"]:
                r["recoveries"] += 1  # first touch after the opponent gave it away
                r["high_recoveries"] += e["x"] >= 50
        prev = e
    # Network share is a 15-minute quantity (as in the live view): average it over 15-minute chunks of the window
    chunks = [fragility(pass_graph(events, team, max(lo, end - 900), end)) for end in range(int(hi), int(lo), -900)
              if end - max(lo, end - 900) >= 300] or [fragility(pass_graph(events, team, lo, hi))]
    for p in m:
        m[p]["network_share"] = float(np.mean([c[p] for c in chunks]))
    return m


@lru_cache(maxsize=1)
def norms(n=40):
    """Role norms from n simulated league matches without the demo storyline (~6 s; the server warms it).
    Counts: mean and variance per full match. Ratios: pooled rate. Network share: mean and variance.
    ponytail: synthetic league norms; swap for real per-role benchmarks when connected to a provider feed."""
    rows = {g: [] for g in GROUPS}
    for s in range(1000, 1000 + n):
        ev = list(simulate(s, storyline=False))
        for t in TEAMS:
            roles = squad(t)
            for p, r in raw_metrics(ev, t, 0, 95 * 60).items():
                rows[GROUP_OF[roles[p]]].append(r)
    out = {}
    for g, rs in rows.items():
        o = {k: (float(np.mean([r[k] for r in rs])), float(np.var([r[k] for r in rs]))) for k in COUNTS + ("network_share",)}
        for k, (a, b) in RATIOS.items():
            o[k] = sum(r[b] for r in rs) / max(1, sum(r[a] for r in rs))
        out[g] = o
    return out


def _fmt(v):
    return f"{v:.2f}" if v and v < 1 else f"{v:.1f}" if v % 1 else f"{int(v)}"


def zscores(r, group, mins):
    """{metric: (z, plain-English evidence)} against the role norm, scaled to the minutes played; z clipped to +-3."""
    nm, f, out = norms()[group], mins / 95, {}
    for k in METRICS:
        if k in RATIOS:
            a, b = RATIOS[k]
            p0, n = nm[k], r[a]
            if n < 3:
                out[k] = (0.0, f"{METRICS[k]}: too few passes to judge")
                continue
            z = (r[b] / n - p0) / math.sqrt(p0 * (1 - p0) / n + 1e-6)
            out[k] = (z, f"{METRICS[k]} {round(100 * r[b] / n)}% vs role norm {round(100 * p0)}%")
        elif k == "network_share":
            mean, var = nm[k]
            z = (r[k] - mean) / math.sqrt(var + 1e-6) * min(1.0, mins / 45)  # short windows are noisy: shrink
            out[k] = (z, f"{METRICS[k]} {round(100 * r[k])}% vs role norm {round(100 * mean)}%")
        else:
            mean, var = nm[k]
            z = (r[k] - mean * f) / (math.sqrt(var * f) + 0.05)
            out[k] = (z, f"{METRICS[k]}: {_fmt(r[k])} vs {_fmt(round(mean * f, 2))} expected for the role by now")
        out[k] = (max(-3.0, min(3.0, out[k][0])), out[k][1])
    return out


# ---------- plans ----------
def weights(plan, player, role):
    """Role job description + every principle that applies to the role + the player's own duty (counts double)."""
    w = dict(ROLE_WEIGHTS[GROUP_OF[role]])
    for pr in plan["principles"]:
        if role in pr["roles"]:
            for k, v in pr["metrics"].items():
                w[k] = w.get(k, 0) + v
    for k, v in plan["duties"].get(player, {}).get("metrics", {}).items():
        w[k] = w.get(k, 0) + 2 * v
    return {k: v for k, v in w.items() if v > 0}


def _rating(zs, w):
    return round(max(1, min(99, 50 + 15 * sum(w[k] * zs[k][0] for k in w) / sum(w.values()))))


def assess(events, team, minute, plan):
    """Every player of one team against the plan: plan fit (match and last 15), form, load vs plan, flags, evidence."""
    t, roles = minute * 60, squad(team)
    full, last = raw_metrics(events, team, 0, t), raw_metrics(events, team, max(0, t - 900), t)
    before = raw_metrics(events, team, 0, t - 900) if minute >= 30 else None
    acts = {p: r["involvement"] + r["shots"] + r["recoveries"] for p, r in full.items()}
    rel = {p: plan["duties"].get(p, {}).get("relevance", 2 if roles[p] == "GK" else 3) for p in roles}
    total_acts, total_rel = sum(acts.values()) or 1, sum(rel.values())
    out = []
    for p, role in roles.items():
        g, w = GROUP_OF[role], weights(plan, p, role)
        zs = zscores(full[p], g, minute)
        fit, fit_last = _rating(zs, w), _rating(zscores(last[p], g, min(15, minute)), w)
        fit_before = _rating(zscores(before[p], g, minute - 15), w) if before else None
        load, expected = acts[p] / total_acts, rel[p] / total_rel
        ratio = load / expected
        form = ("no earlier window yet" if fit_before is None else "fading" if fit_last - fit_before <= -12
                else "rising" if fit_last - fit_before >= 12 else "steady")
        flags = [f for f, cond in (
            ("overloaded: the team leans on them more than the plan intends", ratio >= 1.5 and load >= 0.12),
            ("bypassed: a key role in the plan is not getting the ball", rel[p] >= 4 and ratio <= 0.6),
            ("off plan", fit < 40), ("on plan", fit >= 60), ("fading", form == "fading")) if cond]
        ranked = sorted(w, key=lambda k: w[k] * zs[k][0])
        out.append({
            "player": p, "role": role, "plan_relevance": f"{rel[p]}/5",
            "duty": plan["duties"].get(p, {}).get("duty") or f"{g} role in the team principles",
            "plan_fit": fit, "plan_fit_last_15": fit_last, "plan_fit_before_last_15": fit_before, "form": form,
            "load_share": f"{round(100 * load)}%", "plan_expected_share": f"{round(100 * expected)}%",
            "load_vs_plan": f"{ratio:.1f}x", "flags": flags,
            "strengths": [zs[k][1] for k in ranked[::-1][:2] if zs[k][0] > 0.5],
            "concerns": [zs[k][1] for k in ranked[:2] if zs[k][0] < -0.5],
            "network_share_last_15": f"{round(100 * last[p]['network_share'])}%",
            "weighted_metrics": {k: w[k] for k in sorted(w, key=lambda k: -w[k])}})
    return sorted(out, key=lambda r: -r["plan_fit"])


def execution(events, team, minute, plan):
    """Is each principle working? Team score = weighted z of the principle's metrics over the players it applies to."""
    full, roles, out = raw_metrics(events, team, 0, minute * 60), squad(team), []
    zs = {p: zscores(full[p], GROUP_OF[roles[p]], minute) for p in roles}
    for pr in plan["principles"]:
        ps = [p for p in roles if roles[p] in pr["roles"]]
        per = {p: sum(v * zs[p][k][0] for k, v in pr["metrics"].items()) / sum(pr["metrics"].values()) for p in ps}
        score = round(max(1, min(99, 50 + 15 * float(np.mean(list(per.values())))))) if per else 50
        best, worst = max(per, key=per.get), min(per, key=per.get)
        out.append({"principle": pr["text"], "score": score,
                    "status": "working" if score >= 58 else "not working" if score <= 42 else "mixed",
                    "measured_by": [METRICS[k] for k in pr["metrics"]],
                    "best_executed_by": best, "least_executed_by": worst})
    overall = round(np.mean([o["score"] for o in out])) if out else None
    return {"overall_plan_execution": overall, "principles": out}


def zones(events, team, minute, window=None):
    """Where a team progresses, loses and wins the ball (its own attacking frame: left = y < 33)."""
    lo, hi = (max(0, minute - window) * 60 if window else 0), minute * 60
    third = lambda x: "own third" if x < 33.3 else "middle third" if x < 66.7 else "final third"
    lane = lambda y: "left" if y < 33.3 else "centre" if y < 66.7 else "right"
    losses, wins, entries, shots = {}, {}, {"left": 0, "centre": 0, "right": 0}, {"left": 0, "centre": 0, "right": 0}
    prev = None
    for e in events:
        if e["t"] >= hi:
            break
        if e["t"] >= lo and e["team"] == team:
            if e["type"] == "shot":
                shots[lane(e["y"])] += 1
            elif not e["success"]:
                k = f"{third(e['x'])}, {lane(e['y'])}"
                losses[k] = losses.get(k, 0) + 1
            elif e["x"] < 66.7 <= e["end_x"]:
                entries[lane(e["end_y"])] += 1
            if prev and prev["team"] != team and prev["type"] == "pass" and not prev["success"]:
                k = f"{third(e['x'])}, {lane(e['y'])}"
                wins[k] = wins.get(k, 0) + 1
        prev = e
    top = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1])[:4])
    return {"team": TEAMS[team]["name"], "window": f"last {window} min" if window else "whole match so far",
            "final_third_entries_by_channel": entries, "shots_by_channel": shots,
            "most_ball_losses": top(losses), "most_ball_wins": top(wins)}


# ---------- parsing ----------
KEYWORDS = [  # phrase -> metrics it is measured by (weight 1-3)
    (r"\bpress(es|ing)?\b|counter-?press|win (it|the ball) back|hunt|high line|aggressive", {"high_recoveries": 3, "recoveries": 2}),
    (r"possession|keep (the )?ball|build[- ]?up|patient|retain|short pass|tiki|control the game", {"completion": 3, "involvement": 2, "press_resistance": 1}),
    (r"counter(?!-?press)|direct|vertical|transition|in behind|over the top|long ball|quick(ly)? forward", {"progressive": 3, "long_passes": 2, "final_third": 1}),
    (r"width|wide|overlap|stretch|cross", {"box_entries": 2, "final_third": 2}),
    (r"creat|chance|key pass|assist|unlock|final ball|playmak", {"key_passes": 3, "xa": 2}),
    (r"shoot|shot|finish|goal|clinical|score", {"xg": 3, "shots": 2}),
    (r"progress|break (the )?lines|between the lines|forward pass|carry", {"progressive": 3, "final_third": 2}),
    (r"under pressure|press[- ]resist|compos|play out|calm", {"press_resistance": 3, "completion": 1}),
    (r"secure|careless|cheap|don'?t lose|protect the ball|safe", {"completion": 2, "press_resistance": 2}),
    (r"tackl|win the ball|recover|screen|shield|ball[- ]win|intercept|defend", {"recoveries": 3}),
    (r"link|connect|hub|pivot|dictate|tempo|metronome|through (him|her|them)|orchestrat|distribut", {"network_share": 3, "involvement": 2}),
    (r"switch|diagonal|long pass|launch", {"long_passes": 2}),
    (r"into the box|penalty area|cut[- ]?back|feed the striker", {"box_entries": 3}),
]
POSITIONS = [
    (r"goal-?keeper|keeper|\bgk\b", ["GK"]),
    (r"cent(re|er)[- ]?backs?|\bcbs?\b|central defenders?|back two", ["RCB", "LCB"]),
    (r"right[- ]back", ["RB"]), (r"left[- ]back", ["LB"]),
    (r"full[- ]?backs?|wing[- ]?backs?", ["RB", "LB"]),
    (r"back (four|line)|defen(ce|se|ders)\b", ["RB", "RCB", "LCB", "LB"]),
    (r"holding mid|defensive mid|\bdm\b|#6|\bsix\b|anchor", ["CDM"]),
    (r"cent(re|ral)[- ]mid|box[- ]to[- ]box|#8|\beights?\b", ["RCM", "LCM"]),
    (r"midfield(ers)?", ["CDM", "RCM", "LCM"]),
    (r"right wing(er)?", ["RW"]), (r"left wing(er)?", ["LW"]),
    (r"wingers?|wide (men|players|forwards)", ["RW", "LW"]),
    (r"striker|cent(re|er)[- ]forward|#9|number 9|target man", ["ST"]),
    (r"front (three|line)|forwards|attackers", ["RW", "ST", "LW"]),
]
CELL = r"(^|[,|\t;]\s*)({})\s*($|[,|\t;])"  # a priority column in a pasted sheet
EMPHASIS = [(r"\b(key|crucial|vital|focal|everything|top priority|primary|main|core|central to)\b|" + CELL.format("high|5"), 5),
            (r"\b(important|should|must)\b|" + CELL.format("medium|4"), 4),
            (r"\b(support|cover|rotate|backup|occasional)\b|" + CELL.format("low|1|2"), 2)]


def parse_rules(text, team):
    """Keyword parser: one instruction per sentence, line or sheet row. Never smart, always available."""
    roles, principles, duties = squad(team), [], []
    for s in re.split(r"(?<=[.!?;])\s+|\n+", text):
        s = s.strip(" -•*\t,|")
        low = s.lower()
        if len(s) < 4:
            continue
        metrics = {}
        for rx, ms in KEYWORDS:
            if re.search(rx, low):
                for k, v in ms.items():
                    metrics[k] = max(metrics.get(k, 0), v)
        named = [p for p in roles if re.search(rf"\b{re.escape(p.split()[-1].lower())}\b", low)]
        positions = sorted({r for rx, rs in POSITIONS if re.search(rx, low) for r in rs})
        if not metrics and not named and not positions:
            continue
        rel = next((v for rx, v in EMPHASIS if re.search(rx, low)), 3)
        if named or positions:
            duties += [{"target": t, "duty": s, "metrics": metrics, "relevance": rel} for t in named or positions]
        else:
            principles.append({"text": s, "metrics": metrics, "roles": OUTFIELD})
    first = re.split(r"(?<=[.!?])\s+|\n", text.strip())[0][:200]
    return {"summary": first, "principles": principles, "duties": duties}


def normalise(raw, team):
    """Validate a plan from the model, the rules or the browser: known metrics only (weights 0-3), relevance 1-5,
    targets resolved to this team's players or roles. Unknown things are dropped, not trusted."""
    roles = squad(team)
    by_role = {r: p for p, r in roles.items()}

    def mets(d):
        return {k: round(max(0.0, min(3.0, float(v))), 1) for k, v in (d or {}).items()
                if k in METRICS and isinstance(v, (int, float)) and v > 0} if isinstance(d, dict) else {}

    def resolve(target):
        t = str(target or "").strip()
        if t.upper() in by_role:
            return [by_role[t.upper()]]
        if t.upper() in GROUPS:
            return [by_role[r] for r in GROUPS[t.upper()]]
        return [p for p in roles if t.lower() in (p.lower(), p.split()[-1].lower())][:1]

    principles = []
    for pr in (raw.get("principles") or [])[:12]:
        if not isinstance(pr, dict) or not (m := mets(pr.get("metrics"))):
            continue
        rs = [r for x in (pr.get("roles") or OUTFIELD) for r in (GROUPS.get(str(x).upper()) or [str(x).upper()]) if r in by_role]
        principles.append({"text": str(pr.get("text", ""))[:200], "metrics": m, "roles": sorted(set(rs)) or OUTFIELD})
    raw_duties = raw.get("duties") or []
    if isinstance(raw_duties, dict):  # already normalised (sent back by the page)
        raw_duties = [{"target": p, **d} for p, d in raw_duties.items() if isinstance(d, dict)]
    duties = {}
    for d in raw_duties[:40]:
        if not isinstance(d, dict):
            continue
        for p in resolve(d.get("target") or d.get("player")):
            cur = duties.setdefault(p, {"duty": "", "metrics": {}, "relevance": 1})
            text = str(d.get("duty", ""))[:200]
            if text and text not in cur["duty"]:
                cur["duty"] = f"{cur['duty']} {text}".strip()[:400]
            for k, v in mets(d.get("metrics")).items():
                cur["metrics"][k] = max(cur["metrics"].get(k, 0), v)
            try:
                cur["relevance"] = max(cur["relevance"], int(round(max(1, min(5, float(d.get("relevance", 3)))))))
            except (TypeError, ValueError):
                cur["relevance"] = max(cur["relevance"], 3)
    return {"team": team, "summary": str(raw.get("summary", ""))[:300], "principles": principles, "duties": duties}


PARSE_PROMPT = """You turn a football coach's game plan into JSON for live match analysis.
Team: {team}. Squad (player: role): {squad}.
Role codes: GK, RB, RCB, LCB, LB, CDM, RCM, LCM, RW, ST, LW. Group codes: CB, FB, CM, W (both sides of the pitch).
Metrics (use only these keys): {metrics}
Return JSON: {{"summary": one sentence, "principles": [{{"text": the instruction, "metrics": {{metric: weight 1-3}}, "roles": [role codes it applies to]}}],
 "duties": [{{"target": player name or role/group code, "duty": what they must do, "metrics": {{metric: weight 1-3}}, "relevance": 1-5}}]}}
Team-wide ideas are principles; instructions for a named player or position are duties. Relevance = how central that
player is to the plan (5 = the plan runs through them). Weight 3 = the core of the instruction. Instructions about
the opponent become principles for our players (e.g. "stop their winger" -> our full-back's recoveries)."""


def parse(text, team):
    """Coach's text -> (normalised plan, source)."""
    import ai
    why = "AI not configured"
    if ai.enabled():
        try:
            r = ai.client().with_options(timeout=50).chat.completions.create(  # long plans take ~20 s
                model=ai.deployment("chat"), temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": PARSE_PROMPT.format(
                    team=TEAMS[team]["name"], squad=", ".join(f"{p}: {r}" for p, r in squad(team).items()),
                    metrics="; ".join(f"{k} = {v}" for k, v in METRICS.items()))},
                          {"role": "user", "content": text}])
            plan = normalise(json.loads(r.choices[0].message.content), team)
            if plan["principles"] or plan["duties"]:
                return plan, "ai"
            why = "AI returned no usable instructions"
        except Exception as e:  # a model outage must not block the coach
            why = f"AI unavailable: {type(e).__name__}"
    return normalise(parse_rules(text, team), team), f"rules ({why})"


DEFAULT_PLANS = {
    "home": ("Control the game through patient possession and short passing. Everything goes through Luca Ferrand at the "
             "base of midfield: he dictates the tempo and connects the back line to the attack. Centre-backs must play out "
             "under pressure and never give the ball away cheaply. Full-backs overlap to give width and deliver into the box. "
             "Wingers create chances and counter-press to win the ball back high. Theo Ashgrove is the focal point: "
             "get shots away in the box."),
    "away": ("Stay compact and hit Northbridge on the counter: win the ball in midfield and go direct in behind. "
             "Midfielders must tackle and screen the back four. Wingers break forward quickly in transition. "
             "Mateo Lindqvist is the key outlet: finish the chances. Full-backs support and stay safe on the ball."),
}


@lru_cache(maxsize=2)
def _default(team):
    return json.dumps(normalise(parse_rules(DEFAULT_PLANS[team], team), team))


def default_plan(team):
    return json.loads(_default(team))  # a fresh copy, so callers can't mutate the cache
