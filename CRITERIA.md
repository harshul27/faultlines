# Hackathon criteria → evidence

Every requirement from the challenge brief and the official rules, where it is met, and the test that proves it.
`python check.py` (requirements) and `python validate.py` (realism vs held-out real data) must both pass.

## Challenge brief: one pipeline, five stages

| Requirement | How Fault Lines meets it | Proof |
|---|---|---|
| Synthetic, football-realistic data; no real PL match data | Seeded generator (`match.py`), fictional clubs/players; calibrated on CC0 results + published league averages, then validated on held-out seasons | `check.py`: no real club names; `validate.py`: [reports/validation.md](reports/validation.md) |
| (a) Ingest events as they happen | `stream.py` `LiveEngine.ingest()` processes one event at a time; `replay()` delivers events in wall-clock time | `check.py`: p95 ingest latency < 250 ms (measured ~16 ms; real events arrive every ~5 s) |
| (b) Interpret into stats, patterns, context | Rolling pass network, fragility per player, connectivity index, progression and completion per window, betweenness, clutch score (`network.py`) | `check.py`: hub detected in ≥ 80% of 20 matches; marking it cuts progression vs paired control matches |
| (c) Explain why a moment matters | Fault-line explanation: what the team loses without a player; Foundry model constrained to computed facts (`explain.py`) | `check.py`: fact-check rejects invented numbers; templates are grounded |
| (d) Render on screen, machine-readable and timed | Pitch overlay + timed overlay cues with match clock, priority, display time, anchor (`stream.py` `CUE_SCHEMA`) | `check.py`: every cue validates against the JSON Schema; cues ordered by clock; 5–40 cues/match |
| (e) Personalize: analyst vs casual, favourite club/player, language | Sidebar: follow either club, favourite player lens (passes, network share, clutch), Casual/Analyst, 7 languages | `check.py`: every mode × favourite × language renders; analyst ≠ casual |
| Player identification / focus mode | Favourite-player highlight and lens | manual demo |
| Narrative generation, explainability ("control vs chaos", pressure changes) | Network fracture cues, fault-line shifts, connectivity timeline | `check.py` cue tests |
| Data innovation: create/extend synthetic datasets | Calibrated generator with team-strength prior, team shape, zone risk, pressure | `validate.py` |
| Multi-language storytelling | Language selector → Foundry model | needs Foundry deployment (pending) |

## Judging rubric (20% each)

| Criterion | Evidence | Gap / next step |
|---|---|---|
| Technological implementation | 81/19 train/test split by time (4,940 / 1,190 real matches), regularisation tuned inside training years, 17/17 held-out checks, requirement self-checks, documented modules | Add CI (GitHub Actions running both checks) |
| Agentic design & innovation | Grounded explainer with numeric fact-check | Foundry agent(s) not yet deployed: multi-agent booth is a stretch goal |
| Real-world impact & applicability | Broadcast-ready cue schema; latency budget; fragility is a scouting/tactics metric clubs could use on real data | See roadmap in README |
| User experience & presentation | Streamlit app with live playback, cue feed, pitch overlay | Record the < 2 min demo |
| Adherence to category | All five pipeline stages covered (table above) | — |

## Official rules (submission)

| Rule | Status |
|---|---|
| Public GitHub repo | ⏳ to create |
| Demo video < 2 min on YouTube/Vimeo, no third-party trademarks or copyrighted music | ⏳ fictional clubs used; no PL marks in the app |
| Pitch naming the Microsoft/Azure technologies used | ⏳ after Foundry + Azure deployment |
| Third-party data used within its license (§4.3) | ✅ openfootball is CC0; published figures are facts with sources in `targets.json` |
| Working demo available to judges through the judging period (§4.8) | ⏳ deploy to Azure Container Apps |
| Deadline: 27 Oct 2026, 11:59 pm PT | ⏳ |
