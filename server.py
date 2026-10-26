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
import threading
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

import agents
import ai
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
        sent = 0  # highest id delivered; anything at or below it is never sent twice
        try:
            oldest = channel.history[0][0] if channel.history else channel.seq + 1
            if last and oldest - 1 <= last <= channel.seq:  # resume exactly where the client left off (even if caught up)
                sent = last
                for m in list(channel.history):
                    if m[0] > sent:
                        sent = m[0]
                        yield f"id: {m[0]}\nevent: {m[1]}\ndata: {m[2]}\n\n"
            else:  # fresh join, or too far behind to resume: state of play
                hello = {"meta": channel.meta, "snapshot": channel.state, "events": channel.events,
                         "history": list(channel.minutes.values()), "cues": channel.cues,
                         "speed": SPEED, "server_ts": time.time() * 1000}
                sent = channel.seq
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
            channel.subscribers.discard(q)

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


def _events(seed, minute):
    return [e for e in (channel.raw if seed == "live" else _match(seed)) if e["t"] < minute * 60]


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
    seed = "live" if str(q.get("seed", "live")) == "live" else int(q["seed"])
    minute = int(q.get("minute", 0))
    if (seed != "live" and seed not in DEMO_SEEDS) or not 1 <= minute <= 100:
        raise ValueError("invalid seed or minute")
    if seed == "live" and (not channel.raw or minute > channel.raw[-1]["t"] / 60 + 1):
        raise LookupError("minute not played yet")
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
    if team not in TEAMS or mode not in ("Casual", "Analyst") or lang not in LANGUAGES or (fav and fav not in TEAMS[team]["players"]):
        return JSONResponse({"error": "invalid parameters"}, status_code=400)
    events_key = (channel.seed, sum(e["t"] < minute * 60 for e in channel.raw)) if seed == "live" else seed
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
    real_seed = channel.seed if seed == "live" else seed
    try:
        out = await agents.ask(question, _events(seed, minute), minute, real_seed, lang, history)
    except Exception as e:  # a model outage must not take the page down
        return JSONResponse({"error": f"agents unavailable ({type(e).__name__}); try again"}, status_code=502)
    return JSONResponse({**out, "minute": minute})


async def health(_):
    return JSONResponse({"ok": True, "subscribers": len(channel.subscribers), "seq": channel.seq,
                         "minute": channel.state["minute"] if channel.state else None, "speed": SPEED,
                         "ai": ai.enabled()})


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(channel.run())
    for seed in DEMO_SEEDS:  # warm the Monte Carlo rates so the first prediction question is instant
        asyncio.get_running_loop().run_in_executor(None, agents._goal_rates, seed)
    if ai.enabled():  # first model calls are slow (sign-in, cold deployments): take that hit before viewers do
        asyncio.get_running_loop().run_in_executor(None, agents.warm_up)
    yield
    task.cancel()


app = Starlette(
    routes=[Route("/live", live), Route("/health", health), Route("/explain", explain_route), Route("/ask", ask_route, methods=["POST"]),
            Mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))],
    middleware=[Middleware(CORSMiddleware, allow_origins=os.getenv("ALLOW_ORIGIN", "*").split(","), allow_methods=["GET", "POST"], allow_headers=["content-type"])],
    lifespan=lifespan)
