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
| Personalize | Follow a club, a favourite player (with late-game clutch score), Casual/Analyst mode, 7 languages | `app.py` |

## Web page (live + deep analysis)
**https://harshul27.github.io/faultlines/**: a static page with two tabs:
* **Live:** broadcast-style pitch network with the fault line ringed, ball trail, lower-third cue graphics, scorebug,
  momentum sparklines and an event ticker. Follow either club, pick a favourite player, Casual or Analyst.
* **Deep analysis:** network dependence heat strip (players × minutes), player profile with dependence over time,
  pass map and passing partners, team connectivity, progression and ball security, analyst read, key moments.

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
| Live feed + `/explain` API | **Azure Container Apps** (`server.py`, `Dockerfile`) | 1 replica (shared match clock), 0.25 vCPU, external HTTPS ingress, SSE |
| AI explanations | **Microsoft Foundry**, `gpt-4.1-mini` (GlobalStandard) | Keyless: the container's **managed identity** has *Cognitive Services OpenAI User*; answers are fact-checked, cached per match minute, capped per day (`AI_DAILY_CAP`) |
| Image | **Azure Container Registry** (Basic) | Pulled with the app's managed identity (*AcrPull*), no registry passwords |
| Page | GitHub Pages | CORS on the API allows only the page's origin |

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
