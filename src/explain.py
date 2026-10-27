"""Grounded explanations: the model may only restate computed facts; unsupported numbers or flipped trends are rejected.

Uses Microsoft Foundry when AZURE_OPENAI_ENDPOINT is set (keyless, see ai.py); otherwise a deterministic template.
"""
import json

import ai
import grounding


def pct(v):
    return f"{round(v * 100)}%"


def facts(view, opponent, score, favourite=None):
    spof = view["spof"]
    change = (view["efficiency"] - view["efficiency_prev"]) / view["efficiency_prev"] if view["efficiency_prev"] else 0
    f = {
        "minute": view["minute"], "team": view["name"], "opponent": opponent["name"],
        "score": f"{score[view['team']]}-{score[opponent['team']]}",
        "window_minutes": view["window"],
        # Direction is spelled out so neither the model nor the fact-check has to infer it from a sign
        "passing_spread_trend": f"{'up' if change > 0 else 'down' if change < 0 else 'flat'} {abs(round(change * 100))}% vs previous window",
        "key_player": spof["name"], "key_player_role": spof["role"],
        "connectivity_lost_without_key_player": pct(spof["fragility"]),
        "same_player_previous_window": pct(spof["fragility_prev"]),
        "progressive_passes_in_window": view["progressive"],
        "progressive_passes_previous_window": view["progressive_prev"],
        "pass_completion_in_window": pct(view["completion"]),
        "opponent_key_player": opponent["spof"]["name"],
        "connectivity_opponent_loses_without_their_key_player": pct(opponent["spof"]["fragility"]),
    }
    fav = next((p for p in view["players"] if p["name"] == favourite), None)
    if fav:
        f.update(favourite_player=fav["name"], favourite_role=fav["role"], favourite_passes_in_window=fav["passes"],
                 connectivity_lost_without_favourite=pct(fav["fragility"]), favourite_expected_points_added=f"{fav['xpa']:+.2f}")
    return f


def template(f, mode):
    if mode == "Analyst":
        s = (f"{f['minute']}' | {f['team']} {f['score']} {f['opponent']}. Single point of failure: {f['key_player']} "
             f"({f['key_player_role']}): removing them costs {f['connectivity_lost_without_key_player']} of passing "
             f"connectivity (prev window {f['same_player_previous_window']}). Passing spread "
             f"{f['passing_spread_trend']}; "
             f"progressive passes {f['progressive_passes_in_window']} (prev {f['progressive_passes_previous_window']}), "
             f"completion {f['pass_completion_in_window']}. "
             f"{f['opponent']}'s most critical link, {f['opponent_key_player']}, carries "
             f"{f['connectivity_opponent_loses_without_their_key_player']}.")
    else:
        s = (f"Right now {f['team']} run through {f['key_player']}. Take them out and the team loses "
             f"{f['connectivity_lost_without_key_player']} of its passing links. "
             f"In the last {f['window_minutes']} minutes they moved the ball forward "
             f"{f['progressive_passes_in_window']} times (previously {f['progressive_passes_previous_window']}).")
    if "favourite_player" in f:
        s += (f" Your player {f['favourite_player']}: {f['favourite_passes_in_window']} passes in the last "
              f"{f['window_minutes']} min, holds {f['connectivity_lost_without_favourite']} of the network, "
              f"expected points added {f['favourite_expected_points_added']}.")
    return s


def grounded(text, f):
    """Fact-check: every number must appear in the facts and every trend must keep its direction."""
    return not grounding.check(text, f)


def explain(f, mode="Casual", language="English"):
    """Returns (text, source). Written in English, fact-checked, then translated with numbers preserved.
    source: 'ai', 'ai (shown in English: translation altered numbers)', or a 'template ...' fallback."""
    if not ai.enabled():
        return template(f, mode), "template"
    style = ("a concise tactical analyst for a studio graphic" if mode == "Analyst"
             else "a friendly broadcaster explaining to a casual fan, no jargon")
    msg = [
        {"role": "system", "content": (
            f"You are {style}. Write 2-3 sentences in English explaining why the key player matters to their "
            "team's passing network right now, and whether the team is still moving the ball forward. "
            "Use ONLY the facts given. Copy numbers and trend directions exactly as written; do not compute, "
            "round or invent any other number.")},
        {"role": "user", "content": json.dumps(f, ensure_ascii=False)},
    ]
    try:
        r = ai.client().chat.completions.create(model=ai.deployment("chat"), messages=msg, temperature=0.4)
        text = r.choices[0].message.content.strip()
        if not grounded(text, f):
            return template(f, mode), "template (ai failed fact-check)"
        out, ok = grounding.translate(text, language)
        return out, "ai" if ok else "ai (shown in English: translation altered numbers)"
    except Exception as e:  # never let a model outage break the live overlay
        return template(f, mode), f"template (AI unavailable: {type(e).__name__})"
