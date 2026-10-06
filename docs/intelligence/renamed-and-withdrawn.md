# When the bank renames or withdraws a charge

## What happened

On 2026-10-05 the rent ($2,550 to the landlord, De Mattheis) went wrong in
three independent ways:

| Time (EDT) | Event |
| --- | --- |
| Oct 4, 9:02pm | Plaid delivered the pending charge as `Demattheisinv`. Memory pooled it with eight SimpleFIN payments (`Demattheisinv Web Co Name Nicholas Valente`) and filed it as Housing. |
| Oct 5, 7:06am | FFFCU withdrew the pending charge without posting it. Clerk deleted the row, and with it the filing. |
| Oct 5, 9:36pm | The local model's machine was powered off. |
| Oct 5, 9:46pm | The posted charge arrived as `Pl*Demattheisinv`. Its key, `pl demattheisinv`, matched nothing: the processor's tag comes first, and memory only pooled names that start the same way. The model answered 502 four times in 18 seconds and was never asked again. The review had no suggestion, and until it was answered the rent counted as uncategorized, free-money spending. |

Each failure has a general fix rather than a special case for this landlord.

## 1. A withdrawn pending charge is held

See [anticipated charges](../anticipated-charges.md#pending-charges-the-bank-withdraws).
The deleted row becomes an anticipated charge of origin `pending` that keeps
counting under its category, and the posted row inherits that category when
it arrives, whatever the bank calls it then. Setting:
`plaid_hold_withdrawn_days` (default 5, 0 = off).

## 2. Names with the same words

`merchants.keys_share_words`: two keys name one merchant when every
identifying word of one appears in the other, in any order. Identifying words
are longer than two letters and are not payment-rail words, so `pl`, `sq`,
`web`, and `ach` are decoration. A lone short word never claims anything, the
same guard `keys_related` has.

| Key | Key | Same? |
| --- | --- | --- |
| `pl demattheisinv` | `demattheisinv name nicholas` | yes |
| `prime video` | `amazon prime video` | yes |
| `kam man food` | `krauszers food` | no: each has words of its own |
| `uber` | `uber eats` | no: a lone short word |

The test is looser than `keys_related` and is used where a wrong match costs
little:

- **Resolver step 5** (`domain/intelligence.resolve`). For a name memory has
  never seen under that key or a related one, the names with the same words
  are pooled. If they all agree on one category, it is a **review-only
  suggestion**, never filed automatically (`MerchantMemory.sibling_suggestion`).
  It answers before the model is asked, as a lone exact sighting already
  does, so a renamed merchant gets its old category even with the model down.
  Approving it teaches memory the new name.
- **Matching anticipated charges.** It only breaks ties among rows that
  already agree on account, amount, and date.

It deliberately does not change automatic pooling, rule families, or which
payee name a masked row is given.

## 3. Review spending counts under its suggestion, in the report only

An open review (a bank row or a phone charge) whose suggestion Clerk would
stand behind (from memory or a rule, or a model answer at or above
`ai_min_confidence`) is counted under that category by the overview, the
morning report, yesterday's grade, and Reports, until the person answers.
Nothing is written to Actual. Spending suggested as an income category stays
uncategorized. The report shows the amount as `provisional_cents` /
`provisional_count`; the digest says "Including $X waiting in Review,
counted where Clerk suggests".

## 4. A review the model never answered is asked again

A review left without a suggestion because the model was down, refused, or
not reached is marked `model_unasked`. Every filing run asks again for those
rows (within the filing window, and only when a model is configured), and a
phone charge's review the same way. No second alert is sent. A review the
model did answer, even with "none of these", is not asked again.

## Verified

- Re-running the categorizer on the live state as of 9:47pm with the model
  up returned Housing at 1.0. With this change the resolver suggests Housing
  from the eight SimpleFIN payments before the model is needed
  (`test_a_renamed_merchant_gets_its_old_name_s_category_as_a_suggestion`).
- The night's sequence end to end, as tests: hold on deletion, the posted
  row named by Plaid or matched by amount, the carried decision written by
  the next filing run, memory learning `pl demattheisinv`
  (`tests/test_withdrawn_pending.py`, `tests/test_processing.py`).
- The phone-only `anticipated_charges` table is rebuilt in place on first
  start, keeping every row and index
  (`test_a_phone_only_ledger_is_rebuilt_to_hold_bank_charges_too`).
