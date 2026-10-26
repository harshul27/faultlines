# Fault Lines: who holds the team together?

A live pass-network overlay that finds each team's **single point of failure**, the player whose absence would
cut the team's passing connectivity the most, and explains it in plain language for each fan.

Built for *Inside the Game: Developer Hackathon* (challenge: The Synthetic Match Insights Engine).
All match data is synthetic; clubs and players are fictional. See [CRITERIA.md](CRITERIA.md) for how every
challenge requirement is met and tested.

## Pipeline
| Stage | What happens | File |
|---|---|---|
| Ingest | Seeded synthetic match generator, calibrated to Premier League reality; `LiveEngine` ingests events one by one as they arrive | `match.py`, `stream.py` |
| Interpret | Rolling pass network per team; **fragility** = share of the other players' passing connectivity lost if a player is removed; progression and ball security per window | `network.py` |
| Explain | Microsoft Foundry model writes the story from computed facts only; a fact-check rejects any number not in the facts | `explain.py` |
| Render | Pitch overlay, live cue feed, timed broadcast cues validated against a JSON Schema | `app.py`, `stream.py` |
| Personalize | Follow a club, a favourite player (with expected points added), Casual/Analyst mode, 7 languages | `app.py` |

## Web page (live + deep analysis)
**https://harshul27.github.io/faultlines/**: a static page with two tabs:
* **Live:** broadcast-style pitch network with the fault line ringed, ball trail, lower-third cue graphics, scorebug,
  momentum sparklines and an event ticker. Follow either club, pick a favourite player, Casual or Analyst.
* **Deep analysis:** network dependence heat strip (players × minutes), player profile with dependence over time,
  pass map and passing partners, passing spread, progression and ball security, analyst read, key moments.

Architecture (modelled on live sports data distribution: provider feed → stream processor → pub/sub → clients):
```
match feed (generator) -> LiveEngine (stream processor) -> pub/sub -> SSE /live  -> browser (updates per event)
static CDN: page + precomputed match timelines            -> replay mode + analysis (works without the server)
```
`server.py` runs one shared match clock and pushes every event over Server-Sent Events: late joiners get the state
of play, reconnecting clients resume from `Last-Event-ID`, slow clients are dropped (back-pressure), and analytics run
off the event loop. Scale-out path: Azure Web PubSub between processor and clients. Point the page at a hosted server
by setting `web/config.js` (or `?live=<url>`).
```bash
python export.py                        # rebuild web/data/ replay timelines
uvicorn server:app --port 8000          # live feed + page at http://127.0.0.1:8000
python check_web.py                     # static data + live SSE protocol tests
```

### Azure deployment
| Piece | Azure service | Notes |
|---|---|---|
| Live feed + `/explain`, `/ask`, `/plan`, `/assess` API | **Azure Container Apps**, workload-profiles environment (`server.py`, `Dockerfile`) | 1 replica (shared match clock), 0.5 vCPU / 1 GiB, external HTTPS ingress, SSE. Express environments can't use managed identity, so this runs on a standard one |
| AI explanations + agents | **Microsoft Foundry**: `gpt-4.1-mini` (analyst, narration, translation), `Phi-4-mini-instruct` (router), `text-embedding-3-small` (memory) | Keyless: the container's **managed identity** has *Cognitive Services OpenAI User*; answers are fact-checked, cached per match minute, capped per day (`AI_DAILY_CAP`) |
| Image | **Azure Container Registry** (Basic), built by **GitHub Actions** (`.github/workflows/image.yml`) | ACR Tasks are blocked on student subscriptions, so CI builds and pushes `:sha` and `:latest`; registry credentials live only in GitHub and Container Apps secrets |
| Page | GitHub Pages | CORS on the API allows only the page's origin |

## AI agents: "Ask the analyst"
Viewers ask free-text questions on the Deep analysis tab (`POST /ask`). A small team of agents answers, built with
**Microsoft Agent Framework** on **Microsoft Foundry**, and the page shows a trace of every step:

| Agent | Model | Job |
|---|---|---|
| Router | **Phi-4-mini-instruct** | Micro-decision gate: question → JSON intent + confidence. Off-topic or unclear questions stop here (no big-model call). Hedged request: if Phi is slow, gpt-4.1-mini races it after 2.5 s; keyword rules if both fail |
| Memory | text-embedding-3-small | Keeps only earlier turns whose embedding similarity to the new question clears a salience threshold, not the last N |
| Analyst | gpt-4.1-mini via **Agent Framework** | Calls match tools: `match_state`, `player_profile`, `what_if_remove` (counterfactual network without a player), `win_probability` (10,000-run Monte Carlo from the current score), `compare_windows`; and game-plan tools: `game_plan`, `assess_player`, `squad_assessment` (incl. who to take off), `plan_execution`, `zone_report` |
| Verifier | deterministic | Every number must come from a tool output and every trend must keep its direction (`grounding.py`); on failure the analyst gets the critique and rewrites once (self-reflection), else a deterministic answer from the tool outputs |
| Translator | gpt-4.1-mini | Translates only the verified English answer; the numbers must survive unchanged or the English is shown |

Cost and safety: inputs validated, AI calls capped per day, answers rendered as text (never HTML), keyless managed identity.

## Game plan: every player judged against the coach's tactics
The coach pastes the game plan on the Deep analysis tab: paragraphs, bullet points or a sheet
(`player, position, instruction, priority`). `gameplan.py` turns it into weighted, measurable instructions and the
agents answer coach questions against it ("Who should I take off?", "Is our press working?", "How is Brightwell doing
in his role?", "Where should we attack?").

1. **Read the plan** (`POST /plan`): gpt-4.1-mini in JSON mode maps each instruction to metrics with weights 1-3 and
   gives every player a relevance to the plan (1-5). Keyword rules do it when AI is off. Either way the result is
   validated (`normalise`): known metrics only, weights and relevance clamped, names resolved to the squad.
2. **Weight each player**: role job description (e.g. a CB on completion, progression, recoveries) + every team
   principle that covers the role + the player's own duty, counted double.
3. **Rate against role norms**: 14 metrics per player (involvement, completion, progressive passes, final-third and box
   entries, key passes, xA, xG, shots, press resistance, recoveries, high recoveries, long passes, network share),
   each compared with the role's norm from 40 simulated league matches and scaled to the minutes played (Poisson /
   binomial z-scores, so an early read isn't over-confident). Plan fit = 50 + 15 × weighted z, so 50 = league norm.
4. **Load vs plan**: each player's share of the team's on-ball actions against the share their relevance implies.
   Flags: *overloaded* (the team leans on them more than the plan intends), *bypassed* (a key role isn't getting the
   ball), *off plan*, *on plan*, *fading* (last 15 minutes vs before).
5. **Plan execution and zones**: each team principle scored as working / mixed / not working, with who executes it
   best and least. Plus where the team enters the final third, shoots, loses and wins the ball.

`python check.py` asserts that league-average players average a plan fit of about 50, that untrusted plans are
cleaned, that sheet priorities parse, and that the demo hub shows as overloaded and then fading once man-marked.
`/assess` needs no AI. Limits: there is no bench data (candidates are who to take off), no tracking data (no
off-ball runs or distance covered), and the norms come from the synthetic league.


## How far to trust each part: evaluation
`python evaluate.py [--ai]` tests the judgement-based parts against pass bars fixed in code before the first run, on
held-out simulated matches, and writes [reports/evaluation.md](reports/evaluation.md). Earlier runs and every change
between them are kept in `reports/`; no pass bar was moved. Latest: **12 of 20 pass.**

| Holds up | Fails, and what it means |
|---|---|
| Plan fit is robust to the exact weights (Kendall tau 0.93 under +-50% changes) and early reads hold (77% agree at 45') | Plan fit barely agrees between halves (rho 0.06): synthetic players have no individual quality, so real-data reliability is still unproven |
| Flags (1.7 per team per check) and cues (3 per match) after calibrating thresholds on ordinary matches | 'Fading' detects a real decline (man-marking) but in ordinary play flagged players bounce back: read it as a dip |
| Expected points added beats the old clutch points (rho 0.60 vs -0.02 with realised decisive contributions) | It does not track results (by design it weights moments, not dominance): use xG for dominance |
| AI plan reader 100% of hard cases (negations, sheets, Spanish, opponent instructions); rules 76% | Fault lines: in simulation, teams re-route play after losing a player, so fragility measures dependence now, not certain damage. The what-if now says so; the injury/red-card study on real data decides it |
| Agents on 26 coach questions: right tools 96%, 0 coaching-guard violations, 4% fallbacks, model-judged quality 5.0/5; asked twice, the same recommendation 6 of 6 (substitutions follow an auditable rule; flanks come from fixed matchups) | Asked twice, answers still differ in the supporting players they mention (overlap test 67%, bar 80%) |

The report ends with the real-data studies, fixed in advance, that would settle each open question.

## Run
```bash
pip install -r requirements.txt
python check.py          # requirement tests (live latency, cue schema, grounding, personalization, storyline)
python validate.py       # realism vs held-out real data -> reports/validation.md
streamlit run app.py
python stream.py 23 60   # headless live replay: prints overlay cues as JSON lines
```
For AI explanations, sign in with `az login` and set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT`
(no keys; uses your Entra ID sign-in). Without them the app uses deterministic templates.

## How realistic is the synthetic data?
Only permissively licensed data and published facts are used, and real data never ships in the app:
* **openfootball/football.json** (CC0, public domain): every Premier League result 2010/11 to the current season.
* **Published league averages** from premierleague.com (passes, accuracy, long balls, high turnovers; see `targets.json`).

`calibrate.py` trains on **2010/11–2022/23 (4,940 matches)**; `validate.py` tests on **2023/24–present (1,190 matches)**,
an 81/19 split by time so nothing from the future leaks into training. Pass statistics are fitted on 2023/24 and
tested on 2024/25 and 2025/26. Current result: **17/17 held-out checks pass** ([reports/validation.md](reports/validation.md)).

* A Poisson team-strength model (regularisation tuned inside the training years only) beats the base-rate baseline
  on every held-out match: log loss 1.016 vs 1.075, accuracy 49.4% vs 42.9%. Its fitted home advantage and
  spread of club strengths drive the generator, so synthetic results have real home/draw/away rates.
* The generator matches published passes, accuracy, long balls and high turnovers within each season's real drift.
* Known limitation: synthetic goal counts are a little more spread out than real ones (variance 3.2 vs 2.7).

## The built-in storyline (seed 23)
Northbridge FC route play through holding midfielder Luca Ferrand, who holds about a quarter of their passing
network. From 60' Riverside Athletic man-mark him. Across 20 paired matches (same seed with and without the marking),
the marking doesn't stop Northbridge passing; it **stops them moving the ball forward**: pass completion falls about
4 points and progressive passes fall about 0.3 per minute relative to the unmarked match (`check.py` tests this).

## Roadmap: from synthetic demo to real-world analysis
The event schema in `match.py` is the only contract; a real feed (Opta/Stats Perform, StatsBomb, Wyscout, normalised
with `kloppy`) can replace the generator behind `LiveEngine.ingest()` under the provider's licence.
1. **Value-weighted networks:** weight passes by possession value (xT / VAEP / OBV) so a fault line means lost threat, not lost volume.
2. **Counterfactual impact:** replace graph deletion with a learned possession model that re-simulates the match without the player (or with the actual substitute).
3. **Uncertainty:** bootstrap confidence intervals on fragility; only cue changes that are statistically clear.
4. **Tracking data:** pitch control and open passing lanes, so off-ball availability counts, not just touches.
5. **Real-world validation:** natural experiments (injuries, red cards, substitutions) to test that high fragility predicts a drop in team output.
6. **Production feed handling:** real feeds correct and delete events; make ingest idempotent and order-tolerant, run on Azure Event Hubs + Container Apps, and use `validate.py` as a drift monitor.
