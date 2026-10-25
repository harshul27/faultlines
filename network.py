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


def clutch(events, team, t_to):
    """Late-game impact: actions from 75' while not winning. Pass 1, progressive pass +2, shot 3, goal 10."""
    pts, s, other = defaultdict(float), {"home": 0, "away": 0}, "away" if team == "home" else "home"
    for e in events:
        if e["t"] >= t_to:
            break
        if e["team"] == team and e["minute"] >= 75 and s[team] <= s[other]:
            if e["type"] == "pass" and e["success"]:
                pts[e["player"]] += 1 + (2 if e["end_x"] - e["x"] > 10 else 0)
            elif e["type"] == "shot":
                pts[e["player"]] += 3 + (10 if e["goal"] else 0)
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
    cl = clutch(events, team, t_to)

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
            fragility_prev=round(frag_prev[p], 3), clutch=round(cl.get(p, 0), 1)))
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


def connectivity_timeline(events, team, upto, window=15):
    return [(m, round(efficiency(pass_graph(events, team, max(0, m - window) * 60, m * 60)), 3))
            for m in range(5, upto + 1)]
