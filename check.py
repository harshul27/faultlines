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
n_duties = len({d["duty"] for d in plan["duties"].values() if d["metrics"]})
assert len(ex["principles"]) == len(plan["principles"]) + n_duties and 1 <= ex["overall_plan_execution"] <= 99, "principles and duties scored"
assert gp.execution(ev, "away", 70, gp.default_plan("away"))["principles"], "a plan of position duties is scored too"
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

# Validation fixes (see evaluate.py / reports/evaluation.md)
from network import expected_points

never = gp.normalise(gp.parse_rules("Do not press high. No long balls, build up through the middle. Never give it away cheaply.", "home"), "home")
found = set().union(*(p["metrics"] for p in never["principles"]))
assert "high_recoveries" not in found and "long_passes" not in found and {"completion"} <= found, "negations are respected"
assert 1.5 < expected_points(0, 95, "home") < 1.7, "home side at kickoff ~1.6 expected points (Premier League ~1.6)"
assert expected_points(4, 5, "home") - expected_points(3, 5, "home") < 0.01 < expected_points(0, 5, "home") - expected_points(-1, 5, "home"), \
    "a late equaliser is worth far more than a fifth goal"
paired = list(simulate(23, absent=("home", HUB[1], 3600), fork=(3600, 1)))
assert [e for e in paired if e["t"] < 3600] == [e for e in ev if e["t"] < 3600], "paired experiments share the first hour"
assert sum(e["t"] > 3610 and HUB[1] in (e["player"], e["recipient"]) for e in paired) == 0, "an absent player takes no part"
agents.CTX.set({"events": [e for e in ev if e["t"] < 4200], "minute": 70, "seed": 23, "trace": [], "outputs": [],
                "plans": {t: gp.default_plan(t) for t in TEAMS}, "plan_team": "home", "custom_plan": False})
assert agents.advice_issues("I recommend taking off Luca Ferrand.") and agents.advice_issues("Substitute Tomas Varga."), "coaching guard"
agents.what_if_remove("Ferrand")
assert not agents.advice_issues("I recommend taking off Luca Ferrand."), "guard clears once the counterfactual was checked"
assert "their Callum Reyes (RW) vs our Sami Okafor (LB)" in agents.matchups("home"), "flanks mirror: their right meets our left"
agents.CTX.get()["outputs"].clear()
pick = agents._gather_substitution("home", [])[0]
assert pick["recommended_substitution"] == agents._gather_substitution("home", [])[0]["recommended_substitution"] == HUB[1], \
    "substitution advice is a deterministic rule (demo: the man-marked, fading hub, whose removal costs under 10%)"
agents.CTX.get()["outputs"].clear()
assert agents._gather_matchups("home", ["Callum Reyes"])[0]["faced_by"] == ["Sami Okafor (LB)"], "who faces an opponent is a fact"
print("validation fixes ok")

# Bring your own match: the cleaning pipeline, adapters, isolation from the demo, robustness to realistic noise
import noise
import pipeline
import sources
from match import DEMO_TEAMS, using

demo_teams = {s: {"name": DEMO_TEAMS[s]["name"], "roles": gp.squad(s)} for s in ("home", "away")}
lu, evs, quar, rep = sources.parse(sources.to_csv(ev, demo_teams), "csv", "Test United", "Sample City")
assert rep["score"] == 100 and not quar and not rep["fixes"], "clean data passes untouched"
with using(lu):
    assert TEAMS["home"]["name"] == "Test United" and team_view(evs, "home", 60)["spof"]["name"] == HUB[1], "CSV round trip keeps the analysis"
assert TEAMS["home"]["name"] == DEMO_TEAMS["home"]["name"], "a user's match never leaks into the demo"
assert len(sources.parse(json.dumps(sources.to_native(evs, lu)))[1]) == len(evs), "native JSON round trip"

text, truth = noise.noisy_csv(ev, demo_teams, seed=3)
nl, ne, nq, nr = sources.parse(text)
assert len(ne) == len(ev) and score_at(ne, 1e9) == score_at(ev, 1e9), "noisy file: every real event recovered, same score"
assert truth["broken_rows"] <= {q["source_row"] for q in nq} and len(nq) == len(truth["broken_rows"]), "broken rows quarantined, nothing else"
assert nr["rows"]["out_of_scope"] == truth["out_of_scope"] and 75 <= nr["score"] < 100, "noise is detected and reported"
assert nl["home"]["name"] == DEMO_TEAMS["home"]["name"] and all("<" not in n for s in ("home", "away") for n in nl[s]["players"]), "names cleaned"
with using(nl):
    assert all(team_view(ne, s, m)["spof"]["name"] == team_view(ev, s, m)["spof"]["name"] for s in ("home", "away") for m in (30, 60, 75)), \
        "fault lines survive the noise"
assert all(e["imputed"] == [] or set(e["imputed"]) <= {"success", "recipient", "end_location", "xg", "goal", "location"} for e in ne), "provenance flags"

sb = [{"type": {"name": "Starting XI"}, "team": {"name": "Alpha"}, "tactics": {"lineup": [
          {"player": {"name": "A One"}, "position": {"name": "Goalkeeper"}}, {"player": {"name": "A Two"}, "position": {"name": "Center Forward"}}]}},
      {"type": {"name": "Starting XI"}, "team": {"name": "Beta"}, "tactics": {"lineup": [
          {"player": {"name": "B One"}, "position": {"name": "Goalkeeper"}}]}},
      {"type": {"name": "Pass"}, "team": {"name": "Alpha"}, "player": {"name": "A One"}, "minute": 0, "second": 5, "period": 1,
       "location": [12, 40], "pass": {"end_location": [60, 20], "recipient": {"name": "A Two"}}},
      {"type": {"name": "Shot"}, "team": {"name": "Alpha"}, "player": {"name": "A Two"}, "minute": 0, "second": 9, "period": 1,
       "location": [108, 40], "shot": {"statsbomb_xg": 0.31, "outcome": {"name": "Goal"}}}]
rows, teams = sources.from_statsbomb(sb)
assert teams["home"]["players"][1] == {"name": "A Two", "role": "ST"} and rows[0]["x"] == 10.0 and rows[0]["end_y"] == 25.0, "StatsBomb mapping"
assert rows[1]["xg"] == 0.31 and rows[1]["goal"] is True, "provider xG and goals kept"
try:
    sources.parse(json.dumps(sb))
    raise AssertionError("a 2-event file must fail the quality gate")
except sources.DataError as e:
    assert e.report and not e.report["gate"]["passed"], "a failed gate still returns the report"
for bad_input in ('{"teams": {}, "events": []}', "t,team\n1,home", "[1, 2]"):
    try:
        sources.parse(bad_input)
        raise AssertionError(f"accepted bad input {bad_input[:30]}")
    except sources.DataError:
        pass

live = pipeline.LiveCleaner(lu)
out = live.push([{**e, "recipient": None} if i == 3 else e for i, e in enumerate(sources.to_native(evs, lu)["events"][:40])])
assert len(out) == 40 and live.report()["held_for_next_event"] == 0, "live: a pass missing its receiver waits only for the next event"
nat = sources.to_native(evs, lu)["events"]
assert live.push([{**nat[40], "recipient": None}]) == [] and live.report()["held_for_next_event"] == 1, "live: held until the next event"
assert len(live.push([nat[41]])) == 2, "live: released when it arrives"
assert out[3]["recipient"] == evs[3]["recipient"] and out[3]["imputed"] == ["recipient"], "live: receiver inferred from the next event"
assert live.push([nat[0]]) == [] and live.report()["quarantined"] == 1, "live: late events quarantined"
pkg = pipeline.package(nl, ne, nq, nr, pipeline.fingerprint(text))
import zipfile, io
names = set(zipfile.ZipFile(io.BytesIO(pkg)).namelist())
assert {"clean_events.csv", "clean_events.json", "quarantine.csv", "quality_report.json", "quality_report.md", "manifest.json",
        "DATA_DICTIONARY.md"} <= names, "the package gives the user everything back"
print("bring your own data ok")
