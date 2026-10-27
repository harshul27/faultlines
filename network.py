"""Live pass-network analysis: who holds the team together, and what breaks without them."""
from collections import defaultdict
import networkx as nx
import numpy as np
from scipy.sparse.csgraph import shortest_path

from match import BASE, TEAMS, squad


def pass_graph(events, team, t_from, t_to):
    """Undirected graph of completed passes in [t_from, t_to); edge weight = passes between the pair."""
    G = nx.Graph()
    G.add_nodes_from(TEAMS[team]["players"])
    for e in events:
        if e["team"] == team and e["type"] == "pass" and e["success"] and t_from <= e["t"] < t_to:
            a, b = e["player"], e["recipient"]
            G.add_edge(a, b, weight=G[a][b]["weight"] + 1 if G.has_edge(a, b) else 1)
    total = G.size(weight="weight")
    for _, _, d in G.edges(data=True):
        d["dist"] = total / d["weight"]  # share-based, so connectivity measures shape, not passing volume
    return G


def _inv_dist(W):
    """1/shortest-path matrix (0 on the diagonal and for unreachable pairs). W: dense 'dist' matrix, 0 = no edge."""
    with np.errstate(divide="ignore"):
        inv = 1 / shortest_path(W, directed=False)
    np.fill_diagonal(inv, 0)
    return inv


def _dist_matrix(G):
    return nx.to_numpy_array(G, nodelist=list(G), weight="dist", nonedge=0.0)


def fragility(G):
    """Per player: share of the *other* players' passing connectivity lost if that player disappears.
    ponytail: 11 nodes, so brute-force removal is instant; use approximations only for tracking-scale graphs."""
    W, nodes = _dist_matrix(G), list(G)
    full = _inv_dist(W)
    out = {}
    for i, p in enumerate(nodes):
        keep = [j for j in range(len(nodes)) if j != i]
        with_p = full[np.ix_(keep, keep)].sum()
        out[p] = 0.0 if with_p == 0 else max(0.0, 1 - _inv_dist(W[np.ix_(keep, keep)]).sum() / with_p)
    return out


def efficiency(G):
    """Connectivity index: mean 1/shortest-path over ordered pairs, x1000 only to make it readable."""
    n = len(G)
    return 0.0 if n < 2 else 1000 * _inv_dist(_dist_matrix(G)).sum() / (n * (n - 1))


def score_at(events, t):
    s = {"home": 0, "away": 0}
    for e in events:
        if e["t"] < t and e["goal"]:
            s[e["team"]] += 1
    return s


GOALS = {"home": 1.55, "away": 1.25}  # Premier League goals per match by side, 2010/11-present (openfootball, CC0)


def expected_points(diff, minutes_left, team):
    """Expected league points (3 win, 1 draw) from a goal difference with minutes left: remaining goals for each
    side are Poisson at the league rate. ponytail: league-average rates; use team ratings for a sharper model."""
    other = "away" if team == "home" else "home"
    lf, la = GOALS[team] * minutes_left / 95, GOALS[other] * minutes_left / 95
    k = np.arange(11)
    pf = np.exp(-lf) * lf ** k / np.cumprod(np.r_[1, k[1:]])
    pa = np.exp(-la) * la ** k / np.cumprod(np.r_[1, k[1:]])
    d = diff + k[:, None] - k[None, :]
    joint = pf[:, None] * pa[None, :]
    return float(3 * joint[d > 0].sum() + joint[d == 0].sum())


def points_added(events, team, t_to):
    """Expected points added (xPA) per player: each shot's xG (shooter) and the xG it came from (passer, as xA) times
    the points a goal would have been worth at that moment. Late chances in a close game weigh most; chances at 4-0 almost nothing."""
    pts, s, other = defaultdict(float), {"home": 0, "away": 0}, "away" if team == "home" else "home"
    for i, e in enumerate(events):
        if e["t"] >= t_to:
            break
        if e["team"] == team and e["type"] == "shot":
            left, d = max(0.0, 95 - e["t"] / 60), s[team] - s[other]
            value = e["xg"] * (expected_points(d + 1, left, team) - expected_points(d, left, team))
            pts[e["player"]] += value
            prev = events[i - 1] if i else None
            if prev and prev["team"] == team and prev["type"] == "pass" and prev["success"] and prev["recipient"] == e["player"]:
                pts[prev["player"]] += value
        if e["goal"]:
            s[e["team"]] += 1
    return dict(pts)


def team_view(events, team, minute, window=15):
    """Everything the explainer and the overlay need for one team at a given minute."""
    t_to, t_from = minute * 60, max(0, minute - window) * 60
    G = pass_graph(events, team, t_from, t_to)
    G_prev = pass_graph(events, team, max(0, t_from - window * 60), t_from)
    frag, frag_prev = fragility(G), fragility(G_prev)
    btw = nx.betweenness_centrality(G, weight="dist")
    xpa = points_added(events, team, t_to)

    loc = defaultdict(list)
    for e in events:
        if e["team"] == team and e["type"] == "pass" and t_from <= e["t"] < t_to:
            loc[e["player"]].append((e["x"], e["y"]))
            if e["success"]:
                loc[e["recipient"]].append((e["end_x"], e["end_y"]))

    roles = squad(team)
    players = []
    for p in G:
        pts = loc[p] or [BASE[roles[p]]]
        players.append(dict(
            name=p, role=roles[p],
            x=round(sum(a for a, _ in pts) / len(pts), 1), y=round(sum(b for _, b in pts) / len(pts), 1),
            passes=int(G.degree(p, weight="weight")),
            betweenness=round(btw[p], 3), fragility=round(frag[p], 3),
            fragility_prev=round(frag_prev[p], 3), xpa=round(xpa.get(p, 0), 2)))
    players.sort(key=lambda r: -r["fragility"])

    def flow(lo, hi):  # ball security and progression in a window
        ps = [e for e in events if e["team"] == team and e["type"] == "pass" and lo <= e["t"] < hi]
        return (sum(e["success"] and e["end_x"] - e["x"] > 10 for e in ps),
                sum(e["success"] for e in ps) / len(ps) if ps else 0.0)

    (prog, comp), (prog_prev, comp_prev) = flow(t_from, t_to), flow(max(0, t_from - window * 60), t_from)
    return dict(
        team=team, name=TEAMS[team]["name"], minute=minute, window=window,
        progressive=prog, progressive_prev=prog_prev, completion=round(comp, 3), completion_prev=round(comp_prev, 3),
        efficiency=round(efficiency(G), 3), efficiency_prev=round(efficiency(G_prev), 3),
        players=players, spof=players[0],
        edges=[(a, b, d["weight"]) for a, b, d in G.edges(data=True)])

