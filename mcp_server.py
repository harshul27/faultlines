"""Fault Lines as an MCP server: the analyst's match tools, callable by any agent (GitHub Copilot, Foundry, Claude).

    Streamable HTTP at /mcp on the same app as the page (server.py mounts it). VS Code: .vscode/mcp.json.

Read-only and deterministic: the same functions the Analyst agent calls (agents.py), on the live demo match,
a demo replay, or a user's upload or live feed. No model calls, so no AI credits are spent.
ponytail: no auth, like the rest of the public demo. Add a token verifier before exposing private uploads.
"""
import asyncio
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

import agents
import gameplan
from match import TEAMS, using

mcp = MCPServer(
    "Fault Lines",
    instructions="Live football match intelligence on synthetic matches. Start with list_matches, then match_state "
                 "for the fault line (the player a team's passing depends on). match is 'live' (default), a demo "
                 "seed, 'upload:<id>' or 'feed:<id>'; minute defaults to the latest minute played. team is "
                 "'home', 'away' or a club name.")
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _server():
    import server  # the running app module; imported late because server.py mounts this one
    return server


def _name(lineups, side):
    with using(lineups):
        return TEAMS[side]["name"]


def _seed(match):
    try:
        return _server()._parse_seed(match)
    except ValueError:
        raise ToolError(f"unknown match {match!r}: call list_matches") from None


async def _run(fn, match, minute, *args):
    s = _server()
    seed = _seed(match)
    events, lineups, agent_seed = s._source(seed)
    events = list(events)  # the live feed keeps growing while we work
    if not events:
        raise ToolError("no events yet: the match has not started")
    last = int(events[-1]["t"] // 60) + 1
    minute = max(1, min(minute or last, last))

    def go():
        with using(lineups):
            plans = {t: gameplan.default_plan(t) for t in TEAMS}
            agents.CTX.set({"events": [e for e in events if e["t"] < minute * 60], "minute": minute, "seed": agent_seed,
                            "trace": [], "outputs": [], "plans": plans, "plan_team": "home", "custom_plan": False})
            return fn(*args)
    return {"match": str(seed), "minute": minute, "result": await asyncio.to_thread(go)}


@mcp.tool(annotations=READ)
def list_matches() -> dict:
    """Matches you can analyse: the live demo match, demo replays, and matches users uploaded or stream in."""
    s = _server()
    live = s.channel.state or {}
    return {
        "live": {"match": "live", "minute": live.get("minute"),
                 "teams": [_name(None, t) for t in ("home", "away")], "note": "demo match, replayed in real time"},
        "demo_replays": [{"match": str(seed)} for seed in s.DEMO_SEEDS],
        "uploads": [{"match": f"upload:{i}", "teams": [u["lineups"][t]["name"] for t in ("home", "away")],
                     "quality": u["report"].get("score")} for i, u in s.uploads.items()],
        "feeds": [{"match": f"feed:{i}", "teams": [f.lineups[t]["name"] for t in ("home", "away")],
                   "events": len(f.raw), "idle_seconds": round(time.time() - f.touched)} for i, f in s.feeds.items()],
    }


@mcp.tool(annotations=READ)
async def match_state(match: str = "live", minute: int = 0) -> dict:
    """Score, goals and, for each team: the fault-line player (whose removal would most break its passing network),
    passing-spread trend, forward progression and pass completion over the last 15 minutes."""
    return await _run(agents.match_state, match, minute)


@mcp.tool(annotations=READ)
async def player_profile(player: str, match: str = "live", minute: int = 0) -> dict:
    """One player's passing involvement, network share, fragility and expected points added so far."""
    return await _run(agents.player_profile, match, minute, player)


@mcp.tool(annotations=READ)
async def what_if_remove(player: str, match: str = "live", minute: int = 0) -> dict:
    """Counterfactual: replay the rest of the match without this player and report what the team loses
    (progression, connectivity, win probability). Use before advising a substitution or man-marking."""
    return await _run(agents.what_if_remove, match, minute, player)


@mcp.tool(annotations=READ)
async def win_probability(match: str = "live", minute: int = 0) -> dict:
    """Monte Carlo win/draw/loss probabilities and likely final scores from the current state."""
    return await _run(agents.win_probability, match, minute)


@mcp.tool(annotations=READ)
async def compare_windows(team: str, earlier_minute: int, later_minute: int, match: str = "live", minute: int = 0) -> dict:
    """How a team's passing spread, progression and completion changed between two match minutes."""
    return await _run(agents.compare_windows, match, minute, team, earlier_minute, later_minute)


@mcp.tool(annotations=READ)
async def squad_assessment(team: str = "home", match: str = "live", minute: int = 0) -> dict:
    """Every player rated against the team's game plan: plan fit, form, load vs plan, flags, strengths and concerns,
    plus substitution candidates."""
    return await _run(agents.squad_assessment, match, minute, team)


@mcp.tool(annotations=READ)
async def assess_player(player: str, match: str = "live", minute: int = 0) -> dict:
    """One player's game-plan assessment: weighted metrics for their role, plan fit, load and flags."""
    return await _run(agents.assess_player, match, minute, player)


@mcp.tool(annotations=READ)
async def plan_execution(team: str = "home", match: str = "live", minute: int = 0) -> dict:
    """Is the game plan working? Each principle and duty scored against what the team is actually doing."""
    return await _run(agents.plan_execution, match, minute, team)


@mcp.tool(annotations=READ)
async def zone_report(team: str = "home", last_minutes: int = 0, match: str = "live", minute: int = 0) -> dict:
    """Where on the pitch a team attacks, progresses and loses the ball (whole match, or the last N minutes)."""
    return await _run(agents.zone_report, match, minute, team, last_minutes)


@mcp.tool(annotations=READ)
def data_quality(match: str) -> dict:
    """The cleaning pipeline's quality report for an uploaded match or live feed: score, dimensions, fixes,
    imputed fields, quarantined rows and caveats."""
    s = _server()
    kind, _, sid = str(_seed(match)).partition(":")
    if kind == "upload":
        return s.uploads[sid]["report"]
    if kind == "feed":
        return s._feed_report(s.feeds[sid])
    return {"note": "demo matches are synthetic and clean by construction; quality reports exist for uploads and feeds"}


# ponytail: public read-only server, so no DNS-rebinding host check (that guards servers bound to localhost)
app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
