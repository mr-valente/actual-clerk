# Setting up an Envelope Budget

This guide configures Actual's default **envelope budgeting** so that Actual Clerk can report your free money: what is left each month once everything already promised is accounted for.

```
free money = expected income - committed spending
remaining  = free money - what you have spent since the 1st
```

Allow about twenty minutes, plus a minute at the start of each month.

> Actual's **Tracking Budget** fits this report more closely — its own Projected Savings is the same calculation, and it can budget a whole year in one action. See [Setting up a Tracking Budget](tracking-budget.md) if you are not committed to envelope budgeting.

## Before you start

- Clerk connected to your Actual server and showing your accounts.
- A few months of transaction history in Actual. Clerk uses it to learn where your spending belongs.

### What to expect from this mode

Envelope budgeting is built around assigning every available dollar a job. This report asks you to leave everyday categories unbudgeted on purpose, so two things will look unusual and are safe to ignore:

- Actual's **To Budget** figure stays large, because you are deliberately not assigning everything.
- Category balances roll forward, so a bill category accumulates when underspent. Clerk reads the budgeted amount rather than the balance, so this does not affect free money.

---

## Step 1. Tell Clerk your monthly income

Envelope budgeting has no budgeted amount for income — income arrives in **To Budget** instead. Clerk therefore needs the figure directly.

1. In Clerk, open **Settings → Budget report**.
2. Set **Monthly income** to your expected take-home pay for a normal month.
3. Save.

If you are paid fortnightly, enter **two paychecks**, not 2.17. Three-paycheck months then arrive as a surplus rather than as money already spent.

Leaving this at 0 is possible: Clerk falls back to income received this month, then to a trailing average of recent months. A fixed figure is more predictable when your pay is steady.

## Step 2. Create a category for every recurring bill

In Actual, open **Budget** from the left sidebar. Each thing you pay on a schedule needs its own category: rent or mortgage, electricity, gas, water, internet, phone, insurance, car payment, childcare, debt payments, gym, and every subscription.

To add a category:

1. Hover over the category group it belongs in, such as **Bills**.
2. Click the **+** on the group's row, or open the group's three-dot menu and choose **Add category**.
3. Type the name and press Enter.

To add a group, scroll to the bottom of the category list and use the add-group control.

Group names do not matter to Clerk. What matters is which categories carry a budgeted amount.

## Step 3. Budget each recurring bill

Click a category's **Budgeted** cell and enter its amount. The right amount depends on the kind of bill.

### Fixed bills

Rent, phone, insurance, subscriptions. Enter the exact amount.

### Variable bills

Electricity, water, gas, fuel. Enter a realistic **average**, not the worst case.

Clerk carries a category's unspent budget forward, so quiet months build a cushion that expensive months draw on before anything counts as overspending. Budget $125 for electricity: a $98 month leaves $27 behind, and a later $170 month spends it. Across a year the swings cancel out. Actual's own category balance accumulates the same way, so the two agree.

To find the number, use the month header's three-dot menu:

| Bill behaves like | Use |
| --- | --- |
| Seasonal — electricity, gas, heating | **Set budgets to 12 month average** |
| Drifts with prices — water, internet | **Set budgets to 3 month average** |

Round up slightly.

Budgeting the worst case instead breaks nothing. It simply understates free money by the gap, every month.

### Annual bills

Insurance premiums, domain renewals, memberships, road tax. Divide by twelve and budget that every month. A $600 premium becomes $50 a month.

Each month sets $50 aside inside the category — visibly, since envelope balances roll forward. When the $600 invoice arrives, a year of accrual covers it: no overspending, and the month it lands looks like any other. Clerk's dashboard shows the running total as **Set aside from earlier months**.

## Step 4. Leave everyday categories empty

Groceries, dining, coffee, shopping, hobbies, gifts, entertainment. Leave the **Budgeted** cell blank.

This is what makes the report work. Clerk treats any non-income category with a budget as *committed*, and everything else as discretionary. Spending in an unbudgeted category comes straight out of free money.

This is also the step that leaves Actual's **To Budget** figure high. That is expected here.

## Step 5. Check the numbers agree

Add up the **Budgeted** column across your bill categories. In Clerk, that total should match **Committed** on the dashboard, and free money should equal your monthly income minus it.

If committed is higher than expected, an everyday category has an amount in its Budgeted cell.

## Step 6. Let Clerk take over

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

Actual does not carry budgeted amounts into a new month on its own, and envelope budgeting has no *copy until year end*. A new month starts with an empty Budgeted column, which Clerk reads as nothing being committed — free money would look far larger than it is.

On the 1st of each month:

1. Open the new month in Actual.
2. Click the **three-dot menu** at the top of the budget.
3. Choose **Copy last month's budget**.
4. Adjust anything that changed.

**Set budgets to 3 month average** is the alternative when bills drift.

Forget entirely and Clerk says so: free money jumps to your whole income, because nothing is committed against it.

## How free money is calculated

### Expected income

First match wins:

1. Income budgeted in Actual for the month — envelope budgets have none, so this does not apply.
2. A monthly income set in Clerk under **Settings → Budget report** — what step 1 sets up.
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
| Refunds | Subtracted from what the month has spent |
| Income | Counted as income, never as spending |

### Carried budget

A category keeps whatever it was budgeted and did not spend, for up to twelve months — one annual cycle. Spending draws on that accrual before any of it counts as overspending, so money withheld from free money across several months is never charged again when it is finally spent.

Overspending does not carry the other way. A month that ran over was charged to free money then, and never becomes a debt the next month must also clear.

Clerk works this out from the budgeted amounts it reads, independently of the balances Actual rolls forward. The two follow the same logic, so the accrual you see building in an Actual category and the figure Clerk reports as set aside describe the same money.

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

**Free money is much larger than expected.** The new month has nothing budgeted. Use **Copy last month's budget**.

**Committed is higher than the total of my bills.** An everyday category has a budgeted amount. Clear it, or list your bill groups explicitly under **Settings → Budget report → Committed category groups**.

**An annual bill shows a large overspend.** The accrual has not caught up. Started six months ago, a $600 bill is $300 funded and $300 not. It settles after a full cycle.

**A variable bill overspends every winter.** The budgeted average is too low. Use **Set budgets to 12 month average** rather than a three-month one.

**Free money swings month to month.** Set a fixed **Monthly income** in Clerk rather than relying on what has arrived so far.

**To Budget in Actual is large and keeps nagging.** Expected in this setup, because everyday categories are deliberately unbudgeted. If it bothers you, the tracking budget presents the same money as a projected surplus instead.

### Budgeting every category

Assigning every dollar a job — envelope budgeting's actual philosophy — makes Clerk's default rule wrong, because *anything budgeted is committed* then sweeps in groceries and dining too, and free money collapses to almost nothing.

Tell Clerk which groups are bills: **Settings → Budget report → Committed category groups**, ticking only the groups holding recurring bills. Once anything is ticked, only those groups count as committed and the leave-it-blank rule no longer applies.

## Questions

**I am paid fortnightly, so some months have three paychecks.** Set two paychecks as your monthly income in step 1. The third arrives as surplus rather than as money already spent.

**My income varies.** Leave **Monthly income** at 0 and let Clerk use its trailing average, raising *Months in the income average* in Settings to smooth it further.

**I want savings treated as a bill.** Create a Savings category, budget the target contribution, and it becomes committed like any other bill. Free money then means genuinely free.

**One of my accounts is a credit card.** Card spending is on-budget and counts normally. Paying the card is a transfer between your own accounts, and transfers are ignored, so nothing counts twice.

**An annual bill is too large to accrue in twelve months.** It is an irregular purchase rather than a recurring bill. Budget it in the month it falls due and accept that the month has less free money.
