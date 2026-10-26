"""Live ingest: process events one at a time as they arrive and emit timed, machine-readable overlay cues.

    python stream.py [seed] [speed]   # replay a synthetic match in wall-clock time (speed 60 = 1 match minute per second)
"""
import time

from match import TEAMS, simulate
from network import team_view

CUE_SCHEMA = {
    "type": "object",
    "required": ["cue", "match_clock", "t", "team", "headline", "display_seconds", "priority", "latency_ms"],
    "properties": {
        "cue": {"enum": ["goal", "fault_line_shift", "fault_line_alert", "network_narrowing"]},
        "match_clock": {"type": "string", "pattern": r"^\d{1,3}:\d{2}$"},
        "t": {"type": "number", "minimum": 0},
        "team": {"type": "string"},
        "player": {"type": ["string", "null"]},
        "value": {"type": ["number", "null"]},
        "headline": {"type": "string", "maxLength": 120},
        "anchor": {"type": ["object", "null"]},
        "display_seconds": {"type": "integer", "minimum": 1},
        "priority": {"type": "integer", "minimum": 1, "maximum": 3},
        "latency_ms": {"type": "number", "minimum": 0},
    },
}
# ALERT and NARROWING are calibrated on ordinary matches (python evaluate.py --calibrate; seeds 7000+, never used by the
# tests) so each cue type fires in about half of ordinary team-matches: a cue marks what is unusual for the league.
ALERT, NARROWING = 0.40, -0.18  # fault-line share that triggers an alert; drop in passing spread (play narrowing)
REARM, MARGIN, HOLD, MIN_GAP = round(ALERT - 0.05, 2), 0.05, 120, 300  # re-arm 5 pts lower; a new fault line must lead by 5 pts
                                                            # for 2 minutes; seconds between cues of one kind


class LiveEngine:
    def __init__(self, window=15):
        self.window, self.events, self.cues = window, [], []
        self.spof, self.last, self.armed, self.challenger = {}, {}, {}, {}

    def _cue(self, kind, e, team, headline, prio, player=None, value=None, anchor=None):
        key = (kind, team)
        if kind != "goal" and e["t"] - self.last.get(key, -1e9) < MIN_GAP:
            return None
        self.last[key] = e["t"]
        return dict(cue=kind, match_clock=f"{int(e['t'] // 60)}:{int(e['t'] % 60):02d}", t=e["t"], team=TEAMS[team]["name"],
                    player=player, value=value, headline=headline, anchor=anchor, display_seconds=8 if prio > 1 else 5,
                    priority=prio)

    def ingest(self, e):
        """Add one event; return the cues it triggers (each stamped with processing latency)."""
        start = time.perf_counter()
        self.events.append(e)
        out = []
        if e["goal"]:
            out.append(self._cue("goal", e, e["team"], f"GOAL {e['player']}", 3, e["player"], e["xg"]))
        # Network only changes on completed passes; wait for one full window before judging it
        if e["type"] == "pass" and e["success"] and e["t"] >= self.window * 60:
            team = e["team"]
            v = team_view(self.events, team, e["t"] / 60, self.window)
            s, prev = v["spof"], self.spof.get(team)
            anchor = {"x": s["x"], "y": s["y"]}
            if prev and s["name"] != prev["name"]:
                incumbent = next(q for q in v["players"] if q["name"] == prev["name"])
                name, since = self.challenger.get(team, (None, 0))
                if s["fragility"] - incumbent["fragility"] < MARGIN:  # hysteresis: not clearly overtaken
                    s = incumbent
                    self.challenger.pop(team, None)
                elif name != s["name"] or e["t"] - since < HOLD:  # debounce: the challenger must stay ahead for HOLD s
                    if name != s["name"]:
                        self.challenger[team] = (s["name"], e["t"])
                    s = incumbent
                else:
                    self.challenger.pop(team, None)
                    if s["fragility"] >= ALERT:
                        out.append(self._cue("fault_line_shift", e, team, f"Play now runs through {s['name']}", 2,
                                             s["name"], s["fragility"], anchor))
            else:
                self.challenger.pop(team, None)
            if s["fragility"] < REARM:
                self.armed[team] = True
            if s["fragility"] >= ALERT and self.armed.get(team, True):
                self.armed[team] = False
                out.append(self._cue("fault_line_alert", e, team,
                                     f"{s['name']} holds {round(s['fragility'] * 100)}% of the passing network", 2,
                                     s["name"], s["fragility"], anchor))
            if v["efficiency_prev"] and e["t"] >= 2 * self.window * 60:
                change = v["efficiency"] / v["efficiency_prev"] - 1
                if change <= NARROWING:
                    out.append(self._cue("network_narrowing", e, team,
                                         f"{TEAMS[team]['name']} play narrowing: passing spread down {round(-change * 100)}%", 2,
                                         None, round(change, 3), None))
            self.spof[team] = s
        out = [c for c in out if c]
        ms = (time.perf_counter() - start) * 1000
        for c in out:
            c["latency_ms"] = round(ms, 1)
        self.cues += out
        return out, ms


def replay(seed=7, speed=60):
    """Yield events in wall-clock time, as a live feed would deliver them."""
    t0 = time.monotonic()
    for e in simulate(seed):
        wait = e["t"] / speed - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)
        yield e


if __name__ == "__main__":
    import json
    import sys
    seed, speed = (int(sys.argv[1]) if len(sys.argv) > 1 else 7), (float(sys.argv[2]) if len(sys.argv) > 2 else 60)
    eng = LiveEngine()
    for ev in replay(seed, speed):
        for cue in eng.ingest(ev)[0]:
            print(json.dumps(cue), flush=True)
