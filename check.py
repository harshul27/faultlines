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
