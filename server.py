"""Live feed server: one shared match clock, pushed to every viewer over Server-Sent Events.

    uvicorn server:app --port 8000          # then open http://127.0.0.1:8000
    LIVE_SPEED=10 (match seconds per wall second), ALLOW_ORIGIN=* (CORS for a separately hosted page)
    AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT enable AI explanations (managed identity, no keys)

Mirrors how live sports data is distributed: provider feed -> stream processor -> pub/sub fan-out -> clients.
Late joiners get a state-of-play message; reconnecting clients resume from Last-Event-ID; slow clients are dropped.
Users can bring their own data: upload a match (POST /matches) or open a live push feed (POST /feeds); see sources.py.
ponytail: in-process pub/sub, one replica. Scale-out path: Azure Web PubSub (or Redis) between processor and clients.
"""
import asyncio
import json
import logging
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import agents
import ai
import gameplan
import pipeline
import sources
from explain import explain, facts, template
from feed import DEMO_SEEDS, compact_event, match_meta, snapshot, timeline, user_meta
from match import TEAMS, simulate, using
from network import score_at, team_view
from stream import LiveEngine

SPEED = float(os.getenv("LIVE_SPEED", "10"))
HISTORY = 600  # messages kept for Last-Event-ID resume
LANGUAGES = {"English", "Spanish", "French", "Portuguese", "Hindi", "Arabic", "Japanese"}
AI_DAILY_CAP = int(os.getenv("AI_DAILY_CAP", "2000"))  # protects credits; beyond it, grounded templates only


class Channel:
    """One match clock and its subscribers: the demo replay loop, or a user's live push feed."""
    def __init__(self, lineups=None):
        self.seq, self.history, self.subscribers = 0, deque(maxlen=HISTORY), set()
        self.meta, self.state, self.events = None, None, []
        self.minutes, self.cues = {}, []  # one snapshot per match minute + all cues: history for late joiners
        self.seed, self.raw = None, []    # current match + full-fidelity events for on-demand explanations
        self.lineups, self.lock, self.touched, self.token = lineups, asyncio.Lock(), time.time(), None

    def publish(self, kind, data):
        self.seq += 1
        msg = (self.seq, kind, json.dumps({**data, "server_ts": time.time() * 1000}, separators=(",", ":")))
        self.history.append(msg)
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # back-pressure: a stalled client must not slow the feed for everyone
                self.subscribers.discard(q)

    def start(self, meta, seed):
        self.seed, self.raw, self.meta, self.state, self.events = seed, [], meta, None, []
        self.minutes, self.cues, self.engine = {}, [], LiveEngine()
        self.publish("kickoff", self.meta)

    async def ingest(self, e):
        """One event as it happens: overlay cues, a fresh snapshot on completed passes and goals, publish."""
        self.touched = time.time()
        with using(self.lineups):
            self.raw.append(e)
            cues, ms = self.engine.ingest(e)
            ce = compact_event(e)
            self.events.append(ce)
            msg = {"event": ce, "cues": cues, "process_ms": round(ms, 1)}
            if (e["type"] == "pass" and e["success"]) or e["goal"] or self.state is None:
                self.state = await asyncio.to_thread(snapshot, self.raw, e["t"] / 60)
                msg["snapshot"] = self.state
                self.minutes.setdefault(int(e["t"] // 60), self.state)
            self.cues += cues
            self.publish("event", msg)

    async def run(self):
        """The demo match clock: replay synthetic matches in wall-clock time, forever."""
        i = 0
        while True:
            seed = DEMO_SEEDS[i % len(DEMO_SEEDS)]
            i += 1
            try:
                self.start(match_meta(seed), seed)
                t0 = time.monotonic()
                for e in simulate(seed):
                    wait = e["t"] / SPEED - (time.monotonic() - t0)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await self.ingest(e)
                self.publish("fulltime", {"score": self.state["score"] if self.state else None})
            except Exception:  # a background task's exception is otherwise silent: the live feed would just stop
                logging.exception("demo match %s failed; moving to the next one", seed)
            await asyncio.sleep(15)


channel = Channel()
uploads, feeds = {}, {}  # users' matches and live push feeds: in memory only, never written to disk
MAX_UPLOADS, MAX_FEEDS, FEED_IDLE = 20, 10, 3 * 3600
_processing = asyncio.Semaphore(1)  # one upload analysed at a time, so the live demo keeps its pace


async def live(request):
    fid = request.query_params.get("feed")
    if fid is not None and fid not in feeds:
        return JSONResponse({"error": "unknown feed"}, status_code=404)
    ch = feeds[fid] if fid else channel
    q = asyncio.Queue(maxsize=200)
    try:
        last = int(request.headers.get("last-event-id", 0) or 0)
    except ValueError:  # unreadable resume id: treat as a fresh join
        last = 0

    async def body():
        ch.subscribers.add(q)
        sent = 0  # highest id delivered; anything at or below it is never sent twice
        try:
            oldest = ch.history[0][0] if ch.history else ch.seq + 1
            if last and oldest - 1 <= last <= ch.seq:  # resume exactly where the client left off (even if caught up)
                sent = last
                for m in list(ch.history):
                    if m[0] > sent:
                        sent = m[0]
                        yield f"id: {m[0]}\nevent: {m[1]}\ndata: {m[2]}\n\n"
            else:  # fresh join, or too far behind to resume: state of play
                hello = {"meta": ch.meta, "snapshot": ch.state, "events": ch.events,
                         "history": list(ch.minutes.values()), "cues": ch.cues,
                         "speed": SPEED, "server_ts": time.time() * 1000}
                sent = ch.seq
                yield f"id: {sent}\nevent: hello\ndata: {json.dumps(hello, separators=(',', ':'))}\n\n"
            while True:
                try:
                    seq, kind, data = await asyncio.wait_for(q.get(), timeout=15)
                    if seq <= sent:  # already delivered in the resume/hello above
                        continue
                    sent = seq
                    yield f"id: {seq}\nevent: {kind}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # keeps proxies from closing an idle stream
        finally:
            ch.subscribers.discard(q)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@lru_cache(maxsize=8)
def _match(seed):
    return list(simulate(seed))


_ai = {"day": None, "n": 0, "lock": threading.Lock()}


def _spend(n):
    """Charge n model calls against today's cap; False when the cap would be exceeded (thread-safe)."""
    with _ai["lock"]:
        day = time.strftime("%Y-%m-%d")
        if _ai["day"] != day:
            _ai.update(day=day, n=0)
        if _ai["n"] + n > AI_DAILY_CAP:
            return False
        _ai["n"] += n
        return True


def _parse_seed(raw):
    raw = str(raw if raw is not None else "live")
    if raw == "live":
        return raw
    kind, _, sid = raw.partition(":")
    if kind in ("upload", "feed"):
        if sid not in (uploads if kind == "upload" else feeds):
            raise ValueError("unknown match")
        return raw
    seed = int(raw)
    if seed not in DEMO_SEEDS:
        raise ValueError("invalid seed")
    return seed


def _source(seed):
    """-> (events, lineups or None, seed for the agents): demo live feed, demo replay, user upload or user feed."""
    if seed == "live":
        return channel.raw, None, channel.seed
    if isinstance(seed, int):
        return _match(seed), None, seed
    kind, _, sid = seed.partition(":")
    if kind == "upload":
        return uploads[sid]["events"], uploads[sid]["lineups"], None
    return feeds[sid].raw, feeds[sid].lineups, None


def _events(seed, minute):
    return [e for e in _source(seed)[0] if e["t"] < minute * 60]


_explained = {}  # only successful AI answers are cached; failures retry on the next request


def _explain(seed, minute, team, mode, lang, fav, key):
    if key in _explained:
        return _explained[key]
    events = _events(seed, minute)
    view, opp = team_view(events, team, minute), team_view(events, "away" if team == "home" else "home", minute)
    f = facts(view, opp, score_at(events, minute * 60), fav or None)
    if not _spend(2 if lang != "English" else 1):
        return template(f, mode), "template (daily AI cap reached)"
    text, source = explain(f, mode, lang)
    if source.startswith("ai") and len(_explained) < 5000:
        _explained[key] = (text, source)
    return text, source


def _parse_common(q):
    seed = _parse_seed(q.get("seed", "live"))
    minute = int(q.get("minute", 0))
    if not 1 <= minute <= 130:
        raise ValueError("invalid minute")
    events = _source(seed)[0]
    if not events or minute > events[-1]["t"] / 60 + 1:
        raise LookupError("minute not played yet" if not isinstance(seed, int) else "the match ended before that minute")
    return seed, minute


async def explain_route(request):
    """Grounded explanation for one team at one match minute. Public endpoint, so every input is validated."""
    q = request.query_params
    try:
        seed, minute = _parse_common(q)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except (ValueError, KeyError):
        return JSONResponse({"error": "invalid seed or minute"}, status_code=400)
    team, mode, lang, fav = q.get("team", "home"), q.get("mode", "Casual"), q.get("lang", "English"), q.get("fav", "")
    events, lineups, _ = _source(seed)
    with using(lineups):
        if team not in TEAMS or mode not in ("Casual", "Analyst") or lang not in LANGUAGES or (fav and fav not in TEAMS[team]["players"]):
            return JSONResponse({"error": "invalid parameters"}, status_code=400)
        events_key = (seed if seed != "live" else channel.seed, sum(e["t"] < minute * 60 for e in events)) if not isinstance(seed, int) else seed
        key = (events_key, minute, team, mode, lang, fav)
        text, source = await asyncio.to_thread(_explain, seed, minute, team, mode, lang, fav, key)
    return JSONResponse({"text": text, "source": source, "minute": minute, "language": lang})


async def ask_route(request):
    """Ask the analyst agents a free-text question about the match at a given minute."""
    if not ai.enabled():
        return JSONResponse({"error": "AI is not configured on this server"}, status_code=503)
    try:
        body = await request.json()
        seed, minute = _parse_common(body)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    question, lang = str(body.get("question", "")).strip(), body.get("lang", "English")
    history = [{"q": str(h.get("q", ""))[:300], "a": str(h.get("a", ""))[:800]}
               for h in (body.get("history") or [])[-6:] if isinstance(h, dict)]
    if not 3 <= len(question) <= 300 or lang not in LANGUAGES:
        return JSONResponse({"error": "question must be 3-300 characters; language must be supported"}, status_code=400)
    if not _spend(6):  # worst case per question: memory, router + hedge, analyst x2, translator
        return JSONResponse({"error": "daily AI limit reached; try again tomorrow"}, status_code=429)
    _, lineups, agent_seed = _source(seed)
    with using(lineups):
        try:
            plan_team, plan = _plan_from(body)
        except (ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "invalid game plan"}, status_code=400)
        try:
            out = await agents.ask(question, _events(seed, minute), minute, agent_seed, lang, history, plan, plan_team)
        except Exception as e:  # a model outage must not take the page down
            return JSONResponse({"error": f"agents unavailable ({type(e).__name__}); try again"}, status_code=502)
    return JSONResponse({**out, "minute": minute})


def _plan_from(body):
    """(team, normalised plan or None) from a request body; the browser's copy of the plan is re-validated."""
    team = body.get("plan_team", "home")
    if team not in TEAMS:
        raise ValueError("invalid team")
    plan = body.get("plan")
    if plan is None:
        return team, None
    if not isinstance(plan, dict) or len(json.dumps(plan)) > 20_000:
        raise ValueError("invalid plan")
    return team, gameplan.normalise(plan, team)


async def plan_route(request):
    """Coach's game plan (free text or a pasted sheet) -> structured, weighted plan. Works without AI (rules).
    GET ?team=&seed= returns the default plan's text and its reading (demo clubs' plans, or a generic one)."""
    try:
        body = dict(request.query_params) if request.method == "GET" else await request.json()
        team, lineups = body.get("team", "home"), _source(_parse_seed(body.get("seed", "live")))[1]
    except (ValueError, KeyError, AttributeError, TypeError):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    with using(lineups):
        if team not in TEAMS:
            return JSONResponse({"error": "invalid team"}, status_code=400)
        if request.method == "GET":
            text = gameplan.GENERIC_PLAN if lineups else gameplan.DEFAULT_PLANS[team]
            return JSONResponse({"text": text, "plan": gameplan.default_plan(team), "source": "keyword rules", "metrics": gameplan.METRICS})
        text = str(body.get("text", "")).strip()
        if not 10 <= len(text) <= 6000:
            return JSONResponse({"error": "plan must be 10-6000 characters"}, status_code=400)
        if ai.enabled() and _spend(1):
            plan, source = await asyncio.to_thread(gameplan.parse, text, team)
        else:
            plan, source = gameplan.normalise(gameplan.parse_rules(text, team), team), "rules"
    return JSONResponse({"plan": plan, "source": source, "metrics": gameplan.METRICS})


async def assess_route(request):
    """Squad assessment, plan execution and zones for one team at one minute. Deterministic: no AI needed."""
    try:
        body = await request.json()
        seed, minute = _parse_common(body)
        plan_team, plan = _plan_from(body)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    events, lineups = _events(seed, minute), _source(seed)[1]

    def run():
        with using(lineups):
            p = plan or gameplan.default_plan(plan_team)
            return {"team": plan_team, "minute": minute, "plan": p,
                    "players": gameplan.assess(events, plan_team, minute, p),
                    "execution": gameplan.execution(events, plan_team, minute, p),
                    "zones": gameplan.zones(events, plan_team, minute),
                    "metrics": gameplan.METRICS}
    return JSONResponse(await asyncio.to_thread(run))


SCALE_OPTIONS = ("auto", "percent", "unit", "metres", "statsbomb")
DIRECTION_OPTIONS = ("auto", "attacking", "fixed_home_right", "fixed_home_left")


async def upload_route(request):
    """POST /matches {text, format, home, away, scale, direction, encoding}: a user's match through the cleaning pipeline
    (pipeline.py) -> analysed timeline + quality report. Kept in this server's memory only (newest 20), never on disk."""
    if int(request.headers.get("content-length") or 0) > 9_000_000:
        return JSONResponse({"error": "file too large (8 MB limit)"}, status_code=413)
    try:
        body = await request.json()
        text, fmt = body["text"], body.get("format", "auto")
        scale, direction = body.get("scale", "auto"), body.get("direction", "auto")
        names = [str(body.get(k) or "").strip() or None for k in ("home", "away")]
        encoding = str(body.get("encoding", "utf-8"))[:20]
        if not isinstance(text, str) or fmt not in ("auto", "native", "csv", "statsbomb") or scale not in SCALE_OPTIONS \
                or direction not in DIRECTION_OPTIONS:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "send {text, format, scale, direction} with the file's contents"}, status_code=400)
    async with _processing:
        try:
            lineups, events, quarantine, report = await asyncio.to_thread(
                sources.parse, text, fmt, names[0], names[1], scale, direction, encoding)
        except sources.DataError as e:  # the user still gets the report, so they can see what to fix
            return JSONResponse({"error": str(e), "report": e.report}, status_code=422)
        seed = f"upload:{lineups['id']}"

        def build():
            with using(lineups):
                return timeline(seed, events=events, meta=user_meta(lineups, seed))
        tl = await asyncio.to_thread(build)
    while len(uploads) >= MAX_UPLOADS:
        uploads.pop(next(iter(uploads)))
    uploads[lineups["id"]] = {"lineups": lineups, "events": events, "timeline": tl, "quarantine": quarantine,
                              "report": report, "input": pipeline.fingerprint(text)}
    return JSONResponse({"id": lineups["id"], "seed": seed, "report": report, "teams": tl["meta"]["teams"],
                         "summary": {"events": len(events), "minutes": int(events[-1]["t"] // 60) + 1,
                                     "score": tl["snapshots"][-1]["score"] if tl["snapshots"] else {"home": 0, "away": 0}}})


async def match_route(request):
    u = uploads.get(request.path_params["id"])
    return JSONResponse(u["timeline"]) if u else JSONResponse({"error": "unknown match (uploads live in memory)"}, status_code=404)


async def match_report(request):
    u = uploads.get(request.path_params["id"])
    return JSONResponse(u["report"]) if u else JSONResponse({"error": "unknown match"}, status_code=404)


def _analysis_files(lineups, events, snapshots=None):
    """Analysis outputs for the package: fault lines minute by minute, and every player against the default plan."""
    with using(lineups):
        end = int(events[-1]["t"] // 60) + 1
        lines = ["minute,team,fault_line,network_share,passing_spread,progressive_passes_last_15,pass_completion_last_15"]
        views = ([(s["minute"], t, s["teams"][t]) for s in snapshots for t in ("home", "away")] if snapshots else
                 [(m, t, team_view(events, t, m)) for m in range(5, end + 1, 5) for t in ("home", "away")])
        for m, t, v in views:
            spof = v["spof"]["name"] if isinstance(v["spof"], dict) else v["spof"]
            share = next(p["fragility"] for p in v["players"] if p["name"] == spof)
            lines.append(f"{int(m)},{t},{spof},{share},{v['efficiency']},{v['progressive']},{v['completion']}")
        rows = ["team,player,role,plan_fit,plan_fit_last_15,form,load_vs_plan,flags,strengths,concerns"]
        q = lambda x: '"' + str(x).replace('"', "'") + '"'
        for t in ("home", "away"):
            for r in gameplan.assess(events, t, end, gameplan.default_plan(t)):
                rows.append(",".join([t, q(r["player"]), r["role"], str(r["plan_fit"]), str(r["plan_fit_last_15"]), r["form"],
                                      r["load_vs_plan"], q("; ".join(r["flags"])), q("; ".join(r["strengths"])), q("; ".join(r["concerns"]))]))
    return {"analysis/fault_lines_by_minute.csv": "\n".join(lines) + "\n",
            "analysis/player_assessment_full_time.csv": "\n".join(rows) + "\n"}


def _download(data, name):
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


async def match_package(request):
    """GET /matches/{id}/package: everything back to the user, so their data never has to live on this server."""
    u = uploads.get(request.path_params["id"])
    if u is None:
        return JSONResponse({"error": "unknown match (uploads live in memory; upload it again)"}, status_code=404)

    def build():
        extra = _analysis_files(u["lineups"], u["events"], u["timeline"]["snapshots"])
        return pipeline.package(u["lineups"], u["events"], u["quarantine"], u["report"], u["input"], extra)
    return _download(await asyncio.to_thread(build), f"faultlines-{request.path_params['id']}.zip")


async def feed_create(request):
    """POST /feeds {teams, coordinates: {scale, direction}}: open a live feed for a user's own data source.
    Returns a secret token for pushing. Events are cleaned as they arrive (pipeline.LiveCleaner)."""
    try:
        body = await request.json()
        lineups = sources.lineup(body.get("teams"))
        coords = body.get("coordinates") or {}
        scale, direction = coords.get("scale", "percent"), coords.get("direction", "attacking")
        if scale not in SCALE_OPTIONS[1:] or direction not in DIRECTION_OPTIONS[1:]:
            raise ValueError
    except sources.DataError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except (ValueError, AttributeError, TypeError):
        return JSONResponse({"error": "send {teams: {home: {name, players: [{name, role}]}, away: ...}, coordinates: "
                                      "{scale: percent|unit|metres|statsbomb, direction: attacking|fixed_home_right|fixed_home_left}}"}, status_code=400)
    for sid in [k for k, f in feeds.items() if time.time() - f.touched > FEED_IDLE]:
        feeds.pop(sid)
    if len(feeds) >= MAX_FEEDS:
        return JSONResponse({"error": "too many open feeds; try again later"}, status_code=429)
    sid = lineups["id"]
    ch = Channel(lineups)
    ch.token, ch.cleaner = secrets.token_urlsafe(24), pipeline.LiveCleaner(lineups, scale, direction)
    ch.start(user_meta(lineups, f"feed:{sid}"), f"feed:{sid}")
    feeds[sid] = ch
    return JSONResponse({"id": sid, "seed": f"feed:{sid}", "token": ch.token, "push": f"/feeds/{sid}/events",
                         "watch": f"/live?feed={sid}", "report": f"/feeds/{sid}/report", "package": f"/feeds/{sid}/package",
                         "teams": ch.meta["teams"]})


async def feed_push(request):
    """POST /feeds/{id}/events (Authorization: Bearer <token>): up to 500 events as they happen. Each is cleaned;
    bad or late events are quarantined (see /report), never fail the batch; a pass missing its receiver waits for the next event."""
    ch = feeds.get(request.path_params["id"])
    if ch is None:
        return JSONResponse({"error": "unknown feed"}, status_code=404)
    if not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {ch.token}"):
        return JSONResponse({"error": "missing or wrong token"}, status_code=401)
    try:
        body = await request.json()
        raw = body if isinstance(body, list) else body.get("events")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 500:
            raise ValueError
    except (ValueError, AttributeError):
        return JSONResponse({"error": "send a list of 1-500 events"}, status_code=400)
    async with ch.lock:
        before = len(ch.cleaner.log.quarantine)
        events = ch.cleaner.push(raw)
        for e in events:
            await ch.ingest(e)
    rep = ch.cleaner.report()
    return JSONResponse({"received": len(raw), "accepted": len(events), "held_for_next_event": rep["held_for_next_event"],
                         "quarantined": rep["quarantined"] - before, "quarantine": ch.cleaner.log.quarantine[before:][:20],
                         "minute": round(ch.raw[-1]["t"] / 60, 2) if ch.raw else 0})


def _feed_report(ch):
    c = ch.cleaner
    names = {s: ch.lineups[s]["name"] for s in ("home", "away")}
    rep = pipeline.quality(c.received, range(c.received), ch.raw, c.log, "declared", c.scale, names, "live feed", "utf-8", time.time())
    rep["live"] = c.report()
    return rep


async def feed_report(request):
    ch = feeds.get(request.path_params["id"])
    return JSONResponse(_feed_report(ch)) if ch else JSONResponse({"error": "unknown feed"}, status_code=404)


async def feed_package(request):
    """GET /feeds/{id}/package: the feed's clean events, quarantine, report and analysis so far (or at full time)."""
    ch = feeds.get(request.path_params["id"])
    if ch is None or not ch.raw:
        return JSONResponse({"error": "unknown feed, or no events yet"}, status_code=404)
    async with ch.lock:
        events, quarantine, report = list(ch.raw), list(ch.cleaner.log.quarantine), _feed_report(ch)

    def build():
        info = {"sha256": None, "characters": None, "note": "live feed: events arrived in batches"}
        return pipeline.package(ch.lineups, events, quarantine, report, info, _analysis_files(ch.lineups, events))
    return _download(await asyncio.to_thread(build), f"faultlines-feed-{request.path_params['id']}.zip")


async def health(_):
    return JSONResponse({"ok": True, "subscribers": len(channel.subscribers), "seq": channel.seq,
                         "minute": channel.state["minute"] if channel.state else None, "speed": SPEED,
                         "ai": ai.enabled(), "uploads": len(uploads), "feeds": len(feeds)})


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(channel.run())
    def warm():  # one at a time: parallel CPU-bound warm-ups starve the event loop and the first page load stalls
        gameplan.norms()  # role norms for player assessment
        for seed in DEMO_SEEDS:  # Monte Carlo rates, so the first prediction question is instant
            agents._goal_rates(seed)
    asyncio.get_running_loop().run_in_executor(None, warm)
    if ai.enabled():  # first model calls are slow (sign-in, cold deployments): take that hit before viewers do
        asyncio.get_running_loop().run_in_executor(None, agents.warm_up)
    yield
    task.cancel()


app = Starlette(
    routes=[Route("/live", live), Route("/health", health), Route("/explain", explain_route), Route("/ask", ask_route, methods=["POST"]),
            Route("/plan", plan_route, methods=["GET", "POST"]), Route("/assess", assess_route, methods=["POST"]),
            Route("/matches", upload_route, methods=["POST"]), Route("/matches/{id}", match_route),
            Route("/matches/{id}/report", match_report), Route("/matches/{id}/package", match_package),
            Route("/feeds", feed_create, methods=["POST"]), Route("/feeds/{id}/events", feed_push, methods=["POST"]),
            Route("/feeds/{id}/report", feed_report), Route("/feeds/{id}/package", feed_package),
            Mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))],
    middleware=[Middleware(CORSMiddleware, allow_origins=os.getenv("ALLOW_ORIGIN", "*").split(","), allow_methods=["GET", "POST"], allow_headers=["content-type", "authorization"])],
    lifespan=lifespan)
