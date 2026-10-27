"""One snapshot format shared by the live server (server.py) and the static export (export.py -> web/data/).

Coordinates are converted to a single broadcast frame: home attacks left -> right, away right -> left.
"""
from explain import facts, template
from match import TEAMS, params, simulate, strengths
from network import score_at, team_view
from stream import LiveEngine

DEMO_SEEDS = [23, 7, 11]


def _frame(team, x, y):
    return (x, y) if team == "home" else (round(100 - x, 1), round(100 - y, 1))


def team_snapshot(v, team):
    players = []
    for p in v["players"]:
        x, y = _frame(team, p["x"], p["y"])
        players.append({**{k: p[k] for k in ("name", "role", "passes", "fragility", "betweenness", "xpa")}, "x": x, "y": y})
    return dict(name=v["name"], efficiency=v["efficiency"], efficiency_prev=v["efficiency_prev"],
                progressive=v["progressive"], progressive_prev=v["progressive_prev"], completion=v["completion"],
                spof=v["spof"]["name"], players=players, edges=v["edges"])


def snapshot(events, minute, window=15):
    views = {t: team_view(events, t, minute, window) for t in TEAMS}
    other = {"home": "away", "away": "home"}
    score = score_at(events, minute * 60)
    out = dict(minute=round(minute, 2), score=score,
               teams={t: team_snapshot(views[t], t) for t in TEAMS})
    # Explanations for both audiences, so the static site needs no server to personalise
    out["explain"] = {t: {m: template(facts(views[t], views[other[t]], score), m) for m in ("Casual", "Analyst")}
                      for t in TEAMS}
    return out


def compact_event(e):
    x, y = _frame(e["team"], e["x"], e["y"])
    ex, ey = _frame(e["team"], e["end_x"], e["end_y"])
    return dict(t=e["t"], team=e["team"], type=e["type"], player=e["player"], to=e["recipient"],
                x=x, y=y, ex=ex, ey=ey, ok=e["success"], press=e["pressure"], xg=e["xg"], goal=e["goal"])


def match_meta(seed):
    st = strengths(seed, params())
    return dict(seed=seed, teams={t: dict(name=TEAMS[t]["name"], players=TEAMS[t]["players"],
                                          attack=round(st[t]["attack"], 3), defence=round(st[t]["defence"], 3))
                                  for t in TEAMS})


def user_meta(lineups, seed):
    """Meta for a user's match (no team strengths: those exist only for the synthetic clubs)."""
    return dict(seed=seed, source="user", teams={s: dict(name=lineups[s]["name"], players=lineups[s]["players"])
                                                 for s in ("home", "away")})


def timeline(seed, window=15, events=None, meta=None):
    """Whole match for replay + analysis: events, cues, and a snapshot every match minute.
    A user's match passes its own events and meta (inside match.using(lineups))."""
    events = events if events is not None else list(simulate(seed))
    eng = LiveEngine(window)
    for e in events:
        eng.ingest(e)
    end = int(events[-1]["t"] // 60) + 1
    return dict(meta=meta or match_meta(seed), window=window, events=[compact_event(e) for e in events], cues=eng.cues,
                snapshots=[snapshot(events, m, window) for m in range(1, end + 1)])
