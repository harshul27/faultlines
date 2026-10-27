"""Test: compare the calibrated generator and the strength model against HELD-OUT data. Run: python calibration/validate.py
Writes reports/validation.md and exits non-zero if any check fails (so it doubles as a smoke test).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # the app modules

import json
import sys
from pathlib import Path

import numpy as np

from calibrate import TARGETS, TEST, TRAIN, load, results_summary, scores, simulate_many, walk_forward
from match import params

ROOT = Path(__file__).resolve().parents[1]
SEEDS = range(50_000, 50_380)  # one synthetic season; never used in fitting (fit/polish used 1000-1029, 2000-2149)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = params()
    rows = []  # (group, metric, synthetic, real, tolerance, ok)

    def check(group, metric, syn, real, tol, ok):
        rows.append((group, metric, syn, real, tol, bool(ok)))

    # 1. Strength model: out-of-sample forecasts of every held-out match (each season predicted from earlier seasons only)
    alpha = json.loads((ROOT / "src/calibration.json").read_text())["strength_model"]["alpha"]
    probs, outcomes = walk_forward(TEST, alpha)
    train = [m for s in TRAIN for m in load(s)]
    base = np.array([np.mean([h > a for _, _, h, a in train]), np.mean([h == a for _, _, h, a in train]),
                     np.mean([h < a for _, _, h, a in train])])
    model, naive = scores(probs, outcomes), scores(np.tile(base, (len(outcomes), 1)), outcomes)
    check("Strength model (test seasons)", "log loss vs base-rate baseline", round(model["log_loss"], 4),
          round(naive["log_loss"], 4), "must beat baseline", model["log_loss"] < naive["log_loss"])
    check("Strength model (test seasons)", "Brier score vs baseline", round(model["brier"], 4),
          round(naive["brier"], 4), "must beat baseline", model["brier"] < naive["brier"])
    check("Strength model (test seasons)", "accuracy (W/D/L)", f"{model['accuracy']:.1%}", f"{naive['accuracy']:.1%}",
          "must beat baseline", model["accuracy"] > naive["accuracy"])

    # 2. Generator results vs held-out real results
    sims = simulate_many(p, SEEDS)
    hg = np.array([m["home_goals"] for m in sims]); ag = np.array([m["away_goals"] for m in sims])
    real = results_summary([m for s in TEST for m in load(s)])
    syn_dist = {str(k): float((np.minimum(hg + ag, 6) == k).mean()) for k in range(7)}
    tv = lambda a, b: 0.5 * sum(abs(a[k] - b[k]) for k in a)
    tvd = tv(syn_dist, real["total_goals_dist"])
    # Tolerance from the data: the largest gap any single real training season shows against the same test pool
    tvd_tol = max(tv(results_summary(load(s))["total_goals_dist"], real["total_goals_dist"]) for s in TRAIN)
    for metric, syn, tol in [("goals_per_match", (hg + ag).mean(), 0.25), ("home_win", (hg > ag).mean(), 0.05),
                             ("draw", (hg == ag).mean(), 0.05), ("away_win", (hg < ag).mean(), 0.05),
                             ("nil_nil", ((hg + ag) == 0).mean(), 0.04)]:
        check("Results vs real 2023-24..present", metric, round(float(syn), 3), round(real[metric], 3), f"±{tol}",
              abs(syn - real[metric]) <= tol)
    check("Results vs real 2023-24..present", "total-goals distribution (total variation distance)", round(tvd, 3), 0,
          f"≤ {tvd_tol:.3f} (worst real season)", tvd <= tvd_tol)

    # 3. Generator event stats vs published averages of later seasons (fit used 2023-24 only)
    avg = lambda k: float(np.mean([m[k] for m in sims]))
    syn = {"passes_per_match": avg("passes"), "pass_accuracy_pct": avg("pass_accuracy"),
           "long_balls_per_match": avg("long_balls"), "high_turnovers_per_match": avg("high_turnovers")}
    tols = {"passes_per_match": ("rel", 0.12), "pass_accuracy_pct": ("abs", 1.5),
            "long_balls_per_match": ("rel", 0.15), "high_turnovers_per_match": ("rel", 0.40)}
    for season in TARGETS["test_seasons"]:
        for k, real_v in season["values"].items():
            kind, tol = tols[k]
            err = abs(syn[k] - real_v) / (real_v if kind == "rel" else 1)
            check(f"Event stats vs published {season['season']}", k, round(syn[k], 1), real_v,
                  f"±{tol:.0%}" if kind == "rel" else f"±{tol} pts", err <= tol)

    # Reported, not tested (no verified public figure)
    info = {"shots_per_match": round(avg("shots"), 1),
            "goal-count variance, synthetic vs real (known limitation: synthetic scoring is burstier)":
                f"{float((hg + ag).var()):.2f} vs {float(np.var([h + a for s in TEST for _, _, h, a in load(s)])):.2f}", "final_third_share_pct (fit target, not a test)": round(avg("final_third_share"), 1),
            "matches_simulated": len(sims), "test_matches_real": real["matches"]}

    lines = ["# Validation report", "",
             f"Generator and strength model trained on {TRAIN[0]}..{TRAIN[-1]}; everything below is held out.", "",
             "| Group | Metric | Synthetic / model | Real / baseline | Tolerance | Pass |", "|---|---|---|---|---|---|"]
    lines += [f"| {g} | {m} | {s} | {r} | {t} | {'✅' if ok else '❌'} |" for g, m, s, r, t, ok in rows]
    lines += ["", "Reported only (no verified public figure to test against):", ""] + [f"- {k}: {v}" for k, v in info.items()]
    lines += ["", "Data: openfootball/football.json (CC0) results; premierleague.com published averages (see targets.json)."]
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/validation.md").write_text("\n".join(lines), encoding="utf-8")
    (ROOT / "reports/validation.json").write_text(json.dumps(dict(rows=rows, info=info), indent=1))
    print("\n".join(lines))
    failed = [r for r in rows if not r[-1]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
