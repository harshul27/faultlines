"""Realistic noise for robustness tests: turn a clean synthetic match into the kind of file real providers send.

    text, truth = noisy_csv(events, teams, seed=1, level=1.0)

The clean events are the truth, so a test can measure exactly what the pipeline recovers. level scales every rate.
"""
import random

ALIASES = {"t": "Time", "team": "Team", "type": "Event", "player": "Player Name", "player_role": "Position",
           "recipient": "Receiver", "x": "start_x", "y": "start_y", "end_x": "end_x", "end_y": "end_y",
           "success": "Outcome", "pressure": "under_pressure", "xg": "xG", "goal": "is_goal"}


def _variant(name, rng):
    first, *rest = name.split()
    return rng.choice([f"{first[0]}. {' '.join(rest)}", name.lower(), f"  {name}  ", name.replace(rest[-1], rest[-1][:-1]) if rest else name])


def noisy_csv(events, teams, seed=1, level=1.0):
    """-> (CSV text, truth). truth: {"broken_rows": set of source rows that should be quarantined, "out_of_scope": n}."""
    rng = random.Random(seed)
    rate = lambda p: rng.random() < p * level
    names = {s: teams[s]["name"] for s in ("home", "away")}
    roles = {s: teams[s]["roles"] for s in ("home", "away")}
    varied = {rng.choice(list(roles["home"])), rng.choice(list(roles["away"]))}
    no_role = set(rng.sample(list(roles["home"]), 2) + rng.sample(list(roles["away"]), 1))
    rows = []
    for e in events:
        half = 1 if e["t"] < 45 * 60 else 2
        attacks_right = (e["team"] == "home") == (half == 1)  # fixed pitch frame: teams switch ends at half time
        fx = (lambda v: v) if attacks_right else (lambda v: 100 - v)
        clock = e["t"] - (45 * 60 if half == 2 else 0)  # each half's clock restarts at 00:00
        r = {"Time": f"{int(clock // 60):02d}:{clock % 60:04.1f}", "Half": half, "Team": names[e["team"]],
             "Event": "Pass" if e["type"] == "pass" else "Shot" if e["type"] == "shot" else "Own Goal",
             "Player Name": e["player"], "Position": "" if e["player"] in no_role else roles[e["team"]].get(e["player"], ""),
             "Receiver": e["recipient"] or "", "start_x": fx(e["x"]) * 1.2, "start_y": fx(e["y"]) * 0.8,
             "end_x": fx(e["end_x"]) * 1.2, "end_y": fx(e["end_y"]) * 0.8,
             "Outcome": ("Complete" if e["success"] else "Incomplete") if e["type"] == "pass" else "",
             "under_pressure": "TRUE" if e["pressure"] else "", "xG": e["xg"] if e["type"] == "shot" else "",
             "is_goal": ("Yes" if e["goal"] else "No") if e["type"] == "shot" else ""}
        if e["type"] == "pass" and e["success"]:
            if rate(0.04):
                r["Receiver"] = ""
            if rate(0.02):
                r["Outcome"] = ""
        if e["type"] == "pass" and rate(0.03):
            r["end_x"] = r["end_y"] = ""
        if e["type"] == "shot" and rate(0.3):
            r["xG"] = ""
        for f in ("Player Name", "Receiver"):
            if r[f] in varied and rate(0.3):
                r[f] = _variant(r[f], rng)
        rows.append(r)
        if rate(0.02):
            rows.append(dict(r))  # exact duplicate
        elif rate(0.01):
            d = dict(r)
            m, s = d["Time"].split(":")
            d["Time"] = f"{m}:{min(59.9, float(s) + 0.3):04.1f}"
            rows.append(d)  # near duplicate
        if rate(0.015):
            rows.append({**r, "Event": rng.choice(["Foul Committed", "Substitution", "Ball Recovery", "Pressure"]), "Receiver": ""})
    truth_broken = []
    for _ in range(max(1, int(len(rows) * 0.005 * level))):
        i = rng.randrange(len(rows))
        bad = dict(rows[i])
        bad.update(rng.choice([{"Time": "abc"}, {"start_x": "n/a", "start_y": ""}, {"Team": "Referee"}, {"start_x": 400}]))
        if "start_x" in bad and bad.get("start_y") == "":
            bad["Receiver"] = ""
        rows.insert(i, bad)
        truth_broken.append(id(bad))
    for i in range(len(rows) - 1):  # small local disorder, as with merged feeds
        if rate(0.03):
            rows[i], rows[i + 1] = rows[i + 1], rows[i]
    tag = next(i for i, r in enumerate(rows) if r["Event"] == "Pass")
    rows[tag] = {**rows[tag], "Player Name": f"<b>{rows[tag]['Player Name'].strip()}</b>"}  # markup injection
    broken = {i + 1 for i, r in enumerate(rows) if id(r) in truth_broken}
    out_of_scope = sum(r["Event"] not in ("Pass", "Shot", "Own Goal") for r in rows)
    cols = ["Time", "Half", "Team", "Event", "Player Name", "Position", "Receiver", "start_x", "start_y", "end_x", "end_y",
            "Outcome", "under_pressure", "xG", "is_goal"]
    fmt = lambda v: f"{v:.2f}".replace(".", ",") if isinstance(v, float) else str(v)  # semicolon CSV with decimal commas
    text = "\n".join([";".join(cols)] + [";".join(fmt(r.get(c, "")) for c in cols) for r in rows]) + "\n"
    return text, {"broken_rows": broken, "out_of_scope": out_of_scope, "rows": len(rows)}
