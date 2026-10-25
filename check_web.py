"""Web checks: static replay data is consistent, and the live server speaks SSE correctly. Run: python check_web.py"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jsonschema

from stream import CUE_SCHEMA

ROOT = Path(__file__).parent

# Static data the page reads: every match listed, a snapshot per minute, cues valid and in order
index = json.loads((ROOT / "web/data/index.json").read_text())
assert len(index) >= 3, "page needs several demo matches"
for m in index:
    tl = json.loads((ROOT / "web" / m["file"]).read_text())
    assert [s["minute"] for s in tl["snapshots"]] == list(range(1, len(tl["snapshots"]) + 1)), "one snapshot per minute"
    assert tl["events"] == sorted(tl["events"], key=lambda e: e["t"]), "events in clock order"
    for c in tl["cues"]:
        jsonschema.validate(c, CUE_SCHEMA)
    s = tl["snapshots"][-1]
    assert set(s["teams"]) == {"home", "away"} and all(len(s["teams"][t]["players"]) == 11 for t in s["teams"])
    assert all(0 <= p["x"] <= 100 and 0 <= p["y"] <= 100 for t in s["teams"] for p in s["teams"][t]["players"])
    goals = sum(e["goal"] for e in tl["events"])
    assert goals == s["score"]["home"] + s["score"]["away"], "final snapshot score must match goal events"

# Live server: start it with a fast clock, join, read the state of play and live events, then resume by Last-Event-ID
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
env = {**os.environ, "LIVE_SPEED": "300"}
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--port", str(port)], cwd=ROOT, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = f"http://127.0.0.1:{port}"


def read(headers=None, want=8, timeout=30):
    msgs, cur, deadline = [], {}, time.time() + timeout
    with httpx.stream("GET", f"{base}/live", headers=headers or {}, timeout=timeout) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith(("id:", "event:", "data:")):
                k, v = line.split(":", 1)
                cur[k] = v.strip()
            elif not line and cur:
                msgs.append(cur)
                cur = {}
                if len(msgs) >= want or time.time() > deadline:
                    break
    return msgs


try:
    for _ in range(60):
        try:
            if httpx.get(f"{base}/health", timeout=1).json()["ok"]:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    page = httpx.get(f"{base}/", timeout=5)
    assert page.status_code == 200 and "Fault Lines" in page.text, "server also serves the page"
    time.sleep(1)  # let the clock run so a late joiner has history
    msgs = read()
    assert msgs[0]["event"] == "hello", "a joining client first gets the state of play"
    hello = json.loads(msgs[0]["data"])
    assert hello["meta"] and isinstance(hello["events"], list) and isinstance(hello["history"], list)
    live = [json.loads(m["data"]) for m in msgs[1:] if m["event"] == "event"]
    assert live and all("event" in d and "server_ts" in d for d in live), "live events stream after hello"
    ids = [int(m["id"]) for m in msgs[1:]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids), "event ids strictly increase"
    for d in live:
        for c in d["cues"]:
            jsonschema.validate(c, CUE_SCHEMA)
    assert any("snapshot" in d for d in live), "completed passes push a fresh snapshot"
    resumed = read({"Last-Event-ID": str(ids[-1])}, want=3)
    assert resumed[0]["event"] != "hello" and int(resumed[0]["id"]) == ids[-1] + 1, "reconnect resumes without gaps"
finally:
    proc.terminate()
print("ok")
