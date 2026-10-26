"""Ask the analyst: a small team of agents on Microsoft Foundry, orchestrated with Microsoft Agent Framework.

    Router (Phi-4-mini)      micro-decision gate: question -> JSON intent + confidence; off-topic/unclear stops here
    Memory (embeddings)      keeps only earlier turns whose embedding is close to the new question (salience)
    Analyst (gpt-4.1-mini)   Agent Framework agent with match and game-plan tools (gameplan.py: every player rated on
                             the metrics their role and the coach's plan weight); every number must come from a tool
    Verifier (deterministic) checks numbers and trend directions against the tool outputs; one self-correcting
                             rewrite, then a deterministic fallback built from the tool outputs
    Translator (gpt-4.1-mini) translates the verified English answer; numbers must survive unchanged

Every step is recorded in a trace the page shows, so viewers can see which agent did what and why.
"""
import asyncio
import contextvars
import functools
import json
import re
import time
from functools import lru_cache

import numpy as np

import ai
import gameplan
import grounding
from match import TEAMS, params, simulate, squad, strengths
from network import fragility, pass_graph, score_at, team_view

CTX = contextvars.ContextVar("ctx")  # per-request match context + trace
INTENTS = ("match_summary", "player", "assessment", "tactics", "substitution", "what_if", "prediction", "trend", "off_topic")
pct = lambda v: f"{round(v * 100)}%"


def _trend(new, old, unit=""):
    if not old:
        return "no earlier window to compare"
    ch = (new - old) / old
    return f"{'up' if ch > 0 else 'down' if ch < 0 else 'flat'} {abs(round(ch * 100))}% ({old}{unit} -> {new}{unit})"


# ---------- tools (pure functions of the match so far; registered with the Analyst agent) ----------
def _find_player(name):
    name = (name or "").strip().lower()
    for t in TEAMS:
        for p in TEAMS[t]["players"]:
            if name and (name == p.lower() or name == p.split()[-1].lower() or name in p.lower()):
                return t, p
    return None, None


def _record(tool, args, out, t0):
    CTX.get()["trace"].append({"agent": "Analyst → tool", "tool": tool, "args": args, "ms": round((time.perf_counter() - t0) * 1000)})
    CTX.get()["outputs"].append(out)
    return out


def match_state() -> dict:
    """Current score, minute, goals, and for each team: the fault-line player, passing spread trend,
    forward progression and pass completion over the last 15 minutes."""
    t0, c = time.perf_counter(), CTX.get()
    ev, m = c["events"], c["minute"]
    out = {"minute": m, "score": f"{score_at(ev, m * 60)['home']}-{score_at(ev, m * 60)['away']} (home-away)",
           "goals": [f"{int(e['t'] // 60)}' {e['player']} ({TEAMS[e['team']]['name']})" for e in ev if e["goal"]]}
    for t in TEAMS:
        v = team_view(ev, t, m)
        out[TEAMS[t]["name"]] = {
            "side": t, "fault_line": f"{v['spof']['name']} ({v['spof']['role']}) holds {pct(v['spof']['fragility'])} of the passing network",
            "passing_spread": _trend(round(v["efficiency"], 1), round(v["efficiency_prev"], 1)),
            "progressive_passes_last_15": _trend(v["progressive"], v["progressive_prev"]),
            "pass_completion_last_15": pct(v["completion"])}
    return _record("match_state", {}, out, t0)


def player_profile(player: str) -> dict:
    """One player's role in the passing network: share of the network now and at its peak, passes and
    progressive passes in the last 15 minutes, top passing partners, and expected points added (xPA: chances
    created or taken, weighted by what a goal was worth at that moment)."""
    t0, c = time.perf_counter(), CTX.get()
    team, name = _find_player(player)
    if not name:
        return _record("player_profile", {"player": player}, {"error": f"No player called {player}"}, t0)
    ev, m = c["events"], c["minute"]
    v = team_view(ev, team, m)
    me = next(p for p in v["players"] if p["name"] == name)
    history = [(mm, next(p["fragility"] for p in team_view(ev, team, mm)["players"] if p["name"] == name))
               for mm in range(15, int(m) + 1, 5)]
    peak = max(history, key=lambda h: h[1]) if history else (m, me["fragility"])
    mine = [e for e in ev if e["type"] == "pass" and e["player"] == name and (m - 15) * 60 <= e["t"] < m * 60]
    partners = sorted(((b if a == name else a, w) for a, b, w in v["edges"] if name in (a, b)), key=lambda x: -x[1])[:3]
    out = {"player": name, "team": TEAMS[team]["name"], "role": me["role"],
           "network_share_now": pct(me["fragility"]), "network_share_previous_15": pct(me["fragility_prev"]),
           "peak_network_share": f"{pct(peak[1])} at {peak[0]}'",
           "passes_involved_last_15": me["passes"],
           "own_passes_last_15": len(mine), "own_completion_last_15": pct(sum(e["success"] for e in mine) / len(mine)) if mine else "n/a",
           "progressive_passes_last_15": sum(e["success"] and e["end_x"] - e["x"] > 10 for e in mine),
           "top_partners": [f"{p} ({w} passes)" for p, w in partners], "expected_points_added": f"{me['xpa']:+.2f}"}
    return _record("player_profile", {"player": player}, out, t0)


def what_if_remove(player: str) -> dict:
    """Static counterfactual: remove one player from the current passing network: share of teammates' connectivity
    lost if play kept its current pattern, passes that ran through them, and who becomes the new fault line.
    It measures dependence now, not certain damage: teams can re-route play (see the assumption field)."""
    t0, c = time.perf_counter(), CTX.get()
    team, name = _find_player(player)
    if not name:
        return _record("what_if_remove", {"player": player}, {"error": f"No player called {player}"}, t0)
    m = c["minute"]
    G = pass_graph(c["events"], team, max(0, m - 15) * 60, m * 60)
    total = G.size(weight="weight")
    through = sum(d["weight"] for _, _, d in G.edges(name, data=True))
    others = [p for p in G if p != name]
    after = fragility(G.subgraph(others))
    new_fl = max(after, key=after.get)
    out = {"player": name, "team": TEAMS[team]["name"],
           "teammates_connectivity_lost": pct(fragility(G)[name]),
           "passes_through_player_last_15": f"{through} of {int(total)} ({pct(through / total) if total else '0%'})",
           "new_fault_line_without_them": f"{new_fl} would hold {pct(after[new_fl])} of the network",
           "assumption": "static: the team keeps its current passing pattern. In evaluation the simulated teams re-routed "
                         "play after losing a player, so treat this as how much play depends on them now, not as certain damage",
           "most_affected_partners": [f"{b if a == name else a} ({d['weight']} passes with {name.split()[-1]})"
                                      for a, b, d in sorted(G.edges(name, data=True), key=lambda e: -e[2]["weight"])[:3]]}
    return _record("what_if_remove", {"player": player}, out, t0)


@lru_cache(maxsize=8)
def _goal_rates(seed):
    """Each club's goal rate per 95 minutes, estimated by replaying these two clubs 120 times (~2 s, cached;
    the server warms it at startup)."""
    p = params()
    st = strengths(seed, p)
    goals = np.zeros(2)
    for k in range(120):
        for e in simulate(10_000 + k, p=p, st=st):
            if e["goal"]:
                goals[e["team"] == "away"] += 1
    return goals / 120


def win_probability() -> dict:
    """Monte Carlo forecast from the current score and minute: 10,000 simulated finishes using each club's
    goal rate estimated by re-simulating this fixture. Returns home win / draw / away win and likely scores."""
    t0, c = time.perf_counter(), CTX.get()
    m, sc = c["minute"], score_at(c["events"], c["minute"] * 60)
    rate = _goal_rates(c["seed"]) * max(0.0, 95 - m) / 95
    rng = np.random.default_rng(c["seed"] * 1000 + int(m))
    h = sc["home"] + rng.poisson(rate[0], 10_000)
    a = sc["away"] + rng.poisson(rate[1], 10_000)
    finals = {}
    for x, y in zip(h, a):
        finals[(x, y)] = finals.get((x, y), 0) + 1
    top = sorted(finals.items(), key=lambda kv: -kv[1])[:3]
    hn, an = TEAMS["home"]["name"], TEAMS["away"]["name"]
    out = {"minute": m, "current_score": f"{sc['home']}-{sc['away']}", "simulations": 10_000,
           f"{hn}_win": pct((h > a).mean()), "draw": pct((h == a).mean()), f"{an}_win": pct((h < a).mean()),
           "most_likely_final_scores": [f"{x}-{y} ({pct(n / 10_000)})" for (x, y), n in top]}
    return _record("win_probability", {}, out, t0)


def compare_windows(team: str, earlier_minute: int, later_minute: int) -> dict:
    """Compare one team between two match minutes (each a rolling 15-minute window): fault line,
    passing spread, progressive passes and pass completion, with the direction of each change."""
    t0, c = time.perf_counter(), CTX.get()
    side = _side(team)
    a, b = (max(15, min(int(earlier_minute), c["minute"])), max(15, min(int(later_minute), c["minute"])))
    va, vb = team_view(c["events"], side, a), team_view(c["events"], side, b)
    out = {"team": TEAMS[side]["name"], "earlier": f"{a}'", "later": f"{b}'",
           "fault_line": f"{va['spof']['name']} {pct(va['spof']['fragility'])} -> {vb['spof']['name']} {pct(vb['spof']['fragility'])}",
           "passing_spread": _trend(round(vb["efficiency"], 1), round(va["efficiency"], 1)),
           "progressive_passes": _trend(vb["progressive"], va["progressive"]),
           "pass_completion": f"{pct(va['completion'])} -> {pct(vb['completion'])}"}
    return _record("compare_windows", {"team": team, "earlier_minute": earlier_minute, "later_minute": later_minute}, out, t0)


def _side(team):
    """'home'/'away' from a side, club name or nickname; defaults to the side whose game plan the coach gave."""
    t = (team or "").lower()
    for s in TEAMS:
        if t and (t == s or t in TEAMS[s]["name"].lower() or TEAMS[s]["name"].split()[0].lower() in t):
            return s
    return CTX.get()["plan_team"]


def game_plan(team: str) -> dict:
    """The coach's game plan for a team as the analysis reads it: team principles (and the metrics that measure
    each), and each player's duty and relevance to the plan (1-5)."""
    t0, c = time.perf_counter(), CTX.get()
    side = _side(team)
    plan = c["plans"][side]
    out = {"team": TEAMS[side]["name"], "source": "coach's plan" if side == c["plan_team"] and c["custom_plan"] else "default demo plan",
           "summary": plan["summary"],
           "principles": [{"instruction": p["text"], "measured_by": [gameplan.METRICS[k] for k in p["metrics"]]} for p in plan["principles"]],
           "duties": {p: {"duty": d["duty"], "relevance": f"{d['relevance']}/5"} for p, d in plan["duties"].items()}}
    return _record("game_plan", {"team": team}, out, t0)


def assess_player(player: str) -> dict:
    """How one player is doing in their role against the game plan: plan fit 0-100 (50 = role norm) for the match
    and the last 15 minutes, form, load vs what the plan intends, flags (overloaded, bypassed, off plan, fading),
    strengths and concerns with the evidence, and the metrics their role and duty are weighted on."""
    t0, c = time.perf_counter(), CTX.get()
    team, name = _find_player(player)
    if not name:
        return _record("assess_player", {"player": player}, {"error": f"No player called {player}"}, t0)
    row = next(r for r in gameplan.assess(c["events"], team, c["minute"], c["plans"][team]) if r["player"] == name)
    return _record("assess_player", {"player": player}, {"team": TEAMS[team]["name"], **row}, t0)


def squad_assessment(team: str) -> dict:
    """Every player of a team against the game plan, best plan fit first, with who is overloaded, off plan or
    fading, who is bypassed (a key role not getting the ball), and substitution candidates (who to take off: lowest
    recent plan fit). There is no bench data."""
    t0, c = time.perf_counter(), CTX.get()
    side = _side(team)
    rows = gameplan.assess(c["events"], side, c["minute"], c["plans"][side])
    out_field = [r for r in rows if r["role"] != "GK"]
    subs = sorted((r for r in out_field if r["plan_fit_last_15"] < 50),
                  key=lambda r: r["plan_fit_last_15"] - (10 if r["form"] == "fading" else 0))[:3]
    out = {"team": TEAMS[side]["name"], "minute": c["minute"],
           "players": [{k: r[k] for k in ("player", "role", "plan_relevance", "plan_fit", "plan_fit_last_15", "form", "load_vs_plan", "flags")} for r in rows],
           "overloaded": [r["player"] for r in rows if any(f.startswith("overloaded") for f in r["flags"])],
           "off_plan": [r["player"] for r in rows if "off plan" in r["flags"]],
           "substitution_candidates": [{"player": r["player"], "plan_relevance": r["plan_relevance"],
                                        "plan_fit_last_15": r["plan_fit_last_15"], "form": r["form"],
                                        "concerns": r["concerns"], "network_share_last_15": r["network_share_last_15"]} for r in subs],
           "note": "plan fit 50 = role norm; no bench data, so candidates are who to take off, not who to bring on"}
    return _record("squad_assessment", {"team": team}, out, t0)


def plan_execution(team: str) -> dict:
    """Is the game plan working? For each team principle: a score 0-100 (50 = league norm), working / mixed /
    not working, what measures it, and who executes it best and least; plus overall plan execution."""
    t0, c = time.perf_counter(), CTX.get()
    side = _side(team)
    out = {"team": TEAMS[side]["name"], **gameplan.execution(c["events"], side, c["minute"], c["plans"][side])}
    return _record("plan_execution", {"team": team}, out, t0)


def zone_report(team: str, last_minutes: int = 0) -> dict:
    """Where a team gets into the final third and shoots (by channel: left / centre / right of its attack), and
    where it loses and wins the ball. last_minutes=0 means the whole match so far. Use it for 'where should we
    attack', 'where are we losing the ball' and the opponent's weak side."""
    t0, c = time.perf_counter(), CTX.get()
    out = gameplan.zones(c["events"], _side(team), c["minute"], int(last_minutes) or None)
    return _record("zone_report", {"team": team, "last_minutes": last_minutes}, out, t0)


TOOLS = [match_state, player_profile, what_if_remove, win_probability, compare_windows,
         game_plan, assess_player, squad_assessment, plan_execution, zone_report]


# ---------- router: Phi-4-mini micro-decision gate, hedged ----------
ROUTER_PROMPT = """Classify a viewer's question about ONE football match (Northbridge FC = home, Riverside Athletic = away). Output one JSON object and nothing else.
intent: player (facts about one player), assessment (how a player or the squad is performing in their role / against the game plan, who is overloaded or struggling), tactics (is the game plan or press working, where to attack, where we lose the ball, tactical changes), substitution (who to take off or change), what_if (removing, marking or injuring a specific player), prediction (who wins, final score), trend (change between times), match_summary (general state), off_topic (anything not about this match or football coaching).

Question: Who is Northbridge's most important passer?
JSON: {"intent": "match_summary", "team": "home", "players": [], "confidence": 0.8}
Question: How is Brightwell doing in his role?
JSON: {"intent": "assessment", "team": "home", "players": ["Owen Brightwell"], "confidence": 0.9}
Question: Is our press working and which flank should we attack?
JSON: {"intent": "tactics", "team": null, "players": [], "confidence": 0.9}
Question: Who should I take off?
JSON: {"intent": "substitution", "team": null, "players": [], "confidence": 0.9}
Question: What if Ferrand gets injured?
JSON: {"intent": "what_if", "team": "home", "players": ["Luca Ferrand"], "confidence": 0.95}
Question: Will Riverside come back?
JSON: {"intent": "prediction", "team": "away", "players": [], "confidence": 0.9}
Question: Recommend a pizza place
JSON: {"intent": "off_topic", "team": null, "players": [], "confidence": 0.95}

QUESTION_HERE
JSON:"""
HEDGE_AFTER, ROUTE_BUDGET = 2.5, 8.0  # seconds: ask the backup model after 2.5 s; give up on models after 8 s
_pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=8)


def _classify(model, prompt):
    r = ai.client().with_options(timeout=ROUTE_BUDGET, max_retries=0).chat.completions.create(
        model=ai.deployment(model), temperature=0, max_tokens=60, messages=[{"role": "user", "content": prompt}])
    d = json.loads(re.search(r"\{.*?\}", r.choices[0].message.content, re.S).group(0))  # small models add prose
    return {"intent": d.get("intent") if d.get("intent") in INTENTS else "match_summary",
            "team": d.get("team") if d.get("team") in ("home", "away") else None,
            "players": [p for p in (_find_player(x)[1] for x in d.get("players") or []) if p],
            "confidence": float(d.get("confidence", 0.5))}


def _rules(question):
    """Last-resort router: keywords. Always answers, never smart."""
    q = question.lower()
    players = [p for t in TEAMS for p in TEAMS[t]["players"] if p.split()[-1].lower() in q]
    intent = ("what_if" if re.search(r"what if|without|removed|marked|injur", q) else
              "substitution" if re.search(r"\bsub|take (\w+ )?off|bring (\w+ )?off|replac|tired|fatigu|fresh legs", q) else
              "prediction" if re.search(r"\bwin\b|winning|final score|come back|predict", q) else
              "tactics" if re.search(r"plan|press|tactic|shape|formation|where should|weak|exploit|build[- ]?up|counter|zone|flank|channel|side", q) else
              "assessment" if re.search(r"perform|doing|rating|struggl|overload|carry|load|job|role|form\b|best|worst|playing", q) else
              "trend" if re.search(r"change|since|between|after|before", q) else
              "player" if players else "match_summary")
    return {"intent": intent, "team": None, "players": players, "confidence": 0.6}


_routes = {}  # (question, memory) -> (route, who). Same question, same routing: which model wins the hedge race
# depends on latency, and the two can label a question differently. ponytail: per-process dict, capped.


def _route(question, memory):
    """Hedged request: Phi-4-mini first; if it is slow, gpt-4.1-mini races it; first valid answer wins (and is cached)."""
    from concurrent.futures import FIRST_COMPLETED, wait
    t0 = time.perf_counter()
    key = (" ".join(question.lower().split()), memory)
    if key in _routes:
        route, who = _routes[key]
        CTX.get()["trace"].append({"agent": f"Router (cached: {who})", "detail": f"{route}", "ms": 0})
        return dict(route), f"cached: {who}"
    prompt = ROUTER_PROMPT.replace("QUESTION_HERE", (f"Earlier: {memory}\n" if memory else "") + f"Question: {question}")
    pending = {_pool.submit(_classify, "router", prompt): "Phi-4-mini"}
    route, who, hedged = None, None, False
    while pending and time.perf_counter() - t0 < ROUTE_BUDGET:
        wait_for = HEDGE_AFTER if not hedged else ROUTE_BUDGET - (time.perf_counter() - t0)
        done, _ = wait(pending, timeout=max(0.05, wait_for), return_when=FIRST_COMPLETED)
        for f in done:
            name = pending.pop(f)
            if route is None and not f.exception():
                route, who = f.result(), name
        if route:
            break
        if not hedged:  # slow or failed: hedge with the bigger model
            pending[_pool.submit(_classify, "chat", prompt)] = "gpt-4.1-mini (hedge)"
            hedged = True
    if route is None:
        route, who = _rules(question), "keyword rules (models too slow)"
    elif len(_routes) < 5000:
        _routes[key] = (dict(route), who)
    CTX.get()["trace"].append({"agent": f"Router ({who})", "detail": f"{route}" + (" · hedged after 2.5 s" if hedged else ""),
                               "ms": round((time.perf_counter() - t0) * 1000)})
    return route, who


# ---------- memory: salience by embedding similarity ----------
SALIENCE = 0.45


def _memory(question, history):
    if not history:
        return []
    t0 = time.perf_counter()
    texts = [question] + [f"Q: {h['q']} A: {h['a']}" for h in history]
    vecs = np.array([d.embedding for d in ai.client().embeddings.create(model=ai.deployment("embed"), input=texts).data])
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    sims = vecs[1:] @ vecs[0]
    keep = [texts[i + 1] for i in np.argsort(-sims)[:2] if sims[i] >= SALIENCE]
    CTX.get()["trace"].append({"agent": "Memory (embeddings)", "detail": f"kept {len(keep)} of {len(history)} earlier turns "
                               f"(salience ≥ {SALIENCE}; scores {', '.join(f'{s:.2f}' for s in sims)})",
                               "ms": round((time.perf_counter() - t0) * 1000)})
    return keep


# ---------- analyst: Microsoft Agent Framework agent with tools ----------
ANALYST_PROMPT = (
    "You are the assistant coach and analyst on the bench, answering the head coach (or a viewer) during a live match. "
    "Answer in English in 2-5 sentences; when the question asks what to do, end with one concrete recommendation. "
    "Call the tools to get data: NEVER state a number that a tool did not return. Copy numbers, percentages and "
    "trend directions exactly as the tools word them (e.g. 'down 21%', 'plan fit 44'). Name players in full the first time. "
    "Tools: how a player is doing in their role or against the plan -> assess_player (plan fit 50 = role norm); "
    "the whole squad, who is overloaded, struggling or off plan, who to take off -> squad_assessment; "
    "is the game plan / press / build-up working -> plan_execution; what the plan asks of someone -> game_plan; "
    "where to attack, where we lose or win the ball, the opponent's weak side -> zone_report (for both teams if needed); "
    "facts about a player's passing network -> player_profile; removing or marking a player -> what_if_remove "
    "(always call it before any advice on substituting a specific player, whichever way you advise); "
    "who wins -> win_probability; "
    "changes over time -> compare_windows. Combine tools when the question needs it. Never ask the coach for player "
    "names or positions: the line-ups are in the message; look players up by position there.")


@lru_cache(maxsize=1)
def _analyst():
    from agent_framework import Agent, tool
    from agent_framework.openai import OpenAIChatCompletionClient
    client = OpenAIChatCompletionClient(model=ai.deployment("chat"), credential=ai.credential(),
                                        azure_endpoint=ai.endpoint(), api_version=ai.API_VERSION)
    return Agent(client, ANALYST_PROMPT, name="analyst", tools=[tool(_off_loop(f)) for f in TOOLS],
                 default_options={"temperature": 0, "seed": 7})  # same question, same advice (best effort)


def _off_loop(f):
    """Run a tool in a worker thread so analytics never stall the live SSE feed (context, incl. CTX, is copied)."""
    @functools.wraps(f)
    async def run(*args, **kwargs):
        return await asyncio.to_thread(f, *args, **kwargs)
    return run


async def _analyse(prompt, session=None):
    t0 = time.perf_counter()
    resp = await _analyst().run(prompt, session=session)
    CTX.get()["trace"].append({"agent": "Analyst (gpt-4.1-mini, Agent Framework)", "detail": "drafted answer",
                               "ms": round((time.perf_counter() - t0) * 1000)})
    return resp.text.strip()


FACES = {"RW": ["LB"], "LW": ["RB"], "RB": ["LW"], "LB": ["RW"], "RCM": ["LCM"], "LCM": ["RCM"], "CDM": ["CDM"],
         "ST": ["RCB", "LCB"]}  # teams face each other, so a right-sided player meets the opponent's left side


def matchups(plan_team):
    """Direct opponents as facts, so the analyst never has to mirror flanks itself."""
    other = "away" if plan_team == "home" else "home"
    ours = {r: p for p, r in squad(plan_team).items()}
    return "Direct opponents (their right side meets our left side): " + "; ".join(
        f"their {p} ({r}) vs our {' and '.join(f'{ours[x]} ({x})' for x in FACES[r])}" for p, r in squad(other).items() if r in FACES)


SUB_VERB = re.compile(r"\b(take|taking|bring|bringing|sub|subbing|substitut\w*|replac\w*|withdraw\w*|hook\w*)\b", re.I)


def advice_issues(answer):
    """Coaching guard: recommending a key player (plan relevance 4-5) off needs the counterfactual first, and the
    keeper is never a substitution candidate (there is no bench data). Checked like the numbers: deterministically."""
    c, issues = CTX.get(), []
    checked = {_find_player(s["args"].get("player"))[1] for s in c["trace"] if s.get("tool") == "what_if_remove"}
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        if not SUB_VERB.search(sentence):
            continue
        for team in TEAMS:
            for name, role in squad(team).items():
                if not re.search(rf"\b{re.escape(name.split()[-1])}\b", sentence):
                    continue
                if role == "GK":
                    issues.append(f"do not suggest substituting the goalkeeper {name}: there is no bench data")
                elif c["plans"][team]["duties"].get(name, {}).get("relevance", 3) >= 4 and name not in checked:
                    issues.append(f"{name} is a key player in the plan; call what_if_remove for {name} and say what "
                                  f"the team would lose before recommending a change")
    return list(dict.fromkeys(issues))


def _by(agent, n):
    """Relabel the last n trace entries: tool calls made by the orchestrator or verifier, not the analyst."""
    for s in CTX.get()["trace"][-n:]:
        s["agent"] = f"{agent} → tool"


SUB_MAX_COST = 10  # % of teammates' connectivity a substitution may cost before it is not recommended


def _gather_substitution(plan_team, named):
    """Squad assessment plus the what-if for every substitution candidate and every outfield player named."""
    sq = squad_assessment(TEAMS[plan_team]["name"])
    names = [c["player"] for c in sq["substitution_candidates"]]
    names += [p for p in named if p not in names and squad(_find_player(p)[0] or plan_team).get(p) != "GK"]
    out = [sq] + [what_if_remove(p) for p in names]
    _by("Orchestrator", len(out))
    # The decision is a rule the coach can audit, not the model's tie-break between candidates
    cost = {w["player"]: int(w["teammates_connectivity_lost"].rstrip("%")) for w in out[1:] if "error" not in w}
    pick = next((c["player"] for c in sq["substitution_candidates"] if cost.get(c["player"], 100) < SUB_MAX_COST), None)
    decision = {"recommended_substitution": pick or f"none: every candidate's removal costs {SUB_MAX_COST}%+ of teammates' connectivity",
                "rule": f"lowest recent plan fit (fading counts 10 points against) among candidates whose removal costs under "
                        f"{SUB_MAX_COST}% of teammates' connectivity"}
    CTX.get()["outputs"].append(decision)
    return [decision] + out


def _run_skipped_whatifs(issues):
    names = [m.group(1) for i in issues if (m := re.match(r"(.+) is a key player in the plan; call what_if_remove", i))]
    out = [what_if_remove(p) for p in names]
    _by("Verifier", len(out))
    return out


def _fallback():
    """Deterministic answer straight from the tool outputs: always true, never fluent."""
    outs = [o for o in CTX.get()["outputs"] if "error" not in o] or [match_state()]
    o = outs[-1]
    return "; ".join(f"{k.replace('_', ' ')}: {v if not isinstance(v, (list, dict)) else json.dumps(v, ensure_ascii=False)}"
                     for k, v in o.items())


# ---------- orchestration ----------
async def ask(question, events, minute, seed, language="English", history=(), plan=None, plan_team="home"):
    """Returns {answer, language, source, route, trace, model_calls}. plan: the coach's normalised game plan for
    plan_team (the other side, and plan_team without one, use the default demo plans)."""
    plans = {t: gameplan.default_plan(t) for t in TEAMS}
    if plan:
        plans[plan_team] = plan
    ctx = {"events": events, "minute": minute, "seed": seed, "trace": [], "outputs": [],
           "plans": plans, "plan_team": plan_team, "custom_plan": bool(plan)}
    CTX.set(ctx)
    calls = 0
    memory = await asyncio.to_thread(_memory, question, list(history)[-6:]) if history else []
    calls += bool(history)
    route, who = await asyncio.to_thread(_route, question, " | ".join(memory))
    calls += 0 if who.startswith("cached") else 1 + ("hedge" in who)
    if route["intent"] == "off_topic" or route["confidence"] < 0.35:
        ctx["trace"].append({"agent": "Gate", "detail": f"intent={route['intent']}, confidence={route['confidence']}: no analyst call", "ms": 0})
        msg = ("I can only answer questions about this match and your game plan: try 'Who should I take off?', "
               "'Is our game plan working?' or 'What happens if Ferrand is marked out?'")
        out, _ = await asyncio.to_thread(grounding.translate, msg, language)
        return dict(answer=out, language=language, source="router (gated)", route=route, trace=ctx["trace"], model_calls=calls + (language != "English"))

    from agent_framework import AgentSession
    session = AgentSession()
    lineups = "; ".join(f"{TEAMS[t]['name']}: " + ", ".join(f"{p} ({r})" for p, r in squad(t).items()) for t in TEAMS)
    lineups += ". " + matchups(plan_team)
    prompt = (f"Match minute: {minute}. The coach's team: {TEAMS[plan_team]['name']} ('our', 'we' and 'us' mean them). "
              f"Line-ups (role codes: GK keeper, RB/LB full-backs, RCB/LCB centre-backs, CDM holding midfielder, "
              f"RCM/LCM central midfielders, RW/LW wingers, ST striker): {lineups}.\n"
              f"Router: intent={route['intent']}, team={route['team']}, players={route['players']}.\n"
              + (f"Relevant earlier conversation: {' | '.join(memory)}\n" if memory else "") + f"Question: {question}")
    if route["intent"] == "substitution" or SUB_VERB.search(question):
        # Substitution advice must rest on the counterfactual: gather it deterministically instead of hoping the model asks
        gathered = await asyncio.to_thread(_gather_substitution, plan_team, route["players"])
        prompt += (f"\nData already gathered for this substitution question (use it; call tools for anything else): {json.dumps(gathered)}"
                   "\nThe first item is the rule-based recommendation: lead with recommended_substitution and its reasons from "
                   "the data, then any caveats. Do not pick a different player; if the coach asked about someone else, "
                   "answer that and mention the recommended option.")
    answer = await _analyse(prompt, session)
    calls += 1
    issues = grounding.check(answer, *ctx["outputs"], question, str(minute)) + advice_issues(answer)
    if not ctx["outputs"]:
        issues.append("no tool was called, so nothing is grounded in data")
    ctx["trace"].append({"agent": "Verifier", "detail": "passed" if not issues else "; ".join(issues), "ms": 0})
    source = "ai"
    if issues:  # self-reflection: one rewrite with the verifier's critique
        repaired = await asyncio.to_thread(_run_skipped_whatifs, issues)  # deterministic repair, not a request to remember
        answer = await _analyse("The verifier rejected your answer: " + "; ".join(issues) + ". Rewrite it using only "
                                "numbers and directions exactly as the tools returned them (call tools if needed)."
                                + (f" Results of the what_if_remove checks you skipped: {json.dumps(repaired)}" if repaired else ""), session)
        calls += 1
        issues = grounding.check(answer, *ctx["outputs"], question, str(minute)) + advice_issues(answer)
        if not ctx["outputs"]:
            issues.append("no tool was called")
        ctx["trace"].append({"agent": "Verifier", "detail": "passed after rewrite" if not issues else
                             "failed again -> deterministic answer: " + "; ".join(issues), "ms": 0})
        if issues:
            answer, source = _fallback(), "deterministic (verifier fallback)"
        else:
            source = "ai (self-corrected)"
    if language != "English":
        t0 = time.perf_counter()
        answer, ok = await asyncio.to_thread(grounding.translate, answer, language)
        calls += 1
        ctx["trace"].append({"agent": "Translator (gpt-4.1-mini)", "detail": f"{language}: " + ("numbers preserved" if ok else "numbers changed -> kept English"),
                             "ms": round((time.perf_counter() - t0) * 1000)})
    return dict(answer=answer, language=language, source=source, route=route, trace=ctx["trace"], model_calls=calls)


def warm_up():
    """Sign in and wake the router and analyst deployments; failures are harmless (the hedge covers them)."""
    for model in ("router", "chat"):
        try:
            _classify(model, ROUTER_PROMPT.replace("QUESTION_HERE", "Question: Who will win?"))
        except Exception:
            pass
