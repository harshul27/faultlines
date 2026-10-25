"""Grounded explanations: the model may only restate computed facts; any unsupported number is rejected.

Uses a Microsoft Foundry / Azure OpenAI deployment when AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT are set
(auth via your `az login`, no keys). Otherwise falls back to a deterministic template.
"""
import json
import os
import re


def pct(v):
    return f"{round(v * 100)}%"


def facts(view, opponent, score, favourite=None):
    spof = view["spof"]
    change = (view["efficiency"] - view["efficiency_prev"]) / view["efficiency_prev"] if view["efficiency_prev"] else 0
    f = {
        "minute": view["minute"], "team": view["name"], "opponent": opponent["name"],
        "score": f"{score[view['team']]}-{score[opponent['team']]}",
        "window_minutes": view["window"],
        "team_connectivity_change_vs_previous_window": f"{round(change * 100):+d}%",
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
                 connectivity_lost_without_favourite=pct(fav["fragility"]), favourite_clutch_points=fav["clutch"])
    return f


def template(f, mode):
    if mode == "Analyst":
        s = (f"{f['minute']}' | {f['team']} {f['score']} {f['opponent']}. Single point of failure: {f['key_player']} "
             f"({f['key_player_role']}): removing them costs {f['connectivity_lost_without_key_player']} of passing "
             f"connectivity (prev window {f['same_player_previous_window']}). Team connectivity "
             f"{f['team_connectivity_change_vs_previous_window']} vs the previous {f['window_minutes']} min; "
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
              f"clutch points {f['favourite_clutch_points']}.")
    return s


def grounded(text, f):
    """Fact-check: every number in the text must appear in the facts."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(f)))
    return all(n in allowed for n in re.findall(r"\d+(?:\.\d+)?", text))


_client = None


def _llm():
    global _client
    if _client is None:
        from azure.identity import AzureCliCredential, get_bearer_token_provider
        from openai import AzureOpenAI
        _client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"], api_version="2024-10-21",
            azure_ad_token_provider=get_bearer_token_provider(
                AzureCliCredential(), "https://cognitiveservices.azure.com/.default"))
    return _client


def explain(f, mode="Casual", language="English"):
    """Returns (text, source) where source is 'ai', 'template', or 'template (ai failed fact-check)'."""
    if not (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_DEPLOYMENT")):
        return template(f, mode), "template"
    style = ("a concise tactical analyst for a studio graphic" if mode == "Analyst"
             else "a friendly broadcaster explaining to a casual fan, no jargon")
    msg = [
        {"role": "system", "content": (
            f"You are {style}. Write 2-3 sentences in {language} explaining why the key player matters to their "
            "team's passing network right now, and whether the team is still moving the ball forward. Use ONLY the facts given. Copy numbers exactly as written; "
            "do not compute or invent any other number.")},
        {"role": "user", "content": json.dumps(f, ensure_ascii=False)},
    ]
    try:
        r = _llm().chat.completions.create(model=os.environ["AZURE_OPENAI_DEPLOYMENT"], messages=msg, temperature=0.4)
        text = r.choices[0].message.content.strip()
    except Exception as e:  # never let a model outage break the live overlay
        return template(f, mode) + f" [AI unavailable: {type(e).__name__}]", "template"
    return (text, "ai") if grounded(text, f) else (template(f, mode), "template (ai failed fact-check)")
