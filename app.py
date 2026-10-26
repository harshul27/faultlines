"""Fault Lines: live pass-network fragility overlay. Run: streamlit run app.py"""
import time

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from mplsoccer import Pitch

from explain import explain, facts
from match import TEAMS, simulate
from network import connectivity_timeline, score_at, team_view
from stream import LiveEngine

st.set_page_config(page_title="Fault Lines", layout="wide")


@st.cache_data
def load(seed):
    return list(simulate(seed))


with st.sidebar:
    st.header("Your view")
    seed = st.number_input("Synthetic match seed", 1, 9999, 23)  # demo match: clearest storyline
    team = st.radio("Follow", ["home", "away"], format_func=lambda t: TEAMS[t]["name"])
    favourite = st.selectbox("Favourite player", ["(none)"] + TEAMS[team]["players"])
    mode = st.radio("Mode", ["Casual", "Analyst"], horizontal=True)
    language = st.selectbox("Language", ["English", "Spanish", "French", "Portuguese", "Hindi", "Arabic", "Japanese"])
    window = st.slider("Rolling window (min)", 5, 30, 15)
    live = st.toggle("Live playback")

events = load(seed)
other = "away" if team == "home" else "home"
if "minute" not in st.session_state:
    st.session_state.minute = 30
minute = st.slider("Match minute", 5, 95, key="minute")

# Live ingest: feed the engine every event up to the current clock, exactly as a live feed would arrive
key = (seed, window)
if st.session_state.get("engine_key") != key or st.session_state.get("engine_t", 0) > minute * 60:
    st.session_state.engine, st.session_state.engine_key, st.session_state.engine_t = LiveEngine(window), key, 0
    st.session_state.engine_i, st.session_state.latency = 0, []
eng = st.session_state.engine
with st.spinner("Ingesting events..."):
    while st.session_state.engine_i < len(events) and events[st.session_state.engine_i]["t"] < minute * 60:
        st.session_state.latency.append(eng.ingest(events[st.session_state.engine_i])[1])
        st.session_state.engine_i += 1
st.session_state.engine_t = minute * 60

view, opp = team_view(events, team, minute, window), team_view(events, other, minute, window)
score = score_at(events, minute * 60)
spof = view["spof"]

st.title(f"{TEAMS['home']['name']} {score['home']} - {score['away']} {TEAMS['away']['name']}  ·  {minute}'")

left, right = st.columns([3, 2])
with left:
    pitch = Pitch(pitch_type="opta", pitch_color="#1b4d2b", line_color="#dfe8e1")
    fig, ax = pitch.draw(figsize=(10, 6.5))
    pos = {p["name"]: p for p in view["players"]}
    top = max((w for *_, w in view["edges"]), default=1)
    for a, b, w in view["edges"]:
        pitch.lines(pos[a]["x"], pos[a]["y"], pos[b]["x"], pos[b]["y"], lw=1 + 7 * w / top,
                    color="white", alpha=0.25 + 0.6 * w / top, zorder=1, ax=ax)
    df = pd.DataFrame(view["players"])
    pitch.scatter(df.x, df.y, s=200 + 25 * df.passes, c=df.fragility, cmap="YlOrRd", vmin=0, vmax=0.35,
                  edgecolors="black", linewidth=1, zorder=2, ax=ax)
    pitch.scatter(spof["x"], spof["y"], s=600 + 25 * spof["passes"], facecolors="none", edgecolors="#ff2d2d",
                  linewidth=3, zorder=3, ax=ax)
    for p in view["players"]:
        bold = p["name"] in (spof["name"], favourite)
        pitch.annotate(p["name"].split()[-1], (p["x"], p["y"] - 5), ax=ax, ha="center", va="top", fontsize=9,
                       color="#ffdf5d" if p["name"] == favourite else "white", weight="bold" if bold else None)
    ax.set_title(f"{view['name']} passing network, last {window} min  (attacking →)", color="white")
    fig.set_facecolor("#1b4d2b")
    st.pyplot(fig)
    plt.close(fig)

with right:
    f = facts(view, opp, score, None if favourite == "(none)" else favourite)
    text, source = explain(f, mode, language)
    st.subheader(f"Fault line: {spof['name']}")
    st.metric("Connectivity lost without them", f["connectivity_lost_without_key_player"],
              delta=f"{round((spof['fragility'] - spof['fragility_prev']) * 100)} pts vs prev window"
              if minute >= 2 * window else None, delta_color="off")  # previous window must be full to compare
    st.info(text)
    st.caption(f"Source: {source}. Every number is computed from the event stream.")

    tl = pd.DataFrame({TEAMS[t]["name"]: dict(connectivity_timeline(events, t, minute, window)) for t in TEAMS})
    st.caption("Passing spread over time (higher = ball shared more evenly)")
    st.line_chart(tl, height=200)

    st.caption("Live cues (newest first)")
    for c in reversed(eng.cues[-5:]):
        st.markdown(f"`{c['match_clock']}` **{c['cue'].replace('_', ' ')}**: {c['headline']}")
    lat = sorted(st.session_state.latency)
    if lat:
        st.caption(f"Ingest latency p95: {lat[int(0.95 * (len(lat) - 1))]:.0f} ms over {len(lat)} events")
    with st.expander("Latest broadcast overlay cue (machine-readable)"):
        st.json(eng.cues[-1] if eng.cues else {})

st.dataframe(df[["name", "role", "passes", "fragility", "fragility_prev", "betweenness", "xpa"]],
             hide_index=True, use_container_width=True)

if live and minute < 95:
    time.sleep(1.5)
    st.session_state.minute = minute + 1
    st.rerun()
