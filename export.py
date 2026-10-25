"""Write the static replay/analysis data the web page reads: python export.py -> web/data/*.json"""
import json
from pathlib import Path

from feed import DEMO_SEEDS, timeline

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
