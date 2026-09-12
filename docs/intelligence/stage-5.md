# Stage 5: the model as consultant

Checkpoint for the last stage of [the plan](plan.md). The model is asked
two bounded questions and offered one more piece of context; it still
writes nothing, and every answer that would change what Clerk knows is a
proposal.

## What changed

**Rule hints in the category question.** When an unfamiliar merchant goes
to the model, the prompt now carries the user's rules for merchants with
similar names (`RuleBook.related`: related keys first, then by shared
tokens, six at most) under *Rules this person has set for similar
merchants*. A rule for `amazon` is a better guide to `amazon fresh` than
the category names are.

**The same-merchant question.** With `ai_alias_questions` on (off by
default, `CLERK_AI_ALIAS_QUESTIONS`), the merchant is then shown the
budget's known merchants (the closest dozen by name, from memory, rules,
and alias targets) and asked whether it is one of them under another name.
The answer is a number and a confidence; at 0.7 or better it becomes an
`alias` proposal on the Intelligence page, which the user accepts into an
alias or declines. An unsure or unusable answer is dropped without
counting as a model failure: the question is a courtesy, not part of
filing. It is one more model call per unfamiliar merchant, which is why it
is opt-in.

**Rule proposals from history.** *Propose rules from history* on the
Intelligence page runs a categorize job with `propose_rules`, which reads
the whole retained history and proposes a rule for every merchant filed
one way, every time, at least `rule_promote_after` times, that has no rule
yet. This needs no model. For a budget with years of history it can ask a
lot at once, so the proposals panel gains *Decline all*, and a declined
merchant is never asked about again.

**The API.** `POST /api/jobs` takes `propose_rules` on a categorize job;
`POST /api/intelligence/proposals/decline` closes every open proposal, or
those of one `kind`; accepting an `alias` proposal declares the alias
(refusing one that would chain).

## What was verified

- `uv run --extra dev pytest` passes, with tests for the hint section in
  the prompt, the question being asked only when switched on, a confident
  answer becoming a proposal and an unsure or malformed one being dropped
  silently, the closest-candidates helper, history candidates (unanimous,
  deep enough, no rule yet), the history run and its bulk decline, the
  model's alias answers reaching the proposals table, and the endpoints.

## What the model is never asked

Whether to make a rule, whether to change one, and where a merchant it
has never seen belongs when the user's history already says. Those stay
with the evidence and the user.
