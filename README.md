# Fault Lines

**Who holds the team together, and what happens without them?**

Fault Lines watches a football match event by event and finds each team's *fault line*: the player whose absence
would cut the team's passing connectivity the most. It explains why that matters in plain language, rates every
player against the coach's own game plan, and answers a coach's questions during the match, with every number
traceable to the data.

**Live demo:** https://harshul27.github.io/faultlines/ (mirror: https://faultlines-seven.vercel.app)

**Watch:** [demo video (1:44)](pitch/demo-video.mp4) · **Deck:** [PDF](pitch/fault-lines-pitch.pdf) ·
[PowerPoint, with speaker notes](pitch/fault-lines-pitch.pptx) · [animated HTML](pitch/deck/deck.html)

The demo runs on synthetic, football-realistic matches between two fictional clubs. You can also upload your own
match file or stream your own live events into it.

---

## What you can do with it

| Tab | What it shows |
|---|---|
| **Live** | A broadcast-style pitch: the passing network with the fault line ringed, live cues ("play now runs through…"), scorebug, momentum charts, and a short explanation for a casual fan or an analyst, in 7 languages. Follow either club and pick a favourite player. |
| **Deep analysis** | Paste a game plan (paragraphs or a sheet) and see every player rated against it: plan fit, form, load vs what the plan intends, who is overloaded, bypassed or fading. Ask the analyst anything a coach would ask: "Who should I take off?", "Is our press working?", "Who deals with their striker?", "Where should we attack?" |
| **Your data** | Upload a match (CSV in almost any layout, JSON, or StatsBomb events) or connect a live source. The data is cleaned, you get a quality report, and you can download a data package with the clean events and the analysis. |

## Where it applies

* **Broadcast and streaming graphics:** timed, schema-validated overlay cues and explanations generated alongside the match.
* **Coaching staff on the bench:** an assistant that knows the game plan, checks substitutions against what the team would lose, and never invents a number.
* **Opposition analysis and scouting:** who an opponent depends on, how marking that player changes their passing, and where they lose the ball.
* **Fan and second-screen apps:** the same insight written for a casual fan or an analyst, in the fan's language, around their favourite player.
* **Data teams:** a cleaning pipeline that turns messy provider files and live feeds into clean, documented, auditable data.

## How it works

```
 events (demo generator, your upload, or your live feed)
   │
   ▼
 Data pipeline ── standardise · validate · quarantine · clean · impute (flagged) · quality gate
   │
   ▼
 Live engine ──── rolling 15-minute pass network per team → fragility, progression, ball security, cues
   │
   ├──▶ Server-Sent Events ──▶ browser (live pitch, cues, charts)
   │
   ├──▶ Explanations ── Microsoft Foundry writes from computed facts; a fact-check rejects any unsupported number
   │
   └──▶ Analyst agents ── router → memory → tool-using analyst → verifier → translator (Microsoft Agent Framework)
```

**Fragility**: for each player, the share of the *other* players' passing connectivity that is lost if that player is
removed from the network (graph efficiency over share-weighted pass links). The highest is the fault line.

## Technologies and how they are used

| Technology | Used for | Where |
|---|---|---|
| **Microsoft Foundry: gpt-4.1-mini** | The analyst agent, explanations, reading a coach's game plan (JSON mode), translation | `src/agents.py`, `src/explain.py`, `src/gameplan.py`, `src/grounding.py` |
| **Microsoft Foundry: Phi-4-mini-instruct** | Fast router: classifies each question and stops off-topic ones before a large-model call | `src/agents.py` |
| **Microsoft Foundry: text-embedding-3-small** | Conversation memory: keeps only earlier turns relevant to the new question | `src/agents.py` |
| **Microsoft Agent Framework** | The tool-using analyst agent (10 match and game-plan tools), sessions, deterministic options | `src/agents.py` |
| **Azure Container Apps** | Hosts the API and the live match stream (one shared match clock, SSE, HTTPS ingress) | `src/server.py`, `Dockerfile` |
| **Managed identity / Microsoft Entra ID** | Keyless access to Foundry: no API keys anywhere | `src/ai.py` |
| **Azure Container Registry** | Stores the container image | `.github/workflows/image.yml` |
| **GitHub Actions** | Tests on every push, builds and pushes the image, publishes the page | `.github/workflows/` |
| **GitHub Pages / Vercel** | Static hosting for the web page | `web/` |
| Python: NetworkX, SciPy, NumPy | Pass networks, shortest paths, fragility, Monte Carlo win probability | `src/network.py`, `src/agents.py` |
| Python: scikit-learn | Team-strength model used to calibrate the generator on real results | `calibration/calibrate.py` |
| Python: Starlette, Uvicorn | Async API and Server-Sent Events | `src/server.py` |
| JSON Schema | Validates every broadcast cue | `src/stream.py` |
| Plain HTML, CSS, JavaScript | The page: SVG pitch and charts, no framework | `web/` |

## The analyst agents

Questions go to a small team of agents; the page shows the trace of every step.

| Agent | Model | Job |
|---|---|---|
| Router | Phi-4-mini-instruct | Question → intent + confidence. Off-topic questions stop here. If Phi is slow, gpt-4.1-mini races it after 2.5 s; keyword rules if both fail |
| Memory | text-embedding-3-small | Keeps earlier turns whose similarity to the new question clears a threshold |
| Analyst | gpt-4.1-mini via Agent Framework | Calls tools: `match_state`, `player_profile`, `what_if_remove`, `win_probability` (10,000-run Monte Carlo), `compare_windows`, `game_plan`, `assess_player`, `squad_assessment`, `plan_execution`, `zone_report` |
| Orchestrator | deterministic | For substitution questions, gathers the squad assessment and a what-if for every candidate and applies an auditable rule; for questions about an opponent, looks up who faces them |
| Verifier | deterministic | Every number must come from a tool output and every trend must keep its direction; a key player can't be recommended off without the what-if. One self-correcting rewrite, then a fallback built from the tool outputs |
| Translator | gpt-4.1-mini | Translates the verified answer; if any number changes, the English is shown |

## The game plan

The coach writes the plan in their own words or pastes a sheet (`player, position, instruction, priority`).
`src/gameplan.py` turns each instruction into weighted metrics and gives every player a relevance to the plan (1-5);
negations like "don't press" are respected. Each player is then rated on 14 metrics (involvement, completion,
progression, final-third and box entries, key passes, xA, xG, shots, press resistance, recoveries, high recoveries,
long passes, network share) against role norms, scaled to minutes played. Plan fit 50 = a typical player in that role.
Works without AI (keyword rules) and for any formation.

## Bring your own data

Every upload and live-feed event goes through `src/pipeline.py`:

| Stage | What happens |
|---|---|
| Ingest | Any CSV dialect, UTF-8 or Windows-1252, Fault Lines JSON, StatsBomb events; size and row limits |
| Standardise | Column names and values mapped (`Player Name`, `Complete`, decimal commas, `mm:ss` per-half clocks, club names) |
| Validate | Unrepairable rows go to quarantine with the reason; fouls, substitutions etc. are counted as out of scope |
| Clean | Time order, duplicates, coordinate scale (0-1, 0-100, metres, 120×80), attack direction per half, name variants |
| Impute | Missing receiver, outcome, end point, location or xG filled from neighbouring events, and flagged |
| Quality gate | A score with caveats; data too broken to analyse is stopped and the report says why |
| Deliver | A data package: clean events, quarantine, quality report, manifest with hashes, data dictionary, analysis |

Uploads are kept in memory only (never on disk); live feeds need a secret token. Example:

```bash
curl -X POST https://<server>/feeds -H "Content-Type: application/json" \
  -d '{"teams": {"home": {"name": "Your club", "players": [{"name": "A Player", "role": "CDM"}, {"name": "C Player", "role": "ST"}]}, "away": {"name": "Opponent", "players": [{"name": "B Player", "role": "ST"}]}}}'
curl -X POST https://<server>/feeds/<id>/events -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '[{"t": 12.5, "team": "home", "type": "pass", "player": "A Player", "recipient": "C Player", "x": 40, "y": 50, "end_x": 55, "end_y": 60, "success": true}]'
```

Field reference: the "Data format" section of the Your data tab, or the docstring in `src/sources.py`.

## How far to trust it

Every judgement-based part is tested against pass bars fixed before the first run
([reports/evaluation.md](reports/evaluation.md); earlier runs in `reports/history/`). Latest: **16 of 24 pass.**

| Holds up | Open, and what it means |
|---|---|
| Data pipeline on 20 deliberately messy matches: 100% of events recovered, fault line unchanged, 97% of broken rows quarantined, 0.01% of valid rows wrongly set aside | Teams in the simulation re-route play after losing a player, so fragility measures how much play depends on someone *now*, not guaranteed damage. A real-data study (injuries, red cards) would settle it |
| Agents on 26 coach questions: right tools 96%, 0 unchecked substitution advice, 4% fallbacks, model-judged quality 4.8/5 | Asked twice, the agents lead with the same recommendation 4 of 6 times (bar 80%) |
| Plan fit robust to the exact weights (Kendall tau 0.93) and early reads hold up (77% agree at 45') | Plan fit barely agrees between halves (rho 0.06): synthetic players have no individual quality |
| Plan reader: AI 93%, keyword rules 76% of hard cases (negations, sheets, other languages) | "Fading" is best read as a dip, not a lasting decline |
| Expected points added beats the earlier clutch metric (rho 0.60 vs -0.02) | It weights decisive moments, not dominance: use xG for dominance |

**Is the synthetic data realistic?** The generator is calibrated on public-domain match results
(openfootball, CC0; 2010/11-2022/23) and published league averages, then tested on later seasons it never saw:
**17 of 17 held-out checks pass** ([reports/validation.md](reports/validation.md)).

## Run it

```bash
pip install -r requirements.txt
uvicorn server:app --app-dir src --port 8000   # page + live stream at http://127.0.0.1:8000
python tests/check.py                          # capability tests
python tests/check_web.py                      # live stream and API tests
python tests/evaluate.py [--ai]                # evaluation report -> reports/evaluation.md
python calibration/validate.py                 # realism vs held-out real results -> reports/validation.md
python src/stream.py 23 60                     # headless replay: prints overlay cues as JSON lines
```

AI features need `AZURE_OPENAI_ENDPOINT` (and optionally `AZURE_OPENAI_DEPLOYMENT`, `FOUNDRY_ROUTER_DEPLOYMENT`,
`FOUNDRY_EMBED_DEPLOYMENT`) and an Azure sign-in (`az login`). Without them, everything else works and explanations
use deterministic templates.

## Deploy it

1. **Foundry:** deploy `gpt-4.1-mini`, `Phi-4-mini-instruct` and `text-embedding-3-small` in a Microsoft Foundry resource.
2. **Image:** `docker build -t faultlines .` (or let `.github/workflows/image.yml` build and push to your Azure Container Registry).
3. **Container App:** run the image with external ingress on port 8000, **one replica** (the live match clock lives in
   memory), a system-assigned managed identity with the *Cognitive Services OpenAI User* role on the Foundry
   resource, and these settings:

   | Variable | Meaning |
   |---|---|
   | `AZURE_OPENAI_ENDPOINT` | Your Foundry endpoint |
   | `ALLOW_ORIGIN` | Comma-separated page origins allowed to call the API |
   | `LIVE_SPEED` | Match seconds per real second for the demo (default 10) |
   | `AI_DAILY_CAP` | Daily limit on model calls (default 2000) |

4. **Page:** host `web/` on any static host (GitHub Pages via `.github/workflows/pages.yml`, or Vercel with root
   directory `web`). Set the API address in `web/config.js` or add `?live=<url>` to the page address.

The server also serves the page itself, so step 4 is optional.

## Repository map

```
src/            the application: API and live stream, analysis, agents, data pipeline
  server.py       API, live match stream (SSE), uploads and live feeds
  match.py        synthetic match generator (calibrated by calibration/, parameters in calibration.json)
  stream.py       live engine: one event at a time, timed broadcast cues
  network.py      pass networks, fragility, expected points added
  feed.py         snapshots and timelines for the page
  explain.py      explanations from computed facts
  grounding.py    fact-checking and number-safe translation
  agents.py       the analyst agents and their tools
  gameplan.py     game-plan reading, player assessment, plan execution, zones
  pipeline.py     data cleaning pipeline (validate, quarantine, clean, impute, quality, package)
  sources.py      readers for CSV, JSON and StatsBomb files
  ai.py           keyless Microsoft Foundry clients
  noise.py        realistic provider noise, for samples and robustness tests
  export.py       rebuilds the page's replay data and samples in web/data/
web/            the page (HTML, CSS, JavaScript) and its replay data
tests/          capability tests, live stream and API tests, the evaluation
calibration/    fits the generator on real results (public domain) and validates it on later seasons
reports/        evaluation and validation reports (earlier runs in reports/history/)
pitch/          demo video and the deck (PDF, PowerPoint, animated HTML with its storyline in deck/SCRIPT.md)
```

## Roadmap

1. **Value-weighted networks:** weight passes by possession value (xT, VAEP), so a fault line means lost threat, not lost volume.
2. **Learned counterfactuals:** re-simulate the match without the player (or with the actual substitute) instead of deleting a node.
3. **Uncertainty:** confidence intervals on fragility, and cues only when a change is statistically clear.
4. **Tracking data:** pitch control and open passing lanes, so off-ball availability counts.
5. **Real-world validation:** injuries, red cards and substitutions as natural experiments.
6. **Scale-out:** Azure Web PubSub between the stream processor and viewers, Event Hubs for provider feeds, persistent storage for uploads.

## Data and licences

Only public-domain data and published facts are used to calibrate the generator; no real match data ships with the
app. Clubs and players in the demo are fictional. If you analyse real provider data, use it within that provider's licence.
