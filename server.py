"""Live feed server: one shared match clock, pushed to every viewer over Server-Sent Events.

    uvicorn server:app --port 8000          # then open http://127.0.0.1:8000
    LIVE_SPEED=10 (match seconds per wall second), ALLOW_ORIGIN=* (CORS for a separately hosted page)

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
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from feed import DEMO_SEEDS, compact_event, match_meta, snapshot
from match import simulate
from stream import LiveEngine

SPEED = float(os.getenv("LIVE_SPEED", "10"))
HISTORY = 600  # messages kept for Last-Event-ID resume


class Channel:
    def __init__(self):
        self.seq, self.history, self.subscribers = 0, deque(maxlen=HISTORY), set()
        self.meta, self.state, self.events = None, None, []
        self.minutes, self.cues = {}, []  # one snapshot per match minute + all cues: history for late joiners

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


async def health(_):
    return JSONResponse({"ok": True, "subscribers": len(channel.subscribers), "seq": channel.seq,
                         "minute": channel.state["minute"] if channel.state else None, "speed": SPEED})


@asynccontextmanager
async def lifespan(_):
    task = asyncio.create_task(channel.run())
    yield
    task.cancel()


app = Starlette(
    routes=[Route("/live", live), Route("/health", health),
            Mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True))],
    middleware=[Middleware(CORSMiddleware, allow_origins=os.getenv("ALLOW_ORIGIN", "*").split(","), allow_methods=["GET"])],
    lifespan=lifespan)
