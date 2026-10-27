"""Synthetic, football-realistic match generator.

Every event is in the acting team's attacking frame: x=0 own goal, x=100 opponent goal, y=0..100 (pitch 105 x 68 m).
Clubs and players are fictional. Behaviour knobs are calibrated by calibrate.py against public-domain Premier League
results (openfootball, CC0) and published league averages; fitted values live in src/calibration.json.
A seeded storyline is baked in: Northbridge's holding midfielder is the hub, and from 60' Riverside man-mark them.
"""
import contextvars
import json
import math
import random
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

ROLES = ["GK", "RB", "RCB", "LCB", "LB", "CDM", "RCM", "LCM", "RW", "ST", "LW"]
BASE = {
    "GK": (5, 50), "RB": (28, 85), "RCB": (20, 62), "LCB": (20, 38), "LB": (28, 15),
    "CDM": (40, 50), "RCM": (55, 68), "LCM": (55, 32), "RW": (76, 85), "ST": (86, 50), "LW": (76, 15),
}
DEMO_TEAMS = {
    "home": {"name": "Northbridge FC", "players": [
        "Tomas Varga", "Eli Mensah", "Rui Calder", "Jonah Pike", "Sami Okafor", "Luca Ferrand",
        "Owen Brightwell", "Kai Andersen", "Milo Dancy", "Theo Ashgrove", "Rafa Quinlan"]},
    "away": {"name": "Riverside Athletic", "players": [
        "Bram Holt", "Idris Cole", "Nico Strand", "Felix Moreau", "Dario Venn", "Isaac Thorne",
        "Yusuf Kaleo", "Arlo Penrose", "Callum Reyes", "Mateo Lindqvist", "Jude Harrow"]},
}
LINEUPS = contextvars.ContextVar("lineups", default=None)  # a user's match: {"id", "home": {...}, "away": {...}}


class _Teams(Mapping):
    """TEAMS for the match being analysed: the demo clubs unless a user's match is active in this context
    (see using()). Everything reads squads through here, so analysis code is the same for demo and real data."""
    def __getitem__(self, side):
        return (LINEUPS.get() or DEMO_TEAMS)[side]

    def __iter__(self):
        return iter(("home", "away"))

    def __len__(self):
        return 2


TEAMS = _Teams()


@contextmanager
def using(lineups):
    """Analyse a user's match: lineups = {"id": str, "home": {"name", "players", "roles"}, "away": {...}}."""
    token = LINEUPS.set(lineups)
    try:
        yield
    finally:
        LINEUPS.reset(token)


def custom():
    return LINEUPS.get() is not None


HUB = ("home", "Luca Ferrand")  # the storyline's single point of failure
MARK_FROM = 60 * 60            # Riverside start man-marking the hub at 60'
HUB_PULL = 4.0                 # how much more often teammates look for the hub before marking
M_X, M_Y = 1.05, 0.68          # pitch units -> metres

DEFAULTS = dict(
    step=5.5,          # mean seconds between on-ball actions -> passes per match
    base_ok=0.99,      # pass completion before distance/pressure penalties
    reach=14.0,        # how far players look for a pass (pitch units) -> pass length mix
    forward=2.0,       # preference for forward options -> territory / final-third share
    shot_prob=0.22,    # chance an advanced attacker shoots
    xg_scale=0.7,      # shot quality level -> goals per match
    zone_risk=0.3,     # extra pass failure towards the opponent's goal (safe at the back, risky in the final third)
    compact=0.45,      # how much the team block shifts with the ball (0 = fixed formation)
    press=0.25,        # opponent pressure intensity, rising up the pitch
    home_adv=0.0, att_sd=0.0, def_sd=0.0, att_def_corr=0.0,  # team strength prior (log goal-rate scale)
)
_PARAMS_FILE = Path(__file__).with_name("calibration.json")  # written by calibration/calibrate.py


def params():
    p = dict(DEFAULTS)
    if _PARAMS_FILE.exists():
        p.update(json.loads(_PARAMS_FILE.read_text())["generator"])
    return p


def squad(team):
    t = TEAMS[team]
    return dict(t["roles"]) if "roles" in t else dict(zip(t["players"], ROLES))


def strengths(seed, p):
    """Sample attack/defence (log scale) for both fictional clubs from the prior fitted on real seasons."""
    rng = random.Random(seed * 7919)
    out = {}
    for t in TEAMS:
        a = rng.gauss(0, 1)
        d = p["att_def_corr"] * a + math.sqrt(1 - p["att_def_corr"] ** 2) * rng.gauss(0, 1)
        out[t] = {"attack": a * p["att_sd"], "defence": d * p["def_sd"]}
    return out


def shape(role, bx, by, c):
    """Expected position of a role: the block slides and compresses towards the ball; the keeper stays home."""
    x0, y0 = BASE[role]
    return (x0, y0) if role == "GK" else (x0 * (1 - c) + bx * c, y0 * (1 - c / 2) + by * c / 2)


def _pos(role, bx, by, c, rng, sd=5):
    px, py = shape(role, bx, by, c)
    return min(99, max(1, rng.gauss(px, sd))), min(99, max(1, rng.gauss(py, sd)))


def simulate(seed=7, minutes=95, p=None, storyline=True, st=None, absent=None, fork=None):
    """Yield events in time order: dicts with t, minute, team, type, player, recipient, x, y, end_x, end_y, success, pressure, xg, goal.
    absent=(team, player, from_seconds) plays that side a man down from then on (red card / injury, no substitute);
    the random stream is unchanged until then, so a match with and without the absence is a paired experiment.
    fork=(from_seconds, seed) reseeds the random stream once at that time: replays of the same first part with fresh,
    shared randomness afterwards (common random numbers for paired experiments)."""
    p = p or params()
    rng = random.Random(seed)
    roles = {t: squad(t) for t in TEAMS}
    other = {"home": "away", "away": "home"}
    st = st or strengths(seed, p)  # pass st to replay the same two clubs with fresh randomness (Monte Carlo)
    # Goal-rate multiplier per attacking side; split between shot volume and quality
    mult = {t: math.exp(st[t]["attack"] + st[other[t]]["defence"] + (p["home_adv"] if t == "home" else 0)) for t in TEAMS}
    t, team = 0.0, "home"
    holder = TEAMS[team]["players"][5]
    x, y = 50.0, 50.0
    gone = lambda tm, pl: absent is not None and (tm, pl) == tuple(absent[:2]) and t >= absent[2]
    avail = lambda tm: [q for q in roles[tm] if not gone(tm, q)]

    while t < minutes * 60:
        if fork and t >= fork[0]:
            rng.seed(fork[1])
            fork = None
        t += rng.uniform(0.5, 1.5) * p["step"]
        role = roles[team][holder]
        marked = storyline and t >= MARK_FROM

        if x > 74 and role in ("ST", "RW", "LW", "RCM", "LCM") and rng.random() < min(0.9, p["shot_prob"] * math.sqrt(mult[team])):
            dist = math.hypot((100 - x) * M_X, (50 - y) * M_Y)
            xg = round(min(0.7, p["xg_scale"] * math.sqrt(mult[team]) * math.exp(-dist / 11)), 3)
            goal = rng.random() < xg
            yield dict(t=round(t, 1), minute=int(t // 60), team=team, type="shot", player=holder, recipient=None,
                       x=round(x, 1), y=round(y, 1), end_x=100.0, end_y=50.0, success=goal, pressure=False, xg=xg, goal=goal)
            team = other[team]
            holder = TEAMS[team]["players"][5 if goal else 0]  # kickoff by CDM, else goal kick by GK
            if gone(team, holder):
                holder = avail(team)[0]
            x, y = (50.0, 50.0) if goal else (5.0, 50.0)
            continue

        weights = []
        for pl, r in roles[team].items():
            if pl == holder or (r == "GK" and x > 30) or gone(team, pl):
                weights.append(0)
                continue
            px, py = shape(r, x, y, p["compact"])
            w = math.exp(-math.hypot(px - x, py - y) / p["reach"]) * math.exp(p["forward"] * (px - x) / 25)
            if storyline and (team, pl) == HUB:
                w *= 0.5 if marked else HUB_PULL
            weights.append(w)
        recipient = rng.choices(list(roles[team]), weights)[0]
        ex, ey = _pos(roles[team][recipient], x, y, p["compact"], rng)

        pressure = rng.random() < p["press"] * (0.2 + 0.8 * x / 100) or (marked and (team, recipient) == HUB and rng.random() < 0.7)
        p_ok = (p["base_ok"] - math.hypot((ex - x) * M_X, (ey - y) * M_Y) * 0.004 - (0.18 if pressure else 0)
                - p["zone_risk"] * max(0.0, ex - 50) / 50)
        success = rng.random() < p_ok
        yield dict(t=round(t, 1), minute=int(t // 60), team=team, type="pass", player=holder, recipient=recipient,
                   x=round(x, 1), y=round(y, 1), end_x=round(ex, 1), end_y=round(ey, 1), success=success,
                   pressure=pressure, xg=None, goal=False)

        if success:
            holder, x, y = recipient, ex, ey
        else:  # turnover: nearest opponent wins it, coordinates flip into their frame
            team, x, y = other[team], 100 - ex, 100 - ey
            holder = min(avail(team), key=lambda q: math.hypot(BASE[roles[team][q]][0] - x, BASE[roles[team][q]][1] - y))


if __name__ == "__main__":
    import sys
    for e in simulate(int(sys.argv[1]) if len(sys.argv) > 1 else 7):
        print(json.dumps(e))
