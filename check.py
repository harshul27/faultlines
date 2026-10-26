"""Self-check: one assertion per hackathon requirement it covers (see CRITERIA.md). Run: python check.py
Data realism vs held-out real data lives in validate.py.
"""
import json
import jsonschema
import numpy as np

from calibrate import SEASONS, load
from explain import explain, facts, grounded, template
from match import HUB, TEAMS, simulate
from network import score_at, team_view
from stream import CUE_SCHEMA, LiveEngine

ev = list(simulate(23))  # demo match

# Synthetic, football-realistic data
assert ev == list(simulate(23)), "generator must be deterministic per seed"
real_clubs = {c.lower() for s in SEASONS for h, a, *_ in load(s) for c in (h, a)}
names = {TEAMS[t]["name"].lower() for t in TEAMS} | {p.lower() for t in TEAMS for p in TEAMS[t]["players"]}
assert not names & real_clubs, "synthetic clubs/players must not reuse real Premier League club names"
assert {"pass", "shot"} <= {e["type"] for e in ev} and any(e["pressure"] for e in ev), "events cover passes, shots, pressure"

# (a) Ingest as they happen + live responsiveness
eng, lat = LiveEngine(), []
for e in ev:
    cues, ms = eng.ingest(e)
    lat.append(ms)
    for c in cues:
        jsonschema.validate(c, CUE_SCHEMA)  # (d) render: machine-readable, timed overlay cues
assert np.percentile(lat, 95) < 250, f"live ingest p95 {np.percentile(lat, 95):.0f} ms is too slow for live play"
assert 5 <= len(eng.cues) <= 40, f"{len(eng.cues)} cues: overlays must be informative but not spammy"
assert all(a["t"] <= b["t"] for a, b in zip(eng.cues, eng.cues[1:])), "cues must be emitted in match-clock order"
assert sum(c["cue"] == "goal" for c in eng.cues) == sum(e["goal"] for e in ev), "every goal produces exactly one cue"

# (b) Interpret: the storyline hub is detected, and marking it costs progression (paired vs the same match unmarked)
home, away = team_view(ev, "home", 60), team_view(ev, "away", 60)
assert home["spof"]["name"] == HUB[1], "storyline hub should be the fault line before marking (demo seed)"
seeds = range(7, 27)
assert sum(team_view(list(simulate(s)), "home", 60)["spof"]["name"] == HUB[1] for s in seeds) >= 16, \
    "hub must be detected as the fault line in >= 80% of matches"


def prog_rate_change(events):
    rate = lambda lo, hi: sum(e["team"] == "home" and e["type"] == "pass" and e["success"] and e["end_x"] - e["x"] > 10
                              and lo * 60 <= e["t"] < hi * 60 for e in events) / (hi - lo)
    return rate(60, 95) - rate(20, 60)


net = [prog_rate_change(list(simulate(s))) - prog_rate_change(list(simulate(s, storyline=False))) for s in seeds]
assert np.mean(net) < 0 and sum(n < 0 for n in net) >= 14, "marking the hub should cut forward progression vs control"

# (c) Explain: grounded, and every number traceable to computed facts
f = facts(home, away, score_at(ev, 3600), favourite="Owen Brightwell")
assert grounded(template(f, "Analyst"), f) and grounded(template(f, "Casual"), f), "templates must be grounded"
assert not grounded("Ferrand holds 87% of the network.", f), "fact-check must reject invented numbers"

# (e) Personalize: every mode/language/favourite combination yields an explanation
for mode in ("Casual", "Analyst"):
    for fav in (None, "Owen Brightwell"):
        text, src = explain(facts(home, away, score_at(ev, 3600), fav), mode, "Spanish")
        assert text and src.startswith(("ai", "template")), "explanation must always render"
assert template(f, "Analyst") != template(f, "Casual"), "analyst and casual views must differ"
print("ok")

# Agentic layer, offline: tools are deterministic, grounded, and the fact-check catches flipped trends
import agents
from grounding import check as fact_check

agents.CTX.set({"events": [e for e in ev if e["t"] < 70 * 60], "minute": 70, "seed": 23, "trace": [], "outputs": []})
state, prof = agents.match_state(), agents.player_profile("Ferrand")
wi, wp, cw = agents.what_if_remove("Luca Ferrand"), agents.win_probability(), agents.compare_windows("home", 45, 70)
assert prof["player"] == "Luca Ferrand" and "error" in agents.player_profile("Nobody Real"), "player lookup must be strict"
assert wi["new_fault_line_without_them"].split(" would")[0] != "Luca Ferrand", "what-if must name a different fault line"
probs = [int(wp[k].rstrip("%")) for k in wp if k.endswith("_win") or k == "draw"]
assert 98 <= sum(probs) <= 102 and wp["simulations"] == 10_000, "Monte Carlo outcomes must sum to ~100%"
assert agents.win_probability() == wp, "same minute, same forecast (seeded)"
assert len(agents.CTX.get()["trace"]) == 7, "every tool call is traced"
spread = cw["passing_spread"]  # e.g. 'up 14% (25.1 -> 28.6)'
word, n = spread.split()[0], spread.split()[1]
opposite = "fell" if word == "up" else "rose"
assert fact_check(f"Passing spread {opposite} {n}.", cw), "fact-check must catch a flipped trend"
assert not fact_check(f"Passing spread is {word} {n}.", cw), "a correct trend must pass"
assert agents._rules("What if Ferrand is subbed off?")["intent"] == "what_if"
assert agents._rules("Who will win this?")["intent"] == "prediction"
print("agents ok")

# Game plan -> weighted player assessment
import gameplan as gp

plan = gp.default_plan("home")
assert plan["duties"]["Luca Ferrand"]["relevance"] == 5 and "network_share" in plan["duties"]["Luca Ferrand"]["metrics"], "plan hub parsed"
assert "high_recoveries" not in plan["duties"]["Rui Calder"]["metrics"], "'under pressure' is not a pressing instruction"
sheet = gp.normalise(gp.parse_rules("Player,Position,Instruction,Priority\nLuca Ferrand,CDM,Dictate tempo,High\n"
                                    "Owen Brightwell,RCM,Break lines,Medium\nMilo Dancy,RW,Stay wide and cross,Low", "home"), "home")
assert [sheet["duties"][p]["relevance"] for p in ("Luca Ferrand", "Owen Brightwell", "Milo Dancy")] == [5, 4, 2], "sheet priorities"
bad = gp.normalise({"principles": [{"text": "x", "metrics": {"bogus": 3, "xg": 9}}], "duties": [
    {"target": "Nobody", "metrics": {"xg": 1}}, {"target": "CB", "metrics": {"recoveries": 2}, "relevance": 99}]}, "home")
assert bad["principles"][0]["metrics"] == {"xg": 3.0} and set(bad["duties"]) == {"Rui Calder", "Jonah Pike"}, "untrusted plans are cleaned"
assert all(d["relevance"] == 5 for d in bad["duties"].values()) and gp.normalise(plan, "home") == plan, "clamped; idempotent"
fits = [r["plan_fit"] for s in range(500, 510) for t in TEAMS for r in gp.assess(list(simulate(s, storyline=False)), t, 90, gp.default_plan(t))]
assert 44 <= np.mean(fits) <= 56, f"league-norm players should average ~50 plan fit, got {np.mean(fits):.1f}"
rows = {r["player"]: r for r in gp.assess([e for e in ev if e["t"] < 4200], "home", 70, plan)}
assert len(rows) == 11 and all(1 <= r["plan_fit"] <= 99 for r in rows.values())
assert any(f.startswith("overloaded") for f in rows[HUB[1]]["flags"]) and rows[HUB[1]]["form"] == "fading", \
    "the hub carries more load than the plan intends, and fades once man-marked (60')"
ex = gp.execution(ev, "home", 70, plan)
assert len(ex["principles"]) == len(plan["principles"]) and 1 <= ex["overall_plan_execution"] <= 99
z = gp.zones(ev, "home", 70)
raw = gp.raw_metrics(ev, "home", 0, 4200)
assert sum(z["final_third_entries_by_channel"].values()) == sum(r["final_third"] for r in raw.values()), "zones agree with metrics"

agents.CTX.set({"events": [e for e in ev if e["t"] < 70 * 60], "minute": 70, "seed": 23, "trace": [], "outputs": [],
                "plans": {t: gp.default_plan(t) for t in TEAMS}, "plan_team": "home", "custom_plan": False})
sq, ap = agents.squad_assessment("our team"), agents.assess_player("Ferrand")
assert sq["team"] == TEAMS["home"]["name"] and len(sq["players"]) == 11 and ap["player"] == HUB[1]
assert all(c["player"] != "Tomas Varga" for c in sq["substitution_candidates"]), "never suggest taking off the keeper"
assert agents.plan_execution("Riverside")["team"] == TEAMS["away"]["name"] and agents.zone_report("home", 15)["window"] == "last 15 min"
assert agents.game_plan("home")["duties"][HUB[1]]["relevance"] == "5/5"
for q, want in [("Who should I take off?", "substitution"), ("Is our press working?", "tactics"),
                ("How is Brightwell performing in his role?", "assessment"), ("What if Ferrand is subbed off?", "what_if"),
                ("Which flank should we attack?", "tactics")]:
    assert agents._rules(q)["intent"] == want, (q, agents._rules(q))
print("game plan ok")
