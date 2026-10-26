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
| Real-time delivery to viewers | `server.py`: shared match clock pushed per event over SSE; state of play for late joiners; `Last-Event-ID` resume; back-pressure. Web page (GitHub Pages) with Live and Deep-analysis tabs | `check_web.py`: hello → ordered event ids → snapshots; cues valid; reconnect resumes with no gap |
| (e) Personalize: analyst vs casual, favourite club/player, language | Sidebar: follow either club, favourite player lens (passes, network share, clutch), Casual/Analyst, 7 languages | `check.py`: every mode × favourite × language renders; analyst ≠ casual |
| Player identification / focus mode | Favourite-player highlight and lens | manual demo |
| Narrative generation, explainability ("control vs chaos", pressure changes) | Network-narrowing cues, fault-line shifts, passing-spread timeline | `check.py` cue tests |
| Data innovation: create/extend synthetic datasets | Calibrated generator with team-strength prior, team shape, zone risk, pressure | `validate.py` |
| Multi-language storytelling | Language picker (7 languages) → `/explain` → Microsoft Foundry `gpt-4.1-mini`, numeric fact-check on every answer, template fallback | `check.py` fact-check tests; live demo |

## Judging rubric (20% each)

| Criterion | Evidence | Gap / next step |
|---|---|---|
| Technological implementation | 81/19 train/test split by time (4,940 / 1,190 real matches), regularisation tuned inside training years, 17/17 held-out checks, requirement and web/SSE tests in CI on every push | — |
| Agentic design & innovation | Multi-agent "Ask the analyst" on Microsoft Agent Framework: Phi-4-mini router (gated, hedged), embedding-salience memory, tool-using analyst (what-if counterfactual, Monte Carlo win probability), deterministic verifier with one self-correcting rewrite, number-preserving translator; full trace shown to viewers | — |
| Real-world impact & applicability | Broadcast-ready cue schema; latency budget; fragility is a scouting/tactics metric clubs could use on real data | See roadmap in README |
| User experience & presentation | Public web page (live + deep analysis, mobile-friendly) and Streamlit app | Record the < 2 min demo |
| Adherence to category | All five pipeline stages covered (table above) | — |

## Official rules (submission)

| Rule | Status |
|---|---|
| Public GitHub repo | ✅ https://github.com/harshul27/faultlines |
| Demo video < 2 min on YouTube/Vimeo, no third-party trademarks or copyrighted music | ⏳ fictional clubs used; no PL marks in the app |
| Pitch naming the Microsoft/Azure technologies used | ⏳ write it: Microsoft Foundry (gpt-4.1-mini, Phi-4-mini-instruct, text-embedding-3-small), Microsoft Agent Framework, Azure Container Apps, Azure Container Registry, managed identity / Entra ID, GitHub Actions, GitHub Pages |
| Third-party data used within its license (§4.3) | ✅ openfootball is CC0; published figures are facts with sources in `targets.json` |
| Working demo available to judges through the judging period (§4.8) | ⏳ deploy to Azure Container Apps |
| Deadline: 27 Oct 2026, 11:59 pm PT | ⏳ |
