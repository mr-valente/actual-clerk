# Intelligence

Working notes for making Clerk the home of everything a transaction means:
the simple payee-to-category rules Actual used to hold, the merchant
evidence Clerk learns, the aliases between a merchant's names, and what the
user does in Actual afterwards.

| Document | What it holds |
| --- | --- |
| [plan.md](plan.md) | What the budget and the Actual API hold, the model of memory, the resolver, the data, the page, the takeover from Actual, and the five stages |
| [stage-1.md](stage-1.md) | Rules live in Clerk: the resolver, proposals, the Intelligence page, and what was verified |
| [stage-2.md](stage-2.md) | Taking the simple rules over from Actual: classification, replay, import, retire, restore, and the lab run |
| [stage-3.md](stage-3.md) | One identity: aliases from every source, the payee catalogue read as evidence, the Merchants section, teaching as a rule |
| [stage-4.md](stage-4.md) | Learning from Actual: corrections, disputes, rule-change and retire proposals, category repair, the digest line |

Stages are implemented one at a time on the `feature/plaid` branch; each ends
with a checkpoint before the next begins.
