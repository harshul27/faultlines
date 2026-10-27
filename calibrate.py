"""Train: fit the team-strength model and the generator's knobs on TRAINING data only. Run: python calibrate.py

Data (permissive only):
  * openfootball/football.json, CC0 public domain: Premier League results 2010/11 - present (data/openfootball/).
  * Published league averages from premierleague.com articles (facts, cited in targets.json).
Split by time, so nothing from the future leaks into training:
  train = 2010/11 - 2022/23 (13 seasons), test = 2023/24 - present (held out, used only by validate.py).
For published pass stats the same idea applies: fit to 2023/24, test on 2024/25 and 2025/26.
"""
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson
from sklearn.linear_model import PoissonRegressor

from match import DEFAULTS, simulate

ROOT = Path(__file__).parent
SEASONS = [f"{y}-{str(y + 1)[2:]}" for y in range(2010, 2027)]
TRAIN = SEASONS[:13]   # 2010-11 .. 2022-23
TEST = SEASONS[13:]    # 2023-24 .. 2026-27 (in progress)
TARGETS = json.loads((ROOT / "targets.json").read_text())


# ---------- data ----------
def load(season):
    """[(home, away, home_goals, away_goals)] for played matches."""
    out = []
    for m in json.loads((ROOT / "data/openfootball" / f"{season}.json").read_text())["matches"]:
        s = m.get("score")
        ft = s.get("ft") if isinstance(s, dict) else s  # some files store a bare [h, a] full-time score
        if ft and len(ft) == 2:
            out.append((m["team1"], m["team2"], int(ft[0]), int(ft[1])))
    return out


def results_summary(matches):
    n = len(matches)
    hg = np.array([m[2] for m in matches]); ag = np.array([m[3] for m in matches])
    tot = hg + ag
    return dict(matches=n, goals_per_match=float(tot.mean()), home_win=float((hg > ag).mean()),
                draw=float((hg == ag).mean()), away_win=float((hg < ag).mean()), nil_nil=float((tot == 0).mean()),
                total_goals_dist={str(k): float((np.minimum(tot, 6) == k).mean()) for k in range(7)})


# ---------- team-strength model (Poisson: log rate = mu + home + attack[team-season] + defence[opponent-season]) ----------
def fit_strength(seasons, alpha):
    rows, keys = [], {}
    for s in seasons:
        for h, a, hg, ag in load(s):
            for team, opp, goals, home in ((h, a, hg, 1), (a, h, ag, 0)):
                ka, kd = ("att", s, team), ("def", s, opp)
                keys.setdefault(ka, len(keys)); keys.setdefault(kd, len(keys))
                rows.append((home, keys[ka], keys[kd], goals))
    X = np.zeros((len(rows), 1 + len(keys)))
    for i, (home, ia, idf, _) in enumerate(rows):
        X[i, 0], X[i, 1 + ia], X[i, 1 + idf] = home, 1, 1
    y = np.array([r[3] for r in rows])
    m = PoissonRegressor(alpha=alpha, max_iter=1000).fit(X, y)
    coef = {k: float(m.coef_[1 + i]) for k, i in keys.items()}
    return dict(mu=float(m.intercept_), home=float(m.coef_[0]), coef=coef)


def wdl(lh, la, k=11):
    ph, pa = poisson.pmf(np.arange(k), lh), poisson.pmf(np.arange(k), la)
    grid = np.outer(ph, pa)
    return np.array([np.tril(grid, -1).sum(), np.trace(grid), np.triu(grid, 1).sum()])


def forecast_season(model, prev, season):
    """Predict `season` using ratings from the previous season; promoted clubs get the average promoted-club rating."""
    att = {t: v for (kind, s, t), v in model["coef"].items() if kind == "att" and s == prev}
    dfn = {t: v for (kind, s, t), v in model["coef"].items() if kind == "def" and s == prev}
    promoted = model["promoted"]
    probs, outcomes = [], []
    for h, a, hg, ag in load(season):
        lh = math.exp(model["mu"] + model["home"] + att.get(h, promoted["att"]) + dfn.get(a, promoted["def"]))
        la = math.exp(model["mu"] + att.get(a, promoted["att"]) + dfn.get(h, promoted["def"]))
        probs.append(wdl(lh, la)); outcomes.append(0 if hg > ag else 1 if hg == ag else 2)
    return np.array(probs), np.array(outcomes)


def promoted_prior(model, seasons):
    teams = {s: {t for (k, ss, t) in model["coef"] if ss == s and k == "att"} for s in seasons}
    att, dfn = [], []
    for prev, s in zip(seasons, seasons[1:]):
        for t in teams[s] - teams[prev]:
            att.append(model["coef"][("att", s, t)]); dfn.append(model["coef"][("def", s, t)])
    return {"att": float(np.mean(att)), "def": float(np.mean(dfn))}


def scores(probs, outcomes):
    p = np.clip(probs[np.arange(len(outcomes)), outcomes], 1e-12, 1)
    onehot = np.eye(3)[outcomes]
    return dict(log_loss=float(-np.log(p).mean()), brier=float(((probs - onehot) ** 2).sum(1).mean()),
                accuracy=float((probs.argmax(1) == outcomes).mean()), n=int(len(outcomes)))


def walk_forward(seasons_to_predict, alpha):
    """For each season S: fit on every season before S (but never beyond TRAIN when tuning), predict S."""
    all_p, all_o = [], []
    for s in seasons_to_predict:
        hist = SEASONS[:SEASONS.index(s)]
        m = fit_strength(hist, alpha)
        m["promoted"] = promoted_prior(m, hist)
        p, o = forecast_season(m, hist[-1], s)
        all_p.append(p); all_o.append(o)
    return np.vstack(all_p), np.concatenate(all_o)


def train_strength():
    # Tune regularisation inside the training years only (predict 2020-21..2022-23 from earlier seasons)
    tune = {a: scores(*walk_forward(TRAIN[-3:], a))["log_loss"] for a in (1e-4, 1e-3, 3e-3, 1e-2)}
    alpha = min(tune, key=tune.get)
    m = fit_strength(TRAIN, alpha)
    att = np.array([v for (k, *_), v in m["coef"].items() if k == "att"])
    dfn = np.array([m["coef"][("def",) + key[1:]] for key in m["coef"] if key[0] == "att"])
    return dict(alpha=alpha, tuning_log_loss={str(k): v for k, v in tune.items()}, home_adv=m["home"],
                att_sd=float(att.std()), def_sd=float(dfn.std()), att_def_corr=float(np.corrcoef(att, dfn)[0, 1]))


# ---------- generator calibration ----------
LONG_M, HIGH_TO_X = 32, 100 - 40 / 1.05  # long ball >= 32 m; high turnover = won <= 40 m from goal


def sim_metrics(events):
    passes = [e for e in events if e["type"] == "pass"]
    n = len(passes)
    dist = [math.hypot((e["end_x"] - e["x"]) * 1.05, (e["end_y"] - e["y"]) * 0.68) for e in passes]
    lost = [e for e in passes if not e["success"]]
    goals = {"home": 0, "away": 0}
    for e in events:
        if e["goal"]:
            goals[e["team"]] += 1
    return dict(
        passes=n, pass_accuracy=100 * sum(e["success"] for e in passes) / n,
        long_balls=sum(d >= LONG_M for d in dist), high_turnovers=sum(100 - e["end_x"] >= HIGH_TO_X for e in lost),
        final_third_share=100 * sum(e["end_x"] > 200 / 3 for e in passes) / n,
        shots=sum(e["type"] == "shot" for e in events), home_goals=goals["home"], away_goals=goals["away"])


def simulate_many(p, seeds):
    return [sim_metrics(list(simulate(s, p=p, storyline=False))) for s in seeds]


KNOBS = ["step", "base_ok", "reach", "forward", "shot_prob", "xg_scale", "zone_risk", "compact", "press"]


def fit_generator(strength, goals_target, seeds=range(1000, 1030)):
    t = TARGETS["fit_season"]["values"]
    fixed = {k: strength[k] for k in ("home_adv", "att_sd", "def_sd", "att_def_corr")}

    def loss(v):
        p = {**DEFAULTS, **fixed, **dict(zip(KNOBS, v))}
        if p["base_ok"] > 1 or p["compact"] >= 0.9 or min(v) <= 0:
            return 1e6
        ms = simulate_many(p, seeds)
        avg = lambda k: np.mean([m[k] for m in ms])
        rel = lambda got, want, tol: ((got - want) / want / tol) ** 2  # tolerance as a fraction of the target
        return (rel(avg("passes"), t["passes_per_match"], 0.03)
                + rel(avg("pass_accuracy"), t["pass_accuracy_pct"], 0.006)
                + rel(avg("long_balls"), t["long_balls_per_match"], 0.05)
                + rel(avg("high_turnovers"), t["high_turnovers_per_match"], 0.10)
                + rel(avg("final_third_share"), TARGETS["fit_extra"]["final_third_share_pct"], 0.05)
                + rel(avg("home_goals") + avg("away_goals"), goals_target, 0.03))

    x0 = [DEFAULTS[k] for k in KNOBS]
    res = minimize(loss, x0, method="Nelder-Mead", options=dict(maxiter=350, xatol=1e-3, fatol=1e-3))
    return {**DEFAULTS, **fixed, **{k: round(float(v), 4) for k, v in zip(KNOBS, res.x)}}, float(res.fun)


def polish(gen, goals_target, seeds=range(2000, 2150), rounds=4):
    """Nelder-Mead on 30 seeds is noisy; nudge the three near-monotonic knobs on 150 more TRAINING seeds."""
    t = TARGETS["fit_season"]["values"]
    for _ in range(rounds):
        ms = simulate_many(gen, seeds)
        avg = lambda k: float(np.mean([m[k] for m in ms]))
        gen["step"] = round(gen["step"] * avg("passes") / t["passes_per_match"], 4)
        gen["base_ok"] = round(min(1.0, gen["base_ok"] + (t["pass_accuracy_pct"] - avg("pass_accuracy")) / 100), 4)
        gen["xg_scale"] = round(gen["xg_scale"] * goals_target / (avg("home_goals") + avg("away_goals")), 4)
    return gen


def recent_goals():
    """Goal-rate target: the 3 most recent TRAINING seasons (the game drifts; old seasons understate today)."""
    return results_summary([m for s in TRAIN[-3:] for m in load(s)])["goals_per_match"]


if __name__ == "__main__" and "--polish" in __import__("sys").argv:
    saved = json.loads((ROOT / "calib/params.json").read_text())
    saved["generator"] = polish(saved["generator"], recent_goals())
    saved["goals_target"] = recent_goals()
    (ROOT / "calib/params.json").write_text(json.dumps(saved, indent=1))
    print("polished:", saved["generator"])
elif __name__ == "__main__":
    train_matches = [m for s in TRAIN for m in load(s)]
    test_matches = [m for s in TEST for m in load(s)]
    print(f"train {len(train_matches)} matches ({TRAIN[0]}..{TRAIN[-1]}), test {len(test_matches)} ({TEST[0]}..{TEST[-1]}), "
          f"split {len(train_matches) / (len(train_matches) + len(test_matches)):.0%}/"
          f"{len(test_matches) / (len(train_matches) + len(test_matches)):.0%}")
    real_train = results_summary(train_matches)
    strength = train_strength()
    print("strength model:", json.dumps(strength, indent=1))
    gen, final_loss = fit_generator(strength, recent_goals())
    gen = polish(gen, recent_goals())
    print("generator:", gen, "loss", round(final_loss, 3))
    (ROOT / "calib").mkdir(exist_ok=True)
    (ROOT / "calib/params.json").write_text(json.dumps(dict(
        generator=gen, strength_model=strength, fit_loss=final_loss, real_train=real_train, goals_target=recent_goals(),
        split=dict(train=TRAIN, test=TEST, train_matches=len(train_matches), test_matches=len(test_matches)),
        sources=["openfootball/football.json (CC0)", "targets.json (premierleague.com published averages)"]), indent=1))
    print("wrote calib/params.json")
