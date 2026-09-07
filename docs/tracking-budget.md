# Setting up a Tracking Budget

This guide configures Actual's **Tracking Budget** so that Actual Clerk can report your free money: what is left each month once everything already promised is accounted for.

```
free money = expected income - committed spending
remaining  = free money - what you have spent since the 1st
```

Set up this way, Clerk's free money is identical to Actual's own **Projected Savings**, tracked against your real spending as the month goes on.

Allow about twenty minutes. Afterwards there is roughly one minute of upkeep a year.

> Prefer Actual's default envelope budgeting? See [Setting up an Envelope Budget](envelope-budget.md) instead.

## Before you start

- Clerk connected to your Actual server and showing your accounts.
- A few months of transaction history in Actual. Clerk uses it to find your recurring bills for you.

---

## Step 1. Switch Actual to tracking budgeting

1. In Actual, open **Settings** from the left sidebar.
2. Find the **Budgeting Method** section.
3. Click **Switch to tracking budgeting**.
4. Return to **Budget** in the sidebar.

The budget table now shows budgeted amounts beside actuals for both income and expenses, with pie charts tracking each.

> **This is reversible.** Actual stores the two budgeting methods in separate tables inside your budget file, so switching converts nothing and deletes nothing. Switch back and your envelope figures are exactly as you left them. To keep a backup anyway, use **Settings → Export**.

## Step 2. Create a category for every recurring bill

Each thing you pay on a schedule needs its own category: rent or mortgage, electricity, gas, water, internet, phone, insurance, car payment, childcare, debt payments, gym, and every subscription.

To add a category:

1. Hover over the category group it belongs in, such as **Bills**.
2. Click the **+** on the group's row, or open the group's three-dot menu and choose **Add category**.
3. Type the name and press Enter.

To add a group, scroll to the bottom of the category list and use the add-group control.

Group names do not matter to Clerk. What matters is which categories carry a budgeted amount.

## Step 3. Budget your expected income

1. On the **Budget** screen, find the **Income** group. Actual keeps exactly one.
2. Click the **Budgeted** cell beside your income category.
3. Enter your expected monthly income and press Enter.

If you are paid fortnightly, enter **two paychecks**, not 2.17. Three-paycheck months then arrive as a surplus rather than as money already spent.

Clerk reads this figure directly. No income needs entering in Clerk.

## Step 4. Budget each recurring bill

Click a category's **Budgeted** cell and enter its amount. The right amount depends on the kind of bill.

### Fixed bills

Rent, phone, insurance, subscriptions. Enter the exact amount.

### Variable bills

Electricity, water, gas, fuel. Enter a realistic **average**, not the worst case.

Clerk carries a category's unspent budget forward, so quiet months build a cushion that expensive months draw on before anything counts as overspending. Budget $125 for electricity: a $98 month leaves $27 behind, and a later $170 month spends it. Across a year the swings cancel out.

To find the number, use the month header's three-dot menu:

| Bill behaves like | Use |
| --- | --- |
| Seasonal — electricity, gas, heating | **Set budgets to 12 month average** |
| Drifts with prices — water, internet | **Set budgets to 3 month average** |

Round up slightly and leave it for the year.

Budgeting the worst case instead breaks nothing. It simply understates free money by the gap, every month.

### Annual bills

Insurance premiums, domain renewals, memberships, road tax. Divide by twelve and budget that every month. A $600 premium becomes $50 a month.

Each month sets $50 aside inside the category. When the $600 invoice arrives, a year of accrual covers it: no overspending, and the month it lands looks like any other. The dashboard shows the running total as **Set aside from earlier months**.

### Copy the year forward

For each category just set, click its budgeted amount again and choose **Copy until year end**. This fills every remaining month of the calendar year in one action.

Do not skip this for annual bills — copying forward is what builds the accrual.

## Step 5. Leave everyday categories empty

Groceries, dining, coffee, shopping, hobbies, gifts, entertainment. Leave the **Budgeted** cell blank.

This is what makes the report work. Clerk treats any non-income category with a budget as *committed*, and everything else as discretionary. Spending in an unbudgeted category comes straight out of free money.

The tracking budget will happily forecast these categories too. Doing so changes what free money means — see [Budgeting every category](#budgeting-every-category).

## Step 6. Check the numbers agree

Find **Projected Savings** on Actual's Budget screen, then open Clerk and look at **free money**.

The two should be the same number. If they are, Clerk is reading the budget as intended.

If they differ, an everyday category almost certainly has an amount in its Budgeted cell.

## Step 7. Let Clerk take over

In Clerk:

1. Press **Sync now**. Clerk reads the budget, runs Actual's bank sync, and files what arrives.
2. Open **Review → Catch up on all history** to work through transactions already in the budget.

The dashboard should now show something like:

```text
FREE MONEY · 2026-08 · day 21 of 31

    $1,905.00   92% left

    $175.00 of $2,080.00 spent since the 1st. Free money is $4,000.00
    expected income minus $1,920.00 already budgeted for bills.

    Safe to spend daily   $173.18
    Ahead of pace         $1,234.03
```

| Figure | Meaning |
| --- | --- |
| **Free money left** | What remains, in dollars and as a percentage |
| **Safe to spend daily** | Remaining divided by the days left, including today |
| **Pace marker** on the bar | Where an even burn would put you today |
| **Ahead / behind pace** | The gap between real spending and that even burn |
| **Projected month end** | Where the month lands if the rest matches so far |
| **Received so far** | Income that has actually arrived, beside what was expected |
| **Set aside from earlier months** | Budgeted to a bill previously and not yet spent |

The pace marker is the one to watch. Behind pace on the 8th is a nudge; behind pace on the 24th is nearly over.

---

## Keeping it current

Having copied the year forward in step 4, there is nothing to do month to month.

| When | Do this |
| --- | --- |
| Every January | Copy forward again. *Copy until year end* does not cross into a new calendar year |
| A bill changes | Update that category and copy it forward from the month the change takes effect |
| A new bill starts | Add the category, budget it, copy it forward |

Forget entirely and Clerk says so: free money jumps to your whole income, because nothing is committed against it.

## How free money is calculated

### Expected income

First match wins:

1. Income budgeted in Actual for the month — what step 3 sets up.
2. A monthly income set in Clerk under **Settings → Budget report**.
3. Income actually received this month, once it exceeds the average.
4. A trailing average of recent whole months.

The dashboard always names the basis used, and shows what has arrived beside what was expected, so a light month is visible rather than silent.

### What counts against free money

| Transaction | Counted? |
| --- | --- |
| Spending in a category with no budget | **Yes** — this is what free money pays for |
| Spending in a budgeted category, within budget | No — already committed |
| Spending in a budgeted category, beyond its budget and accrual | **The excess only** |
| Uncategorized spending | **Yes**, and reported separately so its provisional share is visible |
| Spending not yet cleared by the bank | **Yes**, in full |
| Transfers between your own accounts | No |
| Off-budget accounts such as a brokerage | No |
| Refunds | Subtracted from what the same category spent this month; anything left over comes back as money to spend |
| Income | Counted as income, never as spending |

### Refunds

A refund settles against the month that paid for the purchase. Return something you bought this month and the two cancel, leaving that category where it started.

When the purchase belongs to a month already reported, that report stands — it was true when it was sent, and the money is not available back then, it is available now. So the refund comes back as money this month has to spend, shown as **Refunded from earlier months**, rather than as spending pushed below zero. Free money itself does not move, which is what keeps it equal to Actual's Projected Savings.

### Carried budget

A category keeps whatever it was budgeted and did not spend, for up to twelve months — one annual cycle. Spending draws on that accrual before any of it counts as overspending, so money withheld from free money across several months is never charged again when it is finally spent.

Overspending does not carry the other way. A month that ran over was charged to free money then, and never becomes a debt the next month must also clear.

### A year in practice

Rent $1,800, electricity budgeted at $125, and a $600 insurance premium accrued at $50 a month — begun in September, with the premium billed the following March.

| Month | Electricity | Insurance | Set aside | Overspend | Remaining |
| --- | --- | --- | --- | --- | --- |
| Sep | $98 | — | $0 | $0 | $2,230 |
| Oct | $104 | — | $72 | $0 | $2,230 |
| Nov | $139 | — | $138 | $0 | $2,230 |
| Dec | $162 | — | $169 | $23 | $2,207 |
| Jan | $178 | — | $200 | $58 | $2,172 |
| Feb | $151 | — | $250 | $31 | $2,199 |
| Mar | $112 | **$600** | $300 | $250 | $1,980 |
| Apr | $96 | — | $8 | $0 | $2,230 |
| May–Aug | $89–$148 | — | rebuilding to $278 | $0 | $2,230 |

Autumn builds the cushion, and December through February draw it down before anything reaches free money. The winter overspends are real: $125 is genuinely low for a $178 January, and a year in the figure would be $130. March is real too — the premium had accrued only six months, so $250 of it was unfunded. A full year of accrual would have made March unremarkable.

---

## Troubleshooting

**Free money does not match Projected Savings.** An everyday category has a budgeted amount. Clear it, or list your bill groups explicitly under **Settings → Budget report → Committed category groups**.

**An annual bill shows a large overspend.** The accrual has not caught up. Started six months ago, a $600 bill is $300 funded and $300 not. It settles after a full cycle.

**A variable bill overspends every winter.** The budgeted average is too low. Use **Set budgets to 12 month average** rather than a three-month one, and copy it forward.

**Free money is much larger than expected.** Nothing is budgeted for the current month. Copy the year forward from step 4.

**Free money collapsed to almost nothing.** Every category is budgeted, so everything counts as committed. See below.

### Budgeting every category

Forecasting every category — which the tracking budget encourages — makes Clerk's default rule wrong, because *anything budgeted is committed* then sweeps in groceries and dining too.

Tell Clerk which groups are bills: **Settings → Budget report → Committed category groups**, ticking only the groups holding recurring bills. Once anything is ticked, only those groups count as committed.

### Rollover Overspending

Leave this Actual option alone. It pushes a category's deficit into the next month, making the current month look cheaper than it was. Clerk already accounts for overspending in the month it happened.

## Questions

**I am paid fortnightly, so some months have three paychecks.** Budget two paychecks in step 3. The third arrives as surplus rather than as money already spent.

**My income varies.** Budget a conservative figure — the lowest month worth planning around. Free money becomes a floor rather than a guess.

**I want savings treated as a bill.** Create a Savings category, budget the target contribution, and it becomes committed like any other bill. Free money then means genuinely free.

**One of my accounts is a credit card.** Card spending is on-budget and counts normally. Paying the card is a transfer between your own accounts, and transfers are ignored, so nothing counts twice.

**An annual bill is too large to accrue in twelve months.** It is an irregular purchase rather than a recurring bill. Budget it in the month it falls due and accept that the month has less free money.
