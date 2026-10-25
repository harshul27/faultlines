# Validation report

Generator and strength model trained on 2010-11..2022-23; everything below is held out.

| Group | Metric | Synthetic / model | Real / baseline | Tolerance | Pass |
|---|---|---|---|---|---|
| Strength model (test seasons) | log loss vs base-rate baseline | 1.0157 | 1.075 | must beat baseline | ✅ |
| Strength model (test seasons) | Brier score vs baseline | 0.608 | 0.651 | must beat baseline | ✅ |
| Strength model (test seasons) | accuracy (W/D/L) | 49.4% | 42.9% | must beat baseline | ✅ |
| Results vs real 2023-24..present | goals_per_match | 2.953 | 2.981 | ±0.25 | ✅ |
| Results vs real 2023-24..present | home_win | 0.426 | 0.429 | ±0.05 | ✅ |
| Results vs real 2023-24..present | draw | 0.247 | 0.248 | ±0.05 | ✅ |
| Results vs real 2023-24..present | away_win | 0.326 | 0.324 | ±0.05 | ✅ |
| Results vs real 2023-24..present | nil_nil | 0.047 | 0.05 | ±0.04 | ✅ |
| Results vs real 2023-24..present | total-goals distribution (total variation distance) | 0.074 | 0 | ≤ 0.101 (worst real season) | ✅ |
| Event stats vs published 2024-25 | passes_per_match | 940.2 | 893.5 | ±12% | ✅ |
| Event stats vs published 2024-25 | pass_accuracy_pct | 82.5 | 83.7 | ±1.5 pts | ✅ |
| Event stats vs published 2024-25 | long_balls_per_match | 93.5 | 93.4 | ±15% | ✅ |
| Event stats vs published 2024-25 | high_turnovers_per_match | 8.9 | 14.6 | ±40% | ✅ |
| Event stats vs published 2025-26 (first 50 matches) | passes_per_match | 940.2 | 849.1 | ±12% | ✅ |
| Event stats vs published 2025-26 (first 50 matches) | pass_accuracy_pct | 82.5 | 82.6 | ±1.5 pts | ✅ |
| Event stats vs published 2025-26 (first 50 matches) | long_balls_per_match | 93.5 | 99.7 | ±15% | ✅ |
| Event stats vs published 2025-26 (first 50 matches) | high_turnovers_per_match | 8.9 | 11.5 | ±40% | ✅ |

Reported only (no verified public figure to test against):

- shots_per_match: 28.3
- goal-count variance, synthetic vs real (known limitation: synthetic scoring is burstier): 3.21 vs 2.69
- final_third_share_pct (fit target, not a test): 35.4
- matches_simulated: 380
- test_matches_real: 1190

Data: openfootball/football.json (CC0) results; premierleague.com published averages (see targets.json).