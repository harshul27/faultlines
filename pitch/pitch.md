# Fault Lines

**Every team has a fault line.**

Live demo: https://harshul27.github.io/faultlines/ · Code: https://github.com/harshul27/faultlines

---

## The question

Every match raises the same question for coaches, analysts, broadcasters and fans: *who does this team depend on, and what breaks without them?*

Match data can show possession, pass counts and heatmaps. None of it answers that question. Counting a player's actions shows how busy they are. It can't tell you how much of the team stops working when that player is marked, tired or substituted.

## The idea

Every completed pass links two teammates, and over the last 15 minutes those links form a network. For each player, Fault Lines removes them from the network and measures how much of the *other* players' connectivity is lost. The player whose removal does the most damage is the team's **fault line**.

It updates with every event, so the fault line moves as the match moves: a press that cuts off the pivot, a substitution, a tired winger.

## The answer: one insight, four people who need it

**The broadcaster** needs graphics that fire on their own, on time. Events stream in over Server-Sent Events, and Fault Lines emits timed cues for goals, fault-line alerts and a team's play narrowing. Each cue carries a match clock, priority and display time, and is validated against a JSON Schema so a graphics system can take it as it is.

**The fan** wants the story in their language, about their player. They can follow either club, pick a favourite player, choose casual or analyst mode, and read it in one of 7 languages. Every explanation is written from computed facts, and a fact-check rejects any number the data doesn't support.

**The coach** has a plan and needs to know whether it's working. They paste their tactics in their own words, and every player is rated against *that* plan: plan fit, form, load versus what the plan intends, and flags for who is overloaded, fading or off plan. During the match the coach can ask:
- "Who should I take off?"
- "Is our press working?"
- "Where should we attack?"
- "What happens if our key player is marked out?"

The analyst agents answer from the match itself:
- how much of the passing runs through a player
- who would become the next fault line
- which teammates would be hit hardest
- the win probability from here
- how each part of the plan is going

Every number is checked before it is shown. Off-topic questions are turned away before a large model is ever called.

**The data team** gets messy files and still has to trust the result. They can upload almost any CSV layout, JSON or StatsBomb events, or push a live feed. The pipeline maps the columns, merges name spellings, removes duplicates, fixes clocks and attack directions, quarantines broken rows and flags everything it inferred. The result is clean data with a quality score, a downloadable data package and an audit trail. On the project's deliberately messy test file, 1,011 rows go in and 969 clean events come out, with 99/100 quality.

## What sets it apart

1. **It measures dependence.** A busy player and an essential player are different people. Fragility measures what the team loses without someone, where usual tools rank touches, passes or xG.
2. **The coach's plan is the yardstick.** Each player is judged on the metrics that coach's plan weights, not a generic rating.
3. **AI that can't invent a number.** A deterministic verifier checks every figure against the tool outputs, with one self-correcting rewrite. If a number still can't be traced, the agents fall back to the raw tool data.
4. **A guard for the bench.** The agents can't recommend taking off a key player without checking what the team would lose, and never suggest taking off the goalkeeper. There were 0 violations in testing.
5. **One truth, every audience.** Broadcaster, fan, analyst and coach all see the same numbers, each written for them.
6. **Messy data in, audit trail out.** Every fix is listed, and every inferred value is flagged and carried into the analysis notes.

## Proof

The generator behind the demo matches is calibrated on real public-domain results and checked against seasons it never saw, and pass bars were fixed before the first test run.

- **6,130** real results: trained on 4,940 and tested on 1,190 later ones.
- **17/17** held-out realism checks pass.
- **96%** of 26 coach questions answered with the right tools.
- **4.8/5** answer quality across those questions.
- **0** pieces of unchecked substitution advice.
- **100%** of events recovered from deliberately noisy files.

## How it's built

The analyst is a five-step agent pipeline on the **Microsoft Agent Framework**:

1. A **Phi-4-mini-instruct** router classifies each question.
2. **text-embedding-3-small** memory keeps only the earlier turns that matter.
3. A **gpt-4.1-mini** analyst works with 10 match and game-plan tools.
4. A deterministic verifier checks every number.
5. A number-preserving translator handles the 7 languages.

All models run on **Microsoft Foundry**. The API and live match stream run on **Azure Container Apps**, reaching Foundry through **managed identity / Microsoft Entra ID**, so there are no API keys anywhere. Images live in **Azure Container Registry**, and **GitHub Actions** tests, builds and deploys on every push.

## Who would use it tomorrow

- **Broadcasters:** insight cues generated with the match, instead of graphics built by hand.
- **Coaching staff:** an assistant that knows the plan and checks every substitution.
- **Scouts and opposition analysts:** an opponent's fault line, and what marking that player does to them.
- **Fan and second-screen apps:** the story for each fan, in their language, around their player.

Next steps:
- Weight passes by threat (xT, VAEP), so a fault line means lost danger rather than lost volume.
- Re-simulate matches instead of deleting a node.
- Add confidence intervals.
- Validate against real substitutions and red cards with a club or data partner.

## In one line

Who holds the team together, and what happens without them? Fault Lines answers that live, for every match: for the broadcaster's graphics, the fan's language, the coach's plan and the data team's files, with every number checked.

---

### Short version (for a form field)

Every team has a fault line: the one player its passing can't survive without. Fault Lines finds that player live, event by event, by removing each player from the passing network and measuring what the rest of the team loses. The same insight reaches four audiences:
- **Broadcasters** get timed, schema-checked overlay cues.
- **Fans** get explanations in 7 languages, around their favourite player.
- **Coaches** get every player rated against their own game plan, plus answers to "what if?" questions mid-match.
- **Data teams** get a pipeline that turns messy files into audited, clean data.

The coach's questions are answered by agents on the Microsoft Agent Framework: a Phi-4-mini router, embedding memory and a gpt-4.1-mini analyst with 10 tools, all on Microsoft Foundry. A deterministic verifier checks every number before it is shown. It runs on Azure Container Apps with keyless Entra ID access, and it is calibrated and validated on 6,130 real match results.
