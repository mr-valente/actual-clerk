# Plaid migration

Working notes for moving Actual Clerk, and the Actual budget it looks after,
from SimpleFIN to Plaid while keeping the road back open.

| Document | What it holds |
| --- | --- |
| [plan.md](plan.md) | Findings from the Actual API and Plaid, the design, and the five stages |
| [lab.md](lab.md) | The throwaway Actual + Clerk containers used to test against a copy of the real budget |
| [stage-1.md](stage-1.md) | Provider-neutral foundations: what changed, what was verified live |
| [stage-2.md](stage-2.md) | Plaid client, Link, Items, account mapping, and Plaid in the health check |
| [stage-3.md](stage-3.md) | The sync engine: refresh, cursor stream, adoption, import, opening balances |

Stages are implemented one at a time on the `feature/plaid` branch; each ends
with a checkpoint before the next begins.
