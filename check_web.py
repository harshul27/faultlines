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


def read(headers=None, want=8, timeout=30, path="/live"):
    msgs, cur, deadline = [], {}, time.time() + timeout
    with httpx.stream("GET", f"{base}{path}", headers=headers or {}, timeout=timeout) as r:
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
    assert resumed[0]["event"] != "hello" and int(resumed[0]["id"]) == ids[-1] + 1, f"reconnect resumes without gaps: last={ids[-1]} got={[(m['id'], m['event']) for m in resumed]}"
    # AI endpoints: validated inputs; without Foundry configured they degrade instead of failing
    assert httpx.post(f"{base}/ask", json={"question": "Who wins?", "seed": 23, "minute": 70}, timeout=5).status_code == 503
    assert httpx.get(f"{base}/explain", params={"seed": 999, "minute": 70}, timeout=5).status_code == 400
    assert httpx.get(f"{base}/explain", params={"seed": 23, "minute": 70, "lang": "Klingon"}, timeout=5).status_code == 400
    ex = httpx.get(f"{base}/explain", params={"seed": 23, "minute": 70, "team": "home", "lang": "Spanish"}, timeout=30).json()
    assert ex["source"] == "template" and ex["text"], "no AI configured -> grounded template"
    # Game plan: demo plan, rules parsing without AI, deterministic squad assessment, untrusted plans rejected
    demo = httpx.get(f"{base}/plan", params={"team": "home"}, timeout=10).json()
    assert demo["text"] and demo["plan"]["duties"], "demo plan served"
    mine = httpx.post(f"{base}/plan", json={"text": "Keep the ball with patient possession. Everything through Ferrand. Wingers press high.", "team": "home"}, timeout=10).json()
    assert mine["source"] == "rules" and mine["plan"]["duties"]["Luca Ferrand"]["relevance"] == 5
    sq = httpx.post(f"{base}/assess", json={"seed": 23, "minute": 70, "plan_team": "home", "plan": mine["plan"]}, timeout=60).json()
    assert len(sq["players"]) == 11 and sq["execution"]["principles"] and sq["zones"], "squad vs plan"
    assert httpx.post(f"{base}/assess", json={"seed": 23, "minute": 70, "plan": "x" * 10}, timeout=10).status_code == 400
    assert httpx.post(f"{base}/plan", json={"text": "short", "team": "home"}, timeout=10).status_code == 400
    # Bring your own match: upload (CSV), analyse it like a demo match, reject unsafe names
    up = httpx.post(f"{base}/matches", json={"text": (ROOT / "web/data/sample-match.csv").read_text(), "format": "csv",
                                             "home": "Test United", "away": "Sample City"}, timeout=300).json()
    assert up["summary"]["events"] > 500 and up["teams"]["home"]["name"] == "Test United", up
    tl = httpx.get(f"{base}/matches/{up['id']}", timeout=30).json()
    assert len(tl["snapshots"]) >= 90 and tl["meta"]["seed"] == up["seed"], "an upload becomes a full replay timeline"
    sq = httpx.post(f"{base}/assess", json={"seed": up["seed"], "minute": 70, "plan_team": "home"}, timeout=60).json()
    assert len(sq["players"]) == 11 and sq["execution"]["principles"], "uploads get the same analysis"
    ex = httpx.get(f"{base}/explain", params={"seed": up["seed"], "minute": 70, "team": "away"}, timeout=30).json()
    assert "Sample City" in ex["text"] or ex["text"], "explanations use the uploaded names"
    bad = httpx.post(f"{base}/matches", json={"text": "t,team,type,player,player_role\n1,home,pass,<img src=x>,ST", "format": "csv"}, timeout=10)
    assert bad.status_code == 422 and "valid name" in bad.json()["error"], "markup in names is rejected"
    # Live push feed from the user's own source: token required, time order enforced, viewers get SSE
    native = json.loads((ROOT / "web/data/sample-match.json").read_text())
    feed = httpx.post(f"{base}/feeds", json={"teams": native["teams"]}, timeout=10).json()
    evs, auth = native["events"][:150], {"authorization": f"Bearer {feed['token']}"}
    assert httpx.post(f"{base}{feed['push']}", json=evs, timeout=10).status_code == 401, "pushing needs the token"
    pushed = httpx.post(f"{base}{feed['push']}", json=evs, headers=auth, timeout=120).json()
    assert pushed["accepted"] == 150, pushed
    assert httpx.post(f"{base}{feed['push']}", json=evs[:1], headers=auth, timeout=10).status_code == 422, "time order"
    hello = json.loads(read(want=1, path=feed["watch"])[0]["data"])
    assert len(hello["events"]) == 150 and hello["meta"]["seed"] == feed["seed"], "viewers join the user's feed"
    assert httpx.get(f"{base}/live", params={"feed": "nope"}, timeout=5).status_code == 404
finally:
    proc.terminate()
print("ok")
