"""Grounding checks shared by the narrator and the agents.

Model text is checked in English against the data it was given; only then is it translated, and the translation
must carry exactly the same numbers. Checks are deterministic: no second model is trusted to judge the first.
"""
import json
import re
from collections import Counter

NUM = re.compile(r"[0-9]+(?:\.[0-9]+)?")
UP = {"up", "rose", "rise", "rising", "increase", "increased", "increasing", "grew", "growth", "higher", "more",
      "gained", "gain", "improved", "improving", "climbed", "jumped"}
DOWN = {"down", "fell", "fall", "falling", "drop", "dropped", "decrease", "decreased", "decreasing", "lower", "less",
        "fewer", "declined", "decline", "lost", "shrank", "slipped"}
TREND = re.compile(r"\b(up|down)\s+([0-9]+(?:\.[0-9]+)?)%")


def numbers(text):
    return NUM.findall(text)


def number_issues(text, *sources):
    """Numbers in the text that appear in none of the sources (tool outputs, facts, the question)."""
    allowed = set(numbers(" ".join(json.dumps(s) if not isinstance(s, str) else s for s in sources)))
    return [f"number {n} is not in the data" for n in numbers(text) if n not in allowed]


def direction_issues(text, *sources):
    """A trend reported as 'down 21%' must not be described as rising (and vice versa)."""
    trends = {}
    for s in sources:
        for d, n in TREND.findall(json.dumps(s) if not isinstance(s, str) else s):
            trends.setdefault(n, set()).add(d)
    issues = []
    words = re.findall(r"[A-Za-z]+|[0-9]+(?:\.[0-9]+)?%?", text.lower())
    for i, w in enumerate(words):
        n = w.rstrip("%")
        if not w.endswith("%") or n not in trends or len(trends[n]) != 1:
            continue  # only check numbers whose direction is unambiguous in the data
        context = set(words[max(0, i - 8):i + 3])
        wrong = DOWN if "up" in trends[n] else UP
        if context & wrong and not context & (UP if wrong is DOWN else DOWN):
            issues.append(f"{n}% is {'up' if 'up' in trends[n] else 'down'} in the data but described as the opposite")
    return issues


def check(text, *sources):
    return number_issues(text, *sources) + direction_issues(text, *sources)


def translate(text, language):
    """Translate verified English text; returns (text, ok). ok is False if any number changed."""
    if language == "English":
        return text, True
    import ai
    r = ai.client().chat.completions.create(
        model=ai.deployment("chat"), temperature=0,
        messages=[{"role": "system", "content": f"Translate into {language} for a football broadcast. Keep every "
                                                "number, percentage and name exactly as written, digits 0-9. "
                                                "Output only the translation."},
                  {"role": "user", "content": text}])
    out = r.choices[0].message.content.strip()
    return (out, True) if Counter(numbers(out)) == Counter(numbers(text)) else (text, False)
