"""Validation of the judgement-based parts: fault lines, plan fit, flags, cues, expected points added, the plan reader
and the agents. Counting metrics (passes, zones...) need no such test; realism of the data is in validate.py.

    python evaluate.py          # offline tests (~5 min)  -> reports/evaluation.md + reports/evaluation.json
    python evaluate.py --ai     # + plan reader and agents on Microsoft Foundry (az login, AZURE_OPENAI_ENDPOINT)

Pass bars are fixed in BARS before any result is seen. Every test also says what synthetic data can and cannot show:
a pass here means "behaves sensibly in a world we control", not "true on real football".
"""
import argparse
import asyncio
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import kendalltau, spearmanr

import gameplan as gp
from match import TEAMS, simulate, squad
from network import _dist_matrix, _inv_dist, expected_points, pass_graph, points_added, team_view
from stream import LiveEngine

ROOT = Path(__file__).parent
BARS = {
    "fragility_ci_low": 0.0,          # fragility predicts the damage of losing a player: 95% CI above 0
    "fragility_within_role_ci_low": 0.0,  # ...beyond what the position alone explains
    "fragility_vs_passes": 0.0,       # ...and at least as well as a plain pass count (rho difference >= 0)
    "weights_tau_median": 0.8,        # squad ranking survives random +-50% changes to every weight
    "weights_tau_p5": 0.6,
    "early_agree_30": 0.65,           # the 30' read is on the same side of 50 as the full-match read
    "early_agree_45": 0.75,
    "split_half_rho": 0.4,            # first-half vs second-half plan fit of the same player
    "alerts_per_check": (0.2, 2.5),   # mean alert flags (overloaded / bypassed / fading) per team per check
    "fading_ci_high": 0.0,            # 'fading' predicts a lower next-15 plan fit beyond the current one
    "cues_median": (2, 10),           # non-goal cues per match
    "cues_max": 20,
    "xpa_rho": 0.3,                   # a team's xPA margin in a match tracks the points it won
    "reader_ai": 0.9, "reader_rules": 0.7,  # share of plan-reader checks passed
    "agent_tools": 0.9, "agent_fallback_max": 0.1, "agent_consistency": 0.8, "agent_judge": 4.0,
    # Data pipeline on realistic noise (noise.py), level 1. Fixed after a one-match smoke test, before the 20-match run.
    "pipe_events": 0.99, "pipe_faultline": 0.9, "pipe_frag_mae": 0.03, "pipe_planfit_rho": 0.85,
    "pipe_quarantine_recall": 0.95, "pipe_false_quarantine": 0.01, "pipe_score_usable": 75,
}
RNG = np.random.default_rng(0)


def boot(stat, groups, n=1000):
    """95% CI by resampling whole matches, so players from the same match are not treated as independent."""
    keys, vals = list(groups), []
    for _ in range(n):
        rows = [r for i in RNG.integers(0, len(keys), len(keys)) for r in groups[keys[i]]]
        vals.append(stat(rows))
    return float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


def result(name, passed, bar, value, note, scope):
    return {"test": name, "passed": bool(passed), "bar": bar, "result": value, "note": note, "what_it_shows": scope}


# ---------- fault lines: does fragility predict the damage of losing a player? ----------
def _prog(ev, team, lo, hi):
    return sum(e["team"] == team and e["type"] == "pass" and e["success"] and e["end_x"] - e["x"] > 10 and lo <= e["t"] < hi for e in ev)


def _connectivity(ev, team, others):
    """The other players' pairwise passing connectivity 60-95', paths through anyone allowed: exactly the quantity
    fragility predicts the loss of (network.fragility), measured on what actually happened."""
    G = pass_graph(ev, team, 3600, 5700)
    nodes = list(G)
    idx = [nodes.index(q) for q in others]
    return _inv_dist(_dist_matrix(G))[np.ix_(idx, idx)].sum()


REPLAYS = 4  # continuations per pair, with common random numbers (both arms share each replay's random stream)


def test_fragility(seeds):
    """Paired experiment: the same match with and without one outfield player from 60' (red card / injury, no sub).
    The generator has no fault-line storyline here, so nothing tells it which players matter: fragility has to find it.
    Predictors use only the first hour, which is identical in every arm."""
    groups = defaultdict(list)  # seed -> rows (role, fragility, passes, betweenness, progression lost)
    for s in seeds:
        fork = lambda k: (3600, 10 * s + k)
        bases = [list(simulate(s, storyline=False, fork=fork(k))) for k in range(REPLAYS)]
        for team in TEAMS:
            v, b = team_view(bases[0], team, 60), np.mean([_prog(x, team, 3600, 5700) for x in bases])
            for p in v["players"]:
                if p["role"] != "GK":
                    arms = [list(simulate(s, storyline=False, absent=(team, p["name"], 3600), fork=fork(k))) for k in range(REPLAYS)]
                    rem = np.mean([_prog(x, team, 3600, 5700) for x in arms])
                    others = [q for q in TEAMS[team]["players"] if q != p["name"]]
                    lost = 1 - np.mean([_connectivity(x, team, others) for x in arms]) / np.mean([_connectivity(x, team, others) for x in bases])
                    groups[s].append((p["role"], p["fragility"], p["passes"], p["betweenness"], b - rem, lost))

    def rho(rows, k):
        return spearmanr([r[k] for r in rows], [r[4] for r in rows])[0]

    def within(rows, k):  # centre predictor and outcome by position: what is left is not "he's a midfielder"
        means = {role: (np.mean([r[k] for r in rows if r[0] == role]), np.mean([r[4] for r in rows if r[0] == role]))
                 for role in {r[0] for r in rows}}
        return spearmanr([r[k] - means[r[0]][0] for r in rows], [r[4] - means[r[0]][1] for r in rows])[0]

    rows = [r for g in groups.values() for r in g]
    out = {name: {"rho": round(rho(rows, k), 3), "ci": [round(x, 3) for x in boot(lambda rs, k=k: rho(rs, k), groups)],
                  "within_role": round(within(rows, k), 3), "within_ci": [round(x, 3) for x in boot(lambda rs, k=k: within(rs, k), groups)]}
           for name, k in (("fragility", 1), ("passes", 2), ("betweenness", 3))}
    diff_ci = boot(lambda rs: rho(rs, 1) - rho(rs, 2), groups)
    out["fragility_minus_passes_ci"] = [round(x, 3) for x in diff_ci]
    out["pairs"], out["mean_progression_lost"] = len(rows), round(float(np.mean([r[4] for r in rows])), 2)
    # Added after run 2: the generator lets 10 men pass as much as 11 (mean progression lost ~0), so the outcome above
    # cannot separate any predictor. Fragility's literal claim is about connectivity: does the remaining team's
    # realised passing spread fall more after losing a high-fragility player, or does play re-route around the gap?
    net = lambda rs: spearmanr([r[1] for r in rs], [r[5] for r in rs])[0]
    out["connectivity"] = {"rho": round(net(rows), 3), "ci": [round(x, 3) for x in boot(net, groups)],
                           "mean_spread_lost": round(float(np.mean([r[5] for r in rows])), 3)}
    f, c = out["fragility"], out["connectivity"]
    inconclusive = (f" Mean progression lost per removal: {out['mean_progression_lost']} passes: the generator does not make "
                    "losing a player costly (10 men re-route the ball), so this outcome cannot test any predictor.")
    return [
        result("Fault lines: predicted connectivity loss happens (added after run 2)", c["ci"][0] > 0,
               "95% CI of Spearman rho(fragility, realised spread lost) above 0",
               f"rho {c['rho']} (CI {c['ci'][0]} to {c['ci'][1]}); mean realised spread lost {c['mean_spread_lost']:.1%}",
               "Fragility is a static what-if on the network; this checks the remaining players' actual passing network after the removal.",
               "Tests the claim on its own terms in a system that re-routes play. Whether lost connectivity costs results is the real-data question."),
        result("Fault lines: fragility predicts progression lost when a player goes off", f["ci"][0] > BARS["fragility_ci_low"],
               "95% CI of Spearman rho above 0", f"rho {f['rho']} (CI {f['ci'][0]} to {f['ci'][1]}), {len(rows)} paired removals",
               "Each pair: same match, same first hour, player removed at 60'. Outcome: progressive passes 60-95' lost." + inconclusive,
               "The measure finds structurally important players in a passing system with no planted answer. Real football adds tactics, substitutes and opponents adapting: needs the injury/red-card study."),
        result("Fault lines: beyond position", f["within_ci"][0] > BARS["fragility_within_role_ci_low"],
               "95% CI of within-position rho above 0", f"rho {f['within_role']} (CI {f['within_ci'][0]} to {f['within_ci'][1]})",
               "Predictor and outcome centred by position, so 'midfielders matter more' does not count.", "Same as above."),
        result("Fault lines: better than counting passes", diff_ci[0] >= BARS["fragility_vs_passes"],
               "95% CI of rho(fragility) - rho(passes) not below 0",
               f"fragility {f['rho']} vs passes {out['passes']['rho']} vs betweenness {out['betweenness']['rho']} (difference CI {out['fragility_minus_passes_ci'][0]} to {out['fragility_minus_passes_ci'][1]})",
               "If a plain pass count predicts as well, fragility's extra complexity is not earning its place.", "Same as above."),
    ], out


# ---------- plan fit, flags ----------
def _assessments(seeds, storyline, minutes=(20, 30, 45, 60, 75, 90)):
    out = []
    for s in seeds:
        ev = list(simulate(s, storyline=storyline))
        for team in TEAMS:
            plan = gp.default_plan(team)
            out.append((s, team, ev, {m: {r["player"]: r for r in gp.assess(ev, team, m, plan)} for m in minutes}))
    return out


def test_plan_fit(neutral):
    tests, raw = [], {}
    # 1. Weight sensitivity: random +-50% on every weight, squad ranking compared with Kendall's tau
    taus = []
    for s, team, ev, _ in neutral[:40]:
        plan, roles = gp.default_plan(team), squad(team)
        m = gp.raw_metrics(ev, team, 0, 90 * 60)
        zs = {p: gp.zscores(m[p], gp.GROUP_OF[roles[p]], 90) for p in roles}
        w = {p: gp.weights(plan, p, roles[p]) for p in roles}
        base = [gp._rating(zs[p], w[p]) for p in roles]
        for _ in range(100):
            taus.append(kendalltau(base, [gp._rating(zs[p], {k: v * RNG.uniform(0.5, 1.5) for k, v in w[p].items()}) for p in roles])[0])
    med, p5 = float(np.median(taus)), float(np.percentile(taus, 5))
    raw["weights_tau"] = {"median": round(med, 3), "p5": round(p5, 3), "n": len(taus)}
    tests.append(result("Plan fit: robust to the exact weights", med >= BARS["weights_tau_median"] and p5 >= BARS["weights_tau_p5"],
                        f"median tau >= {BARS['weights_tau_median']}, 5th percentile >= {BARS['weights_tau_p5']}",
                        f"median {med:.2f}, 5th percentile {p5:.2f} ({len(taus)} perturbations)",
                        "Every weight multiplied by a random 0.5-1.5. A low tau would mean the ranking is mostly my weight choices.",
                        "Holds for any data: this is a property of the scoring, not of football."))
    # 2. Early reads: is the 20'/30'/45' read on the same side of 50 as the full-match read?
    agree = {}
    for m in (20, 30, 45, 60):
        pairs = [(a[m][p]["plan_fit"], a[90][p]["plan_fit"]) for _, _, _, a in neutral for p in a[90] if a[90][p]["plan_fit"] != 50]
        agree[m] = {"same_side": round(float(np.mean([(x > 50) == (y > 50) for x, y in pairs])), 3),
                    "rho": round(spearmanr(*zip(*pairs))[0], 3)}
    raw["early_reads"] = agree
    tests.append(result("Plan fit: early reads hold up", agree[30]["same_side"] >= BARS["early_agree_30"] and agree[45]["same_side"] >= BARS["early_agree_45"],
                        f"same side of 50 as the 90' read: 30' >= {BARS['early_agree_30']:.0%}, 45' >= {BARS['early_agree_45']:.0%}",
                        ", ".join(f"{m}': {v['same_side']:.0%} (rho {v['rho']})" for m, v in agree.items()),
                        "Checks that the minutes adjustment keeps early verdicts from flipping later.",
                        "Real players vary more between halves (tactics, fatigue, game state), so expect lower agreement on real data."))
    # 3. Split-half reliability: first half vs second half of the same match, same player
    pairs = []
    for s, team, ev, _ in neutral:
        plan, roles = gp.default_plan(team), squad(team)
        h1, h2 = gp.raw_metrics(ev, team, 0, 2700), gp.raw_metrics(ev, team, 2700, 5400)
        for p in roles:
            w, g = gp.weights(plan, p, roles[p]), gp.GROUP_OF[roles[p]]
            pairs.append((gp._rating(gp.zscores(h1[p], g, 45), w), gp._rating(gp.zscores(h2[p], g, 45), w)))
    rho = spearmanr(*zip(*pairs))[0]
    raw["split_half_rho"] = round(rho, 3)
    tests.append(result("Plan fit: reliable within a match", rho >= BARS["split_half_rho"], f"first vs second half rho >= {BARS['split_half_rho']}",
                        f"rho {rho:.2f} over {len(pairs)} player-halves",
                        "A score that changes completely between halves is measuring noise, not the player.",
                        "In synthetic matches players have no lasting individual quality beyond position and team strength, so this is a floor; real players should be more consistent."))
    return tests, raw


def test_flags(neutral, story):
    tests, raw = [], {}
    alerts = ("overloaded", "bypassed", "fading")
    for label, data in (("neutral", neutral), ("storyline", story)):
        per = [sum(any(f.startswith(k) for k in alerts) for r in a[m].values() for f in r["flags"]) for _, _, _, a in data for m in (45, 60, 75, 90)]
        raw[f"alerts_per_check_{label}"] = round(float(np.mean(per)), 2)
    lo, hi = BARS["alerts_per_check"]
    tests.append(result("Flags: not too many, not too few", lo <= raw["alerts_per_check_neutral"] <= hi,
                        f"{lo}-{hi} alert flags per team per check", f"neutral matches {raw['alerts_per_check_neutral']}, storyline matches {raw['alerts_per_check_storyline']}",
                        "Too many flags and coaches stop reading them.", "Rates will shift on real data; retune the 1.5x / 0.6x / 12-point thresholds there."))
    for label, data in (("neutral", neutral), ("storyline", story)):
        groups = defaultdict(list)
        for s, team, _, a in data:
            for m in (45, 60, 75):
                for p, r in a[m].items():
                    groups[(s, team)].append((r["plan_fit_last_15"], r["form"] == "fading", a[m + 15][p]["plan_fit_last_15"]))

        def coef(rows):
            X = np.array([[1, r[0], r[1]] for r in rows], float)
            return np.linalg.lstsq(X, np.array([r[2] for r in rows], float), rcond=None)[0][2]
        rows = [r for g in groups.values() for r in g]
        raw[f"fading_{label}"] = {"coef": round(float(coef(rows)), 2), "ci": [round(x, 2) for x in boot(coef, groups, 500)],
                                  "flagged": int(sum(r[1] for r in rows)), "rows": len(rows)}
    fn, fs = raw["fading_neutral"], raw["fading_storyline"]
    tests.append(result("Flags: 'fading' predicts the next 15 minutes", fn["ci"][1] < BARS["fading_ci_high"],
                        "95% CI of the fading effect below 0, controlling for the current 15-minute fit",
                        f"neutral: {fn['coef']} (CI {fn['ci'][0]} to {fn['ci'][1]}, {fn['flagged']} flags); storyline: {fs['coef']} (CI {fs['ci'][0]} to {fs['ci'][1]}, {fs['flagged']} flags)",
                        "Next-15 fit regressed on current fit + the flag. Without the flag adding information, it is regression to the mean dressed up as insight. "
                        f"Reading: where a real decline exists (storyline: man-marked from 60') the flag carries information "
                        f"({fs['coef']}); in ordinary play flagged players bounce back ({fn['coef']:+}), so a single 'fading' flag should be read as a dip, not a trend.",
                        "The generator has no fatigue, so in neutral matches nothing real can fade; the storyline match (man-marking from 60') is the only true decline. Real test: tracking data (high-speed running) and coach substitutions."))
    return tests, raw


def test_cues(seeds):
    counts = {}
    for label, story in (("neutral", False), ("storyline", True)):
        c = []
        for s in seeds:
            eng = LiveEngine()
            for e in simulate(s, storyline=story):
                eng.ingest(e)
            c.append(sum(q["cue"] != "goal" for q in eng.cues))
        counts[label] = {"median": float(np.median(c)), "max": int(max(c)), "min": int(min(c))}
    lo, hi = BARS["cues_median"]
    n = counts["neutral"]
    return [result("Key-moment cues: informative, not spammy", lo <= n["median"] <= hi and n["max"] <= BARS["cues_max"],
                   f"median {lo}-{hi} non-goal cues per match, none above {BARS['cues_max']}",
                   f"neutral median {n['median']:.0f} (range {n['min']}-{n['max']}); storyline median {counts['storyline']['median']:.0f}",
                   "Counts fault-line shifts, alerts and narrowing per match.",
                   "Precision (are they the moments an analyst would pick?) needs analyst labels on real matches.")], counts


def test_xpa(seeds):
    """Sanity: a team's expected points added margin in a match should track the points it actually won."""
    xs, xg_m, pts = [], [], []
    for s in seeds:
        ev = list(simulate(s, storyline=False))
        sc, val, xg = {"home": 0, "away": 0}, {"home": 0.0, "away": 0.0}, {"home": 0.0, "away": 0.0}
        for e in ev:
            if e["type"] == "shot":
                o = "away" if e["team"] == "home" else "home"
                left, d = max(0.0, 95 - e["t"] / 60), sc[e["team"]] - sc[o]
                val[e["team"]] += e["xg"] * (expected_points(d + 1, left, e["team"]) - expected_points(d, left, e["team"]))
                xg[e["team"]] += e["xg"]
            if e["goal"]:
                sc[e["team"]] += 1
        p = 3 if sc["home"] > sc["away"] else 1 if sc["home"] == sc["away"] else 0
        xs.append(val["home"] - val["away"]); xg_m.append(xg["home"] - xg["away"]); pts.append(p)
    r, r_xg = spearmanr(xs, pts)[0], spearmanr(xg_m, pts)[0]
    tests = [result("Expected points added tracks results", r >= BARS["xpa_rho"], f"rho(xPA margin, points) >= {BARS['xpa_rho']}",
                    f"rho {r:.2f} (xG margin: {r_xg:.2f}) over {len(seeds)} matches",
                    "Run 1 finding: xPA weights chances by what a goal is worth then, so a trailing team's chances count more "
                    "and the margin does not track who won. It measures decisive moments, not dominance: use the xG margin for dominance.",
                    "By construction it inherits xG quality; on real data use the provider's xG.")]
    # Added after run 1 (bar set before running it): as a 'clutch' measure, does xPA beat the old hand-weighted
    # clutch points at ranking players' realised decisive contributions (actual goals and assists x points at stake)?
    rows = []
    for s in seeds:
        ev = list(simulate(s, storyline=False))
        for team in TEAMS:
            xpa, real, old = points_added(ev, team, 1e9), _realised(ev, team), _old_clutch(ev, team)
            rows += [(xpa.get(p, 0), old.get(p, 0), real.get(p, 0)) for p in TEAMS[team]["players"]]
    r_new, r_old = spearmanr([r[0] for r in rows], [r[2] for r in rows])[0], spearmanr([r[1] for r in rows], [r[2] for r in rows])[0]
    tests.append(result("Expected points added beats old clutch points (added after run 1)", r_new > r_old,
                        "rho(xPA, realised points added) > rho(old clutch points, realised)",
                        f"xPA {r_new:.2f} vs clutch points {r_old:.2f} over {len(rows)} player-matches",
                        "Realised points added = goals (scorer and assister) x the points a goal was worth at that moment.",
                        "In synthetic data xG is the true scoring probability, which favours xPA; real xG is an estimate."))
    return tests, {"rho": r, "rho_xg": r_xg, "rho_xpa_realised": r_new, "rho_clutch_realised": r_old}


def _realised(ev, team):
    """Actual points added: each goal's value at that moment, to the scorer and the player who passed to them."""
    pts, sc, other = defaultdict(float), {"home": 0, "away": 0}, "away" if team == "home" else "home"
    for i, e in enumerate(ev):
        if e["goal"] and e["team"] == team:
            left, d = max(0.0, 95 - e["t"] / 60), sc[team] - sc[other]
            v = expected_points(d + 1, left, team) - expected_points(d, left, team)
            pts[e["player"]] += v
            prev = ev[i - 1] if i else None
            if prev and prev["team"] == team and prev["type"] == "pass" and prev["success"] and prev["recipient"] == e["player"]:
                pts[prev["player"]] += v
        if e["goal"]:
            sc[e["team"]] += 1
    return pts


def _old_clutch(ev, team):
    """The replaced metric, kept here only for the comparison: actions from 75' while not winning, hand-weighted."""
    pts, s, other = defaultdict(float), {"home": 0, "away": 0}, "away" if team == "home" else "home"
    for e in ev:
        if e["team"] == team and e["minute"] >= 75 and s[team] <= s[other]:
            if e["type"] == "pass" and e["success"]:
                pts[e["player"]] += 1 + (2 if e["end_x"] - e["x"] > 10 else 0)
            elif e["type"] == "shot":
                pts[e["player"]] += 3 + (10 if e["goal"] else 0)
        if e["goal"]:
            s[e["team"]] += 1
    return pts


# ---------- calibration (separate seeds; prints the thresholds to paste into stream.py and gameplan.py) ----------
def calibrate(seeds):
    """Thresholds from ordinary matches (no storyline, neutral plan): cue types fire in about half of team-matches;
    player flags for about 3% of player-checks."""
    neutral = lambda team: {"team": team, "summary": "", "principles": [], "duties": {}}
    peak, narrow, fade, ratio = [], [], [], []
    for s in seeds:
        ev = list(simulate(s, storyline=False))
        for team in TEAMS:
            views = {m: team_view(ev, team, m) for m in range(15, 96)}
            peak.append(max(v["spof"]["fragility"] for v in views.values()))
            narrow.append(min(v["efficiency"] / v["efficiency_prev"] - 1 for m, v in views.items() if m >= 30 and v["efficiency_prev"]))
            for m in (30, 45, 60, 75, 90):
                for r in gp.assess(ev, team, m, neutral(team)):
                    if r["plan_fit_before_last_15"] is not None:
                        fade.append(r["plan_fit_last_15"] - r["plan_fit_before_last_15"])
                    if r["role"] != "GK":
                        ratio.append(r["load_ratio"])
    out = {"ALERT": round(float(np.median(peak)), 2), "NARROWING": round(float(np.median(narrow)), 2),
           "FADE": int(round(-np.percentile(fade, 3))), "OVERLOAD": round(float(np.percentile(ratio, 97)), 2),
           "BYPASS": round(float(np.percentile(ratio, 3)), 2)}
    print(json.dumps(out))
    return out


# ---------- data pipeline: robustness to realistic noise ----------
def test_pipeline(seeds):
    """Each clean synthetic match is turned into a realistic messy provider file (noise.py: fixed frame and 120x80
    scale, per-half clocks, club names as labels, renamed columns, decimal commas, duplicates, disorder, missing
    receivers / outcomes / end points / xG, name variants, missing roles, out-of-scope and broken rows, markup), cleaned,
    and analysed. The clean match is the truth."""
    import noise
    import sources
    from match import DEMO_TEAMS, using
    from network import score_at
    teams = {s: {"name": DEMO_TEAMS[s]["name"], "roles": squad(s)} for s in ("home", "away")}
    out = {}
    for level in (1.0, 2.0):
        m = defaultdict(list)
        for s in seeds:
            ev = list(simulate(s))
            clean = sources.parse(sources.to_csv(ev, teams))  # same pipeline, clean input: the comparison baseline
            text, truth = noise.noisy_csv(ev, teams, seed=s, level=level)
            try:
                lu, ne, q, rep = sources.parse(text)
            except sources.DataError:
                m["gate_failed"].append(1)
                continue
            m["gate_failed"].append(0)
            m["events"].append(len(ne) / len(ev))
            m["score_same"].append(score_at(ne, 1e9) == score_at(ev, 1e9))
            m["quality"].append(rep["score"])
            qrows = {x["source_row"] for x in q}
            m["recall"].append(len(truth["broken_rows"] & qrows) / max(1, len(truth["broken_rows"])))
            m["false_q"].append(len(qrows - truth["broken_rows"]) / (truth["rows"] - truth["out_of_scope"]))
            with using(clean[0]):
                base = {(t, mm): team_view(ev, t, mm) for t in ("home", "away") for mm in range(20, 91, 10)}
                fit_c = {r["player"]: r["plan_fit"] for t in ("home", "away") for r in gp.assess(ev, t, 90, gp.default_plan(t))}
            with using(lu):
                noisy = {(t, mm): team_view(ne, t, mm) for t in ("home", "away") for mm in range(20, 91, 10)}
                fit_n = {r["player"]: r["plan_fit"] for t in ("home", "away") for r in gp.assess(ne, t, 90, gp.default_plan(t))}
            m["faultline"] += [base[k]["spof"]["name"] == noisy[k]["spof"]["name"] for k in base]
            for k in [(t, 60) for t in ("home", "away")]:
                fb = {p["name"]: p["fragility"] for p in base[k]["players"]}
                m["frag_err"] += [abs(p["fragility"] - fb.get(p["name"], 0)) for p in noisy[k]["players"]]
            common = [p for p in fit_c if p in fit_n]
            m["planfit_rho"].append(spearmanr([fit_c[p] for p in common], [fit_n[p] for p in common])[0])
        out[level] = {"matches": len(seeds), "gate_failed": int(sum(m["gate_failed"])),
                      "events_recovered": round(float(np.mean(m["events"])), 4), "score_same": round(float(np.mean(m["score_same"])), 3),
                      "faultline_agreement": round(float(np.mean(m["faultline"])), 3), "fragility_mae": round(float(np.mean(m["frag_err"])), 4),
                      "planfit_rho_median": round(float(np.median(m["planfit_rho"])), 3), "quarantine_recall": round(float(np.mean(m["recall"])), 3),
                      "false_quarantine": round(float(np.mean(m["false_q"])), 4), "quality_min": int(min(m["quality"])),
                      "quality_max": int(max(m["quality"])), "quality_mean": round(float(np.mean(m["quality"])), 1)}
    a, b = out[1.0], out[2.0]
    scope = ("Shows the pipeline undoes the kinds of mess real provider files have, and the analysis survives it. These results are "
             "optimistic: in this generator the receiver always makes the next touch, so receivers inferred from the next event are "
             "exact; in real matches deflections and 50-50s break that, so expect lower agreement. Real files can also be messy in ways "
             "this generator does not imitate (missing minutes, wrong player IDs): the quality report and its caveats are the guard there. "
             "Some broken rows (e.g. a missing location) are legitimately repaired from the previous pass rather than quarantined, "
             "which counts against the catch rate here.")
    heavy = f" At double noise: events {b['events_recovered']:.1%}, fault line {b['faultline_agreement']:.0%}, quality {b['quality_min']}-{b['quality_max']}."
    return [
        result("Data pipeline: real events recovered, same result", a["events_recovered"] >= BARS["pipe_events"] and a["score_same"] == 1 and not a["gate_failed"],
               f">= {BARS['pipe_events']:.0%} of events, identical final score, no gate failure",
               f"{a['events_recovered']:.2%} of events, score identical in {a['score_same']:.0%} of {a['matches']} matches" + heavy,
               "Noise level 1 on each of the matches; the clean match is the truth.", scope),
        result("Data pipeline: analysis survives the noise", a["faultline_agreement"] >= BARS["pipe_faultline"] and a["fragility_mae"] <= BARS["pipe_frag_mae"]
               and a["planfit_rho_median"] >= BARS["pipe_planfit_rho"],
               f"fault line agrees >= {BARS['pipe_faultline']:.0%}, fragility error <= {BARS['pipe_frag_mae']}, plan fit rho >= {BARS['pipe_planfit_rho']}",
               f"fault line {a['faultline_agreement']:.0%}, fragility error {a['fragility_mae']}, plan fit rho {a['planfit_rho_median']}",
               "Fault lines every 10 minutes for both teams; fragility of every player at 60'; plan-fit ranking at 90'.", scope),
        result("Data pipeline: quarantine is precise", a["quarantine_recall"] >= BARS["pipe_quarantine_recall"] and a["false_quarantine"] <= BARS["pipe_false_quarantine"],
               f"catches >= {BARS['pipe_quarantine_recall']:.0%} of broken rows, sets aside <= {BARS['pipe_false_quarantine']:.0%} of valid rows",
               f"caught {a['quarantine_recall']:.0%}, wrongly set aside {a['false_quarantine']:.2%}",
               "Broken rows: unreadable time, missing location, unknown team, coordinates far off the pitch.", scope),
        result("Data pipeline: the quality score sees the noise", a["quality_max"] < 100 and a["quality_min"] >= BARS["pipe_score_usable"],
               f"noisy files score below 100 (clean = 100) and >= {BARS['pipe_score_usable']} (usable)",
               f"{a['quality_min']}-{a['quality_max']} (mean {a['quality_mean']})", "The score is how far to trust the cleaned data.", scope),
    ], out


# ---------- plan reader ----------
READER_CASES = [  # (text, [(check, target, value)]); target = player, or "team" = anywhere in the plan
    ("Do not press high; sit deep and stay compact.", [("lacks", "team", "high_recoveries")]),
    ("No long balls, build up patiently through the middle.", [("lacks", "team", "long_passes"), ("has", "team", "completion")]),
    ("Never give the ball away cheaply.", [("has", "team", "completion")]),
    ("Don't panic under pressure, play out from the back.", [("has", "team", "press_resistance")]),
    ("Everything goes through Ferrand.", [("has", "Luca Ferrand", "network_share"), ("rel>=", "Luca Ferrand", 5)]),
    ("Player,Position,Instruction,Priority\nLuca Ferrand,CDM,Dictate tempo,High\nMilo Dancy,RW,Beat his man and cross,Low",
     [("rel>=", "Luca Ferrand", 5), ("rel<=", "Milo Dancy", 2), ("has", "Milo Dancy", "box_entries")]),
    ("Their striker Lindqvist is dangerous; our centre-backs must win every duel against him.",
     [("has", "Rui Calder", "recoveries"), ("has", "Jonah Pike", "recoveries")]),
    ("Ashgrove: lead the line and finish the chances. Top priority.", [("has", "Theo Ashgrove", "xg"), ("rel>=", "Theo Ashgrove", 5)]),
    ("Full-backs stay deep and safe, no overlapping.", [("lacks", "Eli Mensah", "box_entries"), ("has", "Eli Mensah", "completion")]),
    ("Wingers counter-press immediately when we lose the ball.", [("has", "Milo Dancy", "high_recoveries"), ("has", "Rafa Quinlan", "high_recoveries")]),
    ("Brightwell and Andersen break lines and arrive late in the box.", [("has", "Owen Brightwell", "progressive"), ("has", "Kai Andersen", "progressive")]),
    ("Okafor can support the attack occasionally but his main job is to defend.", [("has", "Sami Okafor", "recoveries")]),
    ("Avoid crossing; combine through the middle instead.", [("lacks", "team", "box_entries")]),
    ("Todo pasa por Ferrand: él marca el ritmo del partido.", [("has", "Luca Ferrand", "network_share")]),
    ("Press from the front: the striker leads the press.", [("has", "Theo Ashgrove", "high_recoveries")]),
    ("Mensah has licence to bomb forward and deliver crosses.", [("has", "Eli Mensah", "box_entries")]),
    ("We don't need to dominate possession; let them have the ball and hit them on the break.",
     [("lacks", "team", "completion"), ("has", "team", "progressive")]),
    ("Kai Andersen, LCM, arrive in the box late and shoot, Medium", [("has", "Kai Andersen", "xg"), ("rel>=", "Kai Andersen", 4)]),
]


def _effective(plan, target):
    if target == "team":
        return set().union(*(p["metrics"] for p in plan["principles"]), *(d["metrics"] for d in plan["duties"].values()))
    role = squad("home")[target]
    return set(plan["duties"].get(target, {}).get("metrics", {})).union(
        *(p["metrics"] for p in plan["principles"] if role in p["roles"]))


def score_reader(parse):
    passed, total, fails = 0, 0, []
    for text, checks in READER_CASES:
        plan = parse(text)
        for kind, target, val in checks:
            rel = plan["duties"].get(target, {}).get("relevance", 3) if target != "team" else None
            ok = {"has": lambda: val in _effective(plan, target), "lacks": lambda: val not in _effective(plan, target),
                  "rel>=": lambda: rel >= val, "rel<=": lambda: rel <= val}[kind]()
            passed, total = passed + ok, total + 1
            if not ok:
                fails.append(f"{text[:60]!r}: expected {kind} {val} for {target}")
    return passed / total, fails


# ---------- agents ----------
AGENT_CASES = [  # (question, any-of expected tools, must-mention regex or None)
    ("Who should I take off?", {"squad_assessment"}, None),
    ("Is our game plan working?", {"plan_execution"}, None),
    ("How is Owen Brightwell doing against the plan?", {"assess_player"}, None),
    ("Where should we attack?", {"zone_report"}, r"left|centre|center|right|middle"),
    ("Where are we losing the ball?", {"zone_report"}, r"third"),
    ("Who is carrying too much load?", {"squad_assessment", "assess_player"}, None),
    ("Is our press working?", {"plan_execution", "zone_report", "squad_assessment"}, None),
    ("What does the plan ask of Ferrand, and is he doing it?", {"assess_player"}, None),
    ("What happens if Ferrand is marked out of the game?", {"what_if_remove"}, None),
    ("Who is going to win from here?", {"win_probability"}, r"%"),
    ("How has Northbridge changed since half-time?", {"compare_windows"}, None),
    ("Which of our wingers is doing better?", {"assess_player", "squad_assessment"}, r"Dancy|Quinlan"),
    ("Should I take Ferrand off?", {"what_if_remove"}, None),
    ("Is Ashgrove getting enough service?", {"assess_player", "player_profile", "zone_report"}, None),
    ("What is Riverside's weak side?", {"zone_report"}, r"left|centre|center|right"),
    ("Are our full-backs doing their job?", {"assess_player", "squad_assessment"}, r"Mensah|Okafor"),
    ("Who has been our best player?", {"squad_assessment"}, None),
    ("Is Riverside's game plan working?", {"plan_execution"}, None),
    ("Their winger Reyes keeps getting forward; who needs to deal with it?", {"zone_report", "assess_player", "squad_assessment", "player_profile", "game_plan"}, r"Okafor"),
    ("Who is being bypassed in our build-up?", {"squad_assessment"}, None),
    ("Is Kai Andersen fading?", {"assess_player"}, None),
    ("Give me three points for the half-time talk.", {"plan_execution", "squad_assessment", "match_state"}, None),
    ("Should we go more direct?", {"plan_execution", "zone_report", "match_state", "compare_windows"}, None),
    ("Who should I bring on?", {"squad_assessment"}, r"bench"),
    ("Should I take off the goalkeeper?", {"squad_assessment", "assess_player"}, r"Varga|keeper"),
    ("Recommend a pizza place near the stadium", set(), None),  # must be gated: no tools
]
REPEAT = ["Who should I take off?", "Should I take Ferrand off?", "Where should we attack?", "Who has been our best player?",
          "Is our game plan working?", "Who is carrying too much load?"]
JUDGE = ("You grade an assistant coach's answer to a head coach during a live match. Score 1-5: 5 = directly answers "
         "the question, uses the data given, gives a sensible football recommendation when one is asked for; 1 = off "
         "topic or wrong. Return JSON {\"score\": n, \"reason\": \"one sentence\"}.")


def _entities(text):
    names = {n.split()[-1] for t in TEAMS for n in TEAMS[t]["players"] if re.search(rf"\b{n.split()[-1]}\b", text)}
    return names | set(re.findall(r"\b(left|right|centre|center)\b", text.lower()))


async def run_agents():
    import agents
    import ai
    ev = [e for e in simulate(23) if e["t"] < 70 * 60]
    rows, guard = [], 0
    for q, tools, must in AGENT_CASES:
        t0 = time.perf_counter()
        out = await agents.ask(q, ev, 70, 23, "English", (), None, "home")
        used = {s["tool"] for s in out["trace"] if s.get("tool")}
        # The guard judges model-written advice; a deterministic fallback is raw tool data, not advice
        violations = agents.advice_issues(out["answer"]) if used and out["source"].startswith("ai") else []
        guard += bool(violations)
        ok_tools = (not tools and out["source"] == "router (gated)") or bool(tools & used)
        ok_must = must is None or bool(re.search(must, out["answer"], re.I))
        r = ai.client().chat.completions.create(model=ai.deployment("chat"), temperature=0, response_format={"type": "json_object"},
                                                messages=[{"role": "system", "content": JUDGE}, {"role": "user", "content": json.dumps(
                                                    {"question": q, "answer": out["answer"], "data": [s for s in out["trace"]][:12]})[:6000]}])
        judge = json.loads(r.choices[0].message.content)
        rows.append({"question": q, "tools": sorted(used), "tools_ok": ok_tools, "mention_ok": ok_must, "source": out["source"],
                     "guard": violations, "judge": judge.get("score"), "judge_reason": judge.get("reason"),
                     "seconds": round(time.perf_counter() - t0, 1), "answer": out["answer"]})
        print(f"  {q[:50]:50} tools={'ok' if ok_tools else 'MISS'} judge={judge.get('score')} {rows[-1]['seconds']}s", flush=True)
    same, same_rec = [], []
    for q in REPEAT:
        a = [(await agents.ask(q, ev, 70, 23, "English", (), None, "home"))["answer"] for _ in range(2)]
        e1, e2 = _entities(a[0]), _entities(a[1])
        same.append(len(e1 & e2) / max(1, len(e1 | e2)) >= 0.6)
        same_rec.append(_recommendation(a[0]) == _recommendation(a[1]))
    return rows, guard, float(np.mean(same)), float(np.mean(same_rec))


def _recommendation(answer):
    """The first player or channel named in the recommending sentence (else the last sentence): the advice itself."""
    sentences = re.split(r"(?<=[.!?])\s+", answer.strip())
    s = next((x for x in sentences if re.search(r"recommend|suggest|should|focus", x, re.I)), sentences[-1])
    words = [p.split()[-1] for t in TEAMS for p in TEAMS[t]["players"]] + ["left", "right", "centre", "center"]
    cue = re.search(r"recommend|suggest|should|focus", s, re.I)
    after = s[cue.end():] if cue and cue.end() < len(s) else s  # the advice follows the verb ("..., I recommend substituting X")
    names = [(m.start(), m.group(0)) for n in words for m in re.finditer(rf"\b{n}\b", after, re.I)] or \
            [(m.start(), m.group(0)) for n in words for m in re.finditer(rf"\b{n}\b", s, re.I)]
    if not names:  # the recommending sentence names nobody ("he"): use the first player/channel in the answer
        names = [(m.start(), m.group(0)) for n in words for m in re.finditer(rf"\b{n}\b", answer, re.I)]
    return min(names)[1].lower() if names else ""


def test_agents():
    rows, guard, consistency, consistency_rec = asyncio.run(run_agents())
    n = len(rows)
    tool_rate = np.mean([r["tools_ok"] and r["mention_ok"] for r in rows])
    fallback = np.mean([r["source"].startswith("deterministic") for r in rows])
    judge = np.mean([r["judge"] or 0 for r in rows])
    scope = "Tests the agent layer as it would run on real data: the tools change, the orchestration doesn't."
    return [
        result("Agents: right tools and content", tool_rate >= BARS["agent_tools"], f">= {BARS['agent_tools']:.0%} of {n} coach questions",
               f"{tool_rate:.0%}", "Each question lists the tools a good answer needs and, where relevant, what it must mention.", scope),
        result("Agents: coaching guard holds", guard == 0, "0 final answers recommending a key player off unchecked, or the keeper off",
               f"{guard} violations", "Deterministic check on the final answer (after the self-correcting rewrite).", scope),
        result("Agents: verified answers, rarely the fallback", fallback <= BARS["agent_fallback_max"], f"<= {BARS['agent_fallback_max']:.0%} deterministic fallbacks",
               f"{fallback:.0%}", "Fallback = the analyst failed the fact-check twice.", scope),
        result("Agents: same question, same advice", consistency >= BARS["agent_consistency"], f">= {BARS['agent_consistency']:.0%} of repeated questions name the same players/channels",
               f"{consistency:.0%} of {len(REPEAT)}", "Each question asked twice; players and channels named must overlap >= 60%.", scope),
        result("Agents: same recommendation (added after run 4)", consistency_rec >= BARS["agent_consistency"],
               f">= {BARS['agent_consistency']:.0%} of repeated questions lead with the same player/channel",
               f"{consistency_rec:.0%} of {len(REPEAT)}", "The test above also counts supporting names; this compares only the advice itself "
               "(first player or channel named in the recommending sentence). Reported alongside, not instead.", scope),
        result("Agents: answer quality (model judge)", judge >= BARS["agent_judge"], f"mean >= {BARS['agent_judge']} / 5",
               f"{judge:.1f} / 5", "gpt-4.1-mini grades each answer against its own trace. A model judging a model: a screen, not proof; spot-check by hand.",
               "Real coaches should grade a sample before trusting this."),
    ], rows


# ---------- report ----------
HISTORY = """## What changed between runs (no pass bar was changed)

* **Run 1** ([evaluation-run1.md](evaluation-run1.md), 12-match smoke run): 3 of 11 passed.
  * Flags fired 6.8 per team per check: 'bypassed' hit centre-backs for touching the ball less than midfielders.
    Fix: load is compared with each position's normal share of the ball (`gameplan.role_share`).
  * Cues fired 22 per match: a 20% fault-line share is normal in ordinary play.
    Fix: thresholds calibrated on ordinary matches from separate seeds (`evaluate.py --calibrate`, seeds 7000+): alert at 40%, narrowing at -18%, overload 1.41x, bypass 0.56x, fading -22 points. A new fault line must also hold its lead for 2 minutes.
  * Expected points added did not track results: it is leverage-weighted by design. Kept as FAIL; a head-to-head against the old clutch points was added.
  * Fault-line test was underpowered. Fix: common random numbers and 4 replays per pair.
* **Run 2** ([evaluation-run2.md](evaluation-run2.md), full offline run on held-out seeds): 6 of 12 passed.
  * Losing a player costs nothing in the generator (mean progression lost -0.2): the outcome cannot test fault lines.
    Added a connectivity test of fragility's literal claim; the original test is kept and reported as FAIL.
* **Run 3** ([evaluation-run3.md](evaluation-run3.md), first run with the AI plan reader and agents): 9 of 19 passed.
  The agents' coaching guard was added before any agent run, after a manual test recommended taking off the hub unchecked.
  * The connectivity test compared unlike quantities (the intact network with the removed player's passes deleted),
    an error in the test itself. Fix: compare the teammates' connectivity with paths through anyone allowed, as fragility defines it.
  * The analyst did not know the line-ups ("who are the full-backs?"). Fix: line-ups in every request.
  * Riverside's demo plan has only position duties, so plan execution scored nothing. Fix: duties are scored like principles.
  * Consistency 33%: the analyst ran at default temperature. Fix: temperature 0.
  * The AI plan reader missed negations the rules handle. Fix: prompt rule + the rules' negation detector as a
    deterministic guard on the model's reading (`gameplan.drop_negated`). The reader cases were not changed.
* **Run 4** ([evaluation-run4.md](evaluation-run4.md)): after those fixes, 10 of 19 passed.
  * Agents put our right-back on Riverside's right winger (it is our left-back). Fix: direct opponents given as facts
    in every request (`agents.matchups`), and zone reports state their frame.
  * Repeated questions: the core advice was already stable at temperature 0; differences came from the router race
    (Phi-4-mini and the gpt-4.1-mini hedge can label a question differently, and which wins depends on latency) and
    from supporting names. Fix: same question, same route (`agents._routes`), and a fixed seed for the analyst.
    A second consistency measure (the recommendation itself) was added and is reported alongside the original.
* **Run 5** ([evaluation-run5.md](evaluation-run5.md), `--only-ai`): tools 92% (pass), quality 4.8/5; guard 1, consistency 67% / 33%.
  * The one guard 'violation' was a deterministic fallback (raw tool data containing the field name
    `substitution_candidates`), not advice: an error in the test. Fix: the guard judges model-written answers only.
  * The recommendation extractor returned sentence text when the recommending sentence named nobody ("he remains
    the most effective"): an error in the new metric. Fix: fall back to the first player/channel in the answer.
  * Real cause of unstable answers: the guard itself. The analyst recommended taking off Ferrand without the what-if,
    the rewrite sometimes failed, and the answer fell back to raw data. Fix: for substitution questions the
    orchestrator gathers the squad assessment and the what-ifs before the analyst writes; if the guard still catches
    a skipped what-if, the verifier runs it and passes the result into the rewrite.
* **Run 6** ([evaluation-run6.md](evaluation-run6.md), `--only-ai`): guard 0, fallbacks 0%, tools 92%; consistency 67% / 67%.
  * "Who should I take off?" asked three times named three different players from identical data: the data listed
    candidates without choosing, and the model broke the tie differently each time. Fix: a rule the coach can audit
    makes the choice (`agents.SUB_MAX_COST`: lowest recent plan fit, fading counts against, among candidates whose
    removal costs under 10% of teammates' connectivity); the analyst explains it.
  * The recommendation extractor took the first name in the sentence ("Given Pike's importance..., I recommend
    substituting Luca Ferrand" -> Pike): an error in the metric. Fix: names after the recommending verb first.
* **Run 7** ([evaluation-run7.md](evaluation-run7.md), `--only-ai`): 12 of 20; same recommendation 6 of 6, tools 96%.
  * The flank question passed here, but asked again on the live server it named our left winger instead of the
    left-back facing Reyes: the matchups were in the prompt, yet the model still chose. Fix (same pattern as
    substitutions): when a question names an opponent, the orchestrator looks up who faces them and assesses that
    player before the analyst writes (`agents._gather_matchups`).
* **Run 8** ([evaluation-run8.md](evaluation-run8.md), `--only-ai`): 12 of 20; the flank question names the left-back 3 of 3.
* **Run 9** ([evaluation-run9.md](evaluation-run9.md)): adds the data-pipeline robustness tests (`--only-pipeline`): 20 matches turned into messy
  provider files by `noise.py` and cleaned by `pipeline.py`. Bars fixed after a one-match smoke test, before this run.
"""

REAL_DATA = """## Still needs real data (protocols, fixed in advance)

| Claim | Study | Data | Pass bar |
|---|---|---|---|
| High-fragility players hurt most when lost | Injuries and red cards as natural experiments: change in progression and xG 15' after vs before, against the player's fragility, controlling for score, minute, men on the pitch, opponent strength; repeat with pass count and betweenness | 3+ seasons of event data with substitutions and cards | Fragility effect CI below 0 and stronger than pass count |
| Plan fit = doing the job | 3+ coaches grade "did he do his job" per player for 20+ matches; first measure coach-coach agreement (ICC), then plan fit vs consensus; learn weights from the grades and test on held-out matches | Coach grades + event data | Plan fit agrees with coaches about as well as coaches agree with each other |
| Real position norms | Per-90 percentiles per position from a full league season, minimum minutes; neutral-plan fit centred on 50 per position | One season of league event data | Mean 50 +-3 and similar spread in every position |
| 'Fading' is real decline | Flag vs the next 15 minutes (beyond regression to the mean); vs high-speed running drop; vs the real coach's substitutions | Event + tracking data | Flag effect CI below 0; agrees with physical decline |
| Substitution candidates | Overlap with the real coach's next substitution; outcomes after matching on score and minute | Event data with substitutions | Sanity only: candidates overlap real subs more than chance |
| Cue precision | Analysts label a sample of cues meaningful or noise; A/B viewer test | Real matches + analysts | >= 70% labelled meaningful |
| Plan reader | 30-50 real game plans labelled by two coaches (metrics, players, importance); measure their agreement first | Real coaching plans | Reader within coach-coach agreement |

Real event data for these studies: StatsBomb open data allows non-commercial research (keep it out of the shipped app;
check its terms against the hackathon's). Coach grades, bench and tracking data need a club or provider partner.
"""


def write(tests, raw, ai_run):
    md = ["# Evaluation of the judgement-based parts", "",
          f"Generated by `python evaluate.py{' --ai' if ai_run else ''}`. Pass bars are fixed in `BARS` in `evaluate.py` "
          "and were set before the first run. Synthetic results show the methods behave sensibly in a world we control; "
          "the last section lists what only real data can show.", "",
          f"**{sum(t['passed'] for t in tests)} of {len(tests)} passed.**", "",
          "| Test | Pass bar | Result | Verdict |", "|---|---|---|---|"]
    md += [f"| {t['test']} | {t['bar']} | {t['result']} | {'PASS' if t['passed'] else '**FAIL**'} |" for t in tests]
    md += ["", "## Notes per test", ""]
    md += [f"* **{t['test']}** — {t['note']} *Scope:* {t['what_it_shows']}" for t in tests]
    if "reader_fails" in raw:
        md += ["", "## Plan-reader misses", ""] + [f"* {k}: {f}" for k, fs in raw["reader_fails"].items() for f in fs]
    if "agents" in raw:
        md += ["", "## Agent answers", "", "| Question | Tools | Judge | Source |", "|---|---|---|---|"]
        md += [f"| {r['question']} | {', '.join(r['tools']) or '(gated)'} | {r['judge']} | {r['source']} |" for r in raw["agents"]]
    md += ["", HISTORY, "", REAL_DATA]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/evaluation.md").write_text("\n".join(md), encoding="utf-8")
    (ROOT / "reports/evaluation.json").write_text(json.dumps({"tests": tests, "raw": raw}, indent=1, default=str), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--quick", action="store_true", help="fewer matches (smoke test)")
    ap.add_argument("--calibrate", action="store_true", help="print alert thresholds from seeds 7000+ and stop")
    ap.add_argument("--only-ai", action="store_true", help="rerun the AI tests; keep the offline results of the last run")
    ap.add_argument("--only-pipeline", action="store_true", help="rerun the data-pipeline tests; keep everything else")
    args = ap.parse_args()
    if args.calibrate:
        calibrate(range(7000, 7040))
        return
    n = 12 if args.quick else 60
    t0 = time.time()
    stage = lambda name: print(f"[{time.time() - t0:5.0f}s] {name}", flush=True)
    if args.only_pipeline:
        prev = json.loads((ROOT / "reports/evaluation.json").read_text(encoding="utf-8"))
        tests = [t for t in prev["tests"] if not t["test"].startswith("Data pipeline")]
        stage("data pipeline")
        t, prev["raw"]["pipeline"] = test_pipeline(range(8000, 8000 + (6 if args.quick else 20)))
        write(tests + t, prev["raw"], True)
        stage(f"done: {sum(x['passed'] for x in tests + t)}/{len(tests + t)} passed -> reports/evaluation.md")
        return
    if args.only_ai:
        prev = json.loads((ROOT / "reports/evaluation.json").read_text(encoding="utf-8"))
        tests = [t for t in prev["tests"] if not t["test"].startswith(("Agents", "Plan reader (gpt"))]
        raw = {k: v for k, v in prev["raw"].items() if k != "agents"}
        raw["reader_fails"] = {"rules": raw.get("reader_fails", {}).get("rules", [])}
        args.ai = True
    else:
        tests, raw = offline(n, stage)
    if args.ai:
        rate, fails = score_reader(lambda text: gp.parse(text, "home")[0])
        raw["reader_fails"]["ai"] = fails
        tests.append(result("Plan reader (gpt-4.1-mini)", rate >= BARS["reader_ai"], f">= {BARS['reader_ai']:.0%} of checks",
                            f"{rate:.0%} of {sum(len(c) for _, c in READER_CASES)} checks", "Same cases as the rules.",
                            "Language understanding: independent of the match data. Real coaches' plans are the real test."))
        stage("agents")
        t, raw["agents"] = test_agents()
        tests += t
    write(tests, raw, args.ai)
    stage(f"done: {sum(t['passed'] for t in tests)}/{len(tests)} passed -> reports/evaluation.md")


def offline(n, stage):
    tests, raw = [], {}
    stage("fault lines")
    t, raw["fragility"] = test_fragility(range(2000, 2000 + n))
    tests += t
    stage("plan fit")
    neutral = _assessments(range(3000, 3000 + n), storyline=False)
    t, raw["plan_fit"] = test_plan_fit(neutral)
    tests += t
    stage("flags")
    t, raw["flags"] = test_flags(neutral, _assessments(range(4000, 4000 + n), storyline=True))
    tests += t
    stage("cues")
    t, raw["cues"] = test_cues(range(5000, 5000 + max(6, n // 3)))
    tests += t
    stage("expected points added")
    t, raw["xpa"] = test_xpa(range(6000, 6000 + 4 * n))
    tests += t
    stage("data pipeline")
    t, raw["pipeline"] = test_pipeline(range(8000, 8000 + max(6, n // 3)))
    tests += t
    stage("plan reader")
    rate, fails = score_reader(lambda text: gp.normalise(gp.parse_rules(text, "home"), "home"))
    raw["reader_fails"] = {"rules": fails}
    tests.append(result("Plan reader (keyword rules)", rate >= BARS["reader_rules"], f">= {BARS['reader_rules']:.0%} of checks",
                        f"{rate:.0%} of {sum(len(c) for _, c in READER_CASES)} checks", "Negations, opponent instructions, sheets, other languages, emphasis.",
                        "Language understanding: independent of the match data."))
    return tests, raw


if __name__ == "__main__":
    main()
