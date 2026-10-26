"""Live feed server: one shared match clock, pushed to every viewer over Server-Sent Events.

    uvicorn server:app --port 8000          # then open http://127.0.0.1:8000
    LIVE_SPEED=10 (match seconds per wall second), ALLOW_ORIGIN=* (CORS for a separately hosted page)
    AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT enable AI explanations (managed identity, no keys)

Mirrors how live sports data is distributed: provider feed -> stream processor -> pub/sub fan-out -> clients.
Late joiners get a state-of-play message; reconnecting clients resume from Last-Event-ID; slow clients are dropped.
ponytail: in-process pub/sub, one replica. Scale-out path: Azure Web PubSub (or Redis) between processor and clients.
"""
import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from explain import explain, facts, template
from feed import DEMO_SEEDS, compact_event, match_meta, snapshot
from match import TEAMS, simulate
from network import score_at, team_view
from stream import LiveEngine

SPEED = float(os.getenv("LIVE_SPEED", "10"))
HISTORY = 600  # messages kept for Last-Event-ID resume
LANGUAGES = {"English", "Spanish", "French", "Portuguese", "Hindi", "Arabic", "Japanese"}
AI_DAILY_CAP = int(os.getenv("AI_DAILY_CAP", "2000"))  # protects credits; beyond it, grounded templates only


class Channel:
    def __init__(self):
        self.seq, self.history, self.subscribers = 0, deque(maxlen=HISTORY), set()
        self.meta, self.state, self.events = None, None, []
        self.minutes, self.cues = {}, []  # one snapshot per match minute + all cues: history for late joiners
        self.seed, self.raw = None, []    # current match + full-fidelity events for on-demand explanations

    def publish(self, kind, data):
        self.seq += 1
        msg = (self.seq, kind, json.dumps({**data, "server_ts": time.time() * 1000}, separators=(",", ":")))
        self.history.append(msg)
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # back-pressure: a stalled client must not slow the feed for everyone
                self.subscribers.discard(q)

    async def run(self):
        """The match clock: replay synthetic matches in wall-clock time, forever."""
        i = 0
        while True:
            seed = DEMO_SEEDS[i % len(DEMO_SEEDS)]
            events, eng, t0 = [], LiveEngine(), time.monotonic()
            self.seed, self.raw = seed, events
            self.meta, self.state = match_meta(seed), None
            self.events = []
            self.minutes, self.cues = {}, []
            self.publish("kickoff", self.meta)
            for e in simulate(seed):
                wait = e["t"] / SPEED - (time.monotonic() - t0)
                if wait > 0:
                    await asyncio.sleep(wait)
                events.append(e)
                cues, ms = eng.ingest(e)
                ce = compact_event(e)
                self.events.append(ce)
                msg = {"event": ce, "cues": cues, "process_ms": round(ms, 1)}
                if (e["type"] == "pass" and e["success"]) or e["goal"] or self.state is None:
                    self.state = await asyncio.to_thread(snapshot, events, e["t"] / 60)
                    msg["snapshot"] = self.state
                    self.minutes.setdefault(int(e["t"] // 60), self.state)
                self.cues += cues
                self.publish("event", msg)
            self.publish("fulltime", {"score": self.state["score"] if self.state else None})
            await asyncio.sleep(15)
            i += 1


channel = Channel()


async def live(request):
    q = asyncio.Queue(maxsize=200)
    last = int(request.headers.get("last-event-id", 0) or 0)

    async def body():
        channel.subscribers.add(q)
        try:
            missed = [m for m in channel.history if m[0] > last] if last else []
            if missed and missed[0][0] == last + 1:  # resume exactly where the client left off
                for m in missed:
                    yield f"id: {m[0]}\nevent: {m[1]}\ndata: {m[2]}\n\n"
            else:  # fresh join: state of play
                hello = {"meta": channel.meta, "snapshot": channel.state, "events": channel.events,
                         "history": list(channel.minutes.values()), "cues": channel.cues,
                         "speed": SPEED, "server_ts": time.time() * 1000}
                yield f"id: {channel.seq}\nevent: hello\ndata: {json.dumps(hello, separators=(',', ':'))}\n\n"
            while True:
                try:
                    seq, kind, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {seq}\nevent: {kind}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # keeps proxies from closing an idle stream
        finally:
            channel.subscribers.discard(q)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@lru_cache(maxsize=8)
def _match(seed):
    return list(simulate(seed))


_ai_calls = {"day": None, "n": 0}


@lru_cache(maxsize=4096)
def _explain(seed, minute, team, mode, lang, fav, events_key):
    """events_key only splits the cache: a new live match or more events means a new answer."""
    events = [e for e in (channel.raw if seed == "live" else _match(seed)) if e["t"] < minute * 60]
    view, opp = team_view(events, team, minute), team_view(events, "away" if team == "home" else "home", minute)
    f = facts(view, opp, score_at(events, minute * 60), fav or None)
    day = time.strftime("%Y-%m-%d")
    if _ai_calls["day"] != day:
        _ai_calls.update(day=day, n=0)
    if _ai_calls["n"] >= AI_DAILY_CAP:
        return template(f, mode), "template (daily AI cap reached)"
    _ai_calls["n"] += 1
    return explain(f, mode, lang)


async def explain_route(request):
    """Grounded explanation for one team at one match minute. Public endpoint, so every input is validated."""
    q = request.query_params
    try:
        seed = "live" if q.get("seed", "live") == "live" else int(q["seed"])
        minute = int(q.get("minute", "0"))
    except ValueError:
        return JSONResponse({"error": "bad seed or minute"}, status_code=400)
    team, mode, lang, fav = q.get("team", "home"), q.get("mode", "Casual"), q.get("lang", "English"), q.get("fav", "")
    if ((seed != "live" and seed not in DEMO_SEEDS) or not 1 <= minute <= 100 or team not in TEAMS
            or mode not in ("Casual", "Analyst") or lang not in LANGUAGES or (fav and fav not in TEAMS[team]["players"])):
        return JSONResponse({"error": "invalid parameters"}, status_code=400)
    key = None
    if seed == "live":
        if not channel.raw or minute > channel.raw[-1]["t"] / 60 + 1:
            return JSONResponse({"error": "minute not played yet"}, status_code=409)
        key = (channel.seed, sum(e["t"] < minute * 60 for e in channel.raw))
    text, source = await asyncio.to_thread(_explain, seed, minute, team, mode, lang, fav, key)
    return JSONResponse({"text": text, "source": source, "minute": minute, "language": lang})


async def health(_):
    return JSONResponse({"ok": True, "subscribers": len(channel.subscribers), "seq": channel.seq,
                         "minute": channel.state["minute"] if channel.state else None, "speed": SPEED,
                         "ai": bool(os.getenv("AZURE_OPENAI_ENDPOINT"))})


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(channel.run())
    yield
    task.cancel()


app = Starlette(
    routes=[Route("/live", live), Route("/health", health), Route("/explain", explain_route),
            Mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))],
    middleware=[Middleware(CORSMiddleware, allow_origins=os.getenv("ALLOW_ORIGIN", "*").split(","), allow_methods=["GET"])],
    lifespan=lifespan)
