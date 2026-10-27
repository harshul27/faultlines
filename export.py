"""Write the static replay/analysis data the web page reads: python export.py -> web/data/*.json"""
import json
from pathlib import Path

import sources
from feed import DEMO_SEEDS, timeline
from match import DEMO_TEAMS, simulate, squad

OUT = Path(__file__).parent / "web" / "data"

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for seed in DEMO_SEEDS:
        tl = timeline(seed)
        (OUT / f"match-{seed}.json").write_text(json.dumps(tl, separators=(",", ":")))
        last = tl["snapshots"][-1]["score"]
        index.append(dict(seed=seed, file=f"data/match-{seed}.json", home=tl["meta"]["teams"]["home"]["name"],
                          away=tl["meta"]["teams"]["away"]["name"], final=f"{last['home']}-{last['away']}"))
        print("wrote", seed)
    (OUT / "index.json").write_text(json.dumps(index, indent=1))
    # Templates for "bring your own match": one synthetic match in each supported format
    ev, teams = list(simulate(7)), {s: {"name": DEMO_TEAMS[s]["name"], "roles": squad(s)} for s in ("home", "away")}
    (OUT / "sample-match.json").write_text(json.dumps(sources.to_native(ev, teams), separators=(",", ":")))
    (OUT / "sample-match.csv").write_text(sources.to_csv(ev, teams))
    import noise  # the same match as a provider might send it: shows the cleaning pipeline at work
    (OUT / "sample-messy.csv").write_text(noise.noisy_csv(ev, teams, seed=7)[0], encoding="utf-8")
    print("wrote samples")
