---
title: "TypeSafe / Jev documentation, as read on 2026-10-13"
source: https://docs.typesafe.ai/llms.txt and linked pages
authors_org: TypeSafe
published: live
read: 2026-10-13
type: docs
code: n/a (SDK: typesafe_sdk)
relevance: high (the decision model we use every action)
---

# TypeSafe docs

The live docs are the source of truth; re-read before changing the Jev questions. Pages read:
concepts/system-one, concepts/how-to-build-with-system-one, concepts/state, concepts/use-case-map,
primitives, primitives/choice, primitives/noul, primitives/score, primitives/advanced, confidence,
patterns/fan-out, patterns/composite-scoring, patterns/confidence-routing, patterns/intent-routing,
cookbooks/rerank_typesafe, cookbooks/function_calling, cookbooks/parallel_questions,
cookbooks/skill_suggestion, cookbooks/basic_memory, api, models, model-jaggedness/jev-1.13.

## Limits and cost (models page, as quoted by the reader)

- 64k tokens per request for state plus all questions; 32k for state plus the single longest
  question.
- A Choice accepts up to 255 options; Score has 2 to 10 levels; no documented cap on questions
  per request beyond tokens.
- Pricing per input token only ($42 per billion tokens); output tokens free.
- Rate limits 100K tokens per second, 80 requests per second; "can change without notice."
- Latency: "most queries complete in about 100 ms"; 0.16 to 0.31 s for a 182-option Choice.

## Guidance that bears on our four questions

- "Keep question IDs descriptive for your code. IDs are not sent to the model"; write the complete
  question in `instructions`.
- "Define Noul conditions precisely"; vague terms "make the probability hard to interpret."
- "Multi-hop reasoning costs accuracy." (`completable` asks for a plan over the menu.)
- "Keep content and facts in the state, and put the judgments in the questions." When
  instructions and criteria ask for different things the model "might get confused."
- "Include an 'other' or 'none of the above' option when the list might not cover every input."
- jev-1.13 "leans toward the option that comes first." Shuffle option order.
- "Jev will perform better on semantic questions than mathematical ones"; "Counting is unreliable";
  "Accuracy falls as the state grows with content unrelated to the decision."
- Score: "Describe situations, not degrees"; each level judged on its own; "weak in numerical
  calibration", so use the level, not the fractional mean.
- `choice` is argmax; no documented temperature. The documented lever is confidence routing (floor
  0.5 to 0.6; above 0.85 for risky acts) plus a none option.
- "Send every question that uses the same state in one request"; "adding more questions usually
  has little effect on response time." parallel_questions cookbook: 13 questions in one call was
  12.2x cheaper and 10x faster than 13 calls.
- basic_memory cookbook: prior outcomes as a state field; "memory only grows"; prune with a
  relevance question.

## What applies to this project

- **Add `none_of_these` to the action Choice** and fire the builder when it wins at a checkpoint,
  instead of the `completable` Noul over the proven menu. Maps to: `agentLoop.js:356-394`.
  Addresses: F4 (R3).
- **Make `progress` a Score with concrete situations, or drop it** in favour of the code's own
  `madeProgress`. Maps to: `agentLoop.js:382-388, 708-718` (R5).
- **Pre-digest numbers**: near / far / out of reach, blocked / open, visited / new, instead of
  dx,dy,dz offsets, 30-block rays and counts. Maps to: `observe.js`, `baseActions.js` menu
  text (R5, R6).
- **Shuffle menu order per request.** Maps to: `buildMenu` in `agentLoop.js:307-315`.
- **Use free fan-out**: per-option "would this change the world now" Nouls, per-direction
  exploration Scores, all in the same request (R5).
- **Measure latency before tuning the 500 ms floor** (`agentLoop.js:16`); the docs say ~100 ms
  typical, so the floor caps throughput (R2 first).
- **The menu lines put facts into criteria.** Consider moving live facts into a state field keyed
  by option id and keeping criteria as plain action meanings.

## Cookbooks that resemble our loop

None exactly. function_calling (Choice over functions plus per-argument Nouls in one request);
skill_suggestion (182-option Choice plus gate Nouls, top-3 re-rank, 0.30 abstain gate);
confidence-routing (Choice with confidence floors); basic_memory (outcomes as state).

## Not found

A dedicated limits page, a "changing state" guide, a maximum question count.
