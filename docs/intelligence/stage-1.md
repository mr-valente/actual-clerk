# Stage 1: rules live in Clerk

Checkpoint for the first stage of [the plan](plan.md). Clerk now holds
payee-to-category rules of its own, applies them ahead of everything else,
proposes new ones from evidence, and shows all of it on a new page. Nothing
in Actual's rule table is read or changed yet; that is Stage 2.

## What changed

**The resolver** (`domain/intelligence.py`). One pure function answers "where
does this merchant belong" from what Clerk already knows, in a fixed order:
alias canonicalization, a rule (account-scoped first, then general; exact
before family match), learned evidence meeting the thresholds, a single
exact sighting as a review-only suggestion. It returns `None` when it has
nothing, and the caller decides whether to ask the model. The filing cascade
and anticipated charges both use it, so a rule reaches a phone notification
the same moment it reaches a bank row.

**Rules** (`merchant_rules`). A rule is `merchant_key → category` with an
optional `account_id` scope and a `match` mode (`exact` or `family`, the
latter also claiming keys that add a store or city to the rule's own).
Declaring a rule for a merchant that already has one changes that rule
rather than making a second. Status is `active`, `paused`, or `retired`; a
retired rule keeps its row so it can be reinstated. Each rule counts how
many transactions it has filed and when it last did.

A rule applies **regardless of `apply_mode`**: that setting governs what
Clerk may do with learned evidence, and a rule is precisely what the user
has taken out of Clerk's judgment. A rule whose category is no longer in
the budget is skipped, not guessed around; the page marks it as needing a
category.

**Proposals** (`proposals`). Where Clerk used to offer to write a native
Actual rule, it now records a proposal of kind `rule` with the evidence
(consistent filings, the descriptors seen) and waits. Accepting declares
the rule with `source = proposal`; declining closes the question for that
merchant; declaring a rule by hand withdraws any open proposal for it. The
table is keyed on kind and merchant so a question is asked once.

**Decisions** gain `source = rule`. A transaction a rule filed is recorded
like any other decision, with the rule in its rationale, but is *not* fed
back into merchant memory: the rule is the user's word already. It counts
against the rule instead.

**The API.** `GET /api/intelligence` (rules, proposals, aliases, counts,
categories, accounts); `POST /api/intelligence/rules` by merchant text or
key; `PATCH` to change category, status, or match; `DELETE` to retire;
`POST /api/intelligence/proposals/{id}/resolve` with accept or decline;
`GET /api/intelligence/merchant?text=` to preview the key a name becomes.
Review resolution (single and bulk) takes `always: true`, which applies the
category and declares the rule in one write. The old `/api/rules` endpoints
that wrote Actual rules are gone; the worker method survives for the
restore path in Stage 2.

**The page.** *Intelligence* sits between Review and Connections. Three
figures (rules, proposals, merchants learned), the proposals with their
evidence and two buttons, and the rules: an add form that previews the key
as the merchant name is typed, a filter, per-rule category change, pause,
resume, retire, and a switch to show retired rules. Review rows gain
*Always* beside *Apply*; the decision drawer gains *Apply and always* on an
open review and *Make it a rule* on an applied one, and explains a rule
application. The sidebar badge on Review counts transactions only; the one
on Intelligence counts open proposals. Anticipated charges show "by a rule
you set" when a rule categorized them.

**Settings.** `rule_promotion_enabled` and `rule_promote_after` keep their
names (stored settings and environment variables refer to them) and are
relabelled: they now govern proposing Clerk rules.

## What was verified

- The Python suite passes (`uv run --extra dev pytest`), with new tests for
  the resolver (`tests/test_intelligence.py`), the rule and proposal tables,
  the cascade's rule layer (applies in review mode, beats contradicting
  history, skips a missing category, follows an alias), the job runner
  (counts rule applications, learns nothing from them, proposes instead of
  suggesting Actual rules), the API, and anticipated charges.
- Against a local Clerk with a seeded snapshot and no Actual: the page
  renders with rules, a proposal, and the add form; creating a family rule
  from `TST* Blue Bottle Coffee 4471` yields the key `blue bottle coffee`;
  accepting the proposal declares its rule and clears the badge; the key
  preview answers as the name is typed.

## Not in this stage

- Reading, retiring, or restoring Actual's own rules (Stage 2).
- Aliases from Actual's payee catalogue and the Merchants section (Stage 3).
- Corrections observed in Actual, disputes, and category repair (Stage 4).
- Any new question to the model (Stage 5).
