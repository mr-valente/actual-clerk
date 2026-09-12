"""Prompts for the one question Clerk asks a model.

The model never invents structure. It receives the budget's existing categories
as a numbered list and answers with a number, which removes every failure mode
that comes from asking a small local model to reproduce a UUID. It is also shown
how this budget has filed comparable spending, because a category tree is a
personal document: `Costco` belongs under Groceries in one budget and under
Household in another, and only the user's own history says which.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

SYSTEM_PROMPT = """You are a careful bookkeeping assistant for one person's personal budget.

You are given one merchant and the complete list of categories that already exist in this budget. Choose the single category where this person would file spending at that merchant.

Rules:
- Answer with the number of a category from the list. Never invent a category number.
- The examples show how this person has actually filed comparable spending. Follow the pattern they establish, even when a different category name sounds more natural to you.
- Answer 0 when no listed category is a reasonable home for this merchant, and put the name of the missing category in suggested_new_category. Answering 0 is correct and useful; a confident wrong answer is not.
- Set confidence honestly: above 0.85 only when the merchant clearly belongs in that category, near 0.5 when you are choosing between two plausible categories, below 0.4 when you are guessing.
- Judge by what the merchant sells, not by the size of the charge.
- Keep the reason to one short sentence."""


def _money(cents: int, currency: str) -> str:
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(int(cents)), 100)
    return f"{sign}{whole:,}.{remainder:02d} {currency}"


def format_category_list(candidates: Sequence[dict[str, Any]]) -> str:
    """Number every candidate; the number is what the model answers with."""
    lines = []
    for index, candidate in enumerate(candidates, start=1):
        group = candidate.get("group_name") or ""
        suffix = f"  (group: {group})" if group else ""
        income = "  [income]" if candidate.get("is_income") else ""
        lines.append(f"{index}. {candidate['name']}{suffix}{income}")
    return "\n".join(lines)


def build_user_prompt(
    *,
    merchant: dict[str, Any],
    candidates: Sequence[dict[str, Any]],
    examples: Sequence[dict[str, Any]],
    currency: str = "USD",
    rule_hints: Sequence[dict[str, Any]] = (),
) -> str:
    """Assemble the classification request for one merchant."""

    sections: list[str] = ["MERCHANT TO CATEGORIZE"]
    sections.append(f"Name as shown in Actual: {merchant.get('label') or merchant.get('key')}")
    descriptions = [item for item in merchant.get("descriptions", []) if item][:3]
    if descriptions:
        sections.append("Bank descriptions: " + " | ".join(descriptions))
    amounts = merchant.get("amounts") or []
    if amounts:
        shown = ", ".join(_money(amount, currency) for amount in amounts[:6])
        sections.append(f"Recent amounts: {shown}")
    if merchant.get("account_name"):
        sections.append(f"Account: {merchant['account_name']}")
    if merchant.get("count"):
        sections.append(f"Transactions waiting: {merchant['count']}")
    if merchant.get("cadence"):
        sections.append(f"Observed cadence: {merchant['cadence']}")

    if rule_hints:
        sections.append("")
        sections.append("RULES THIS PERSON HAS SET FOR SIMILAR MERCHANTS")
        for hint in rule_hints:
            sections.append(
                f"- {hint.get('merchant_label') or hint.get('merchant_key')} "
                f"-> {hint.get('category_name') or '?'} (always)"
            )

    if examples:
        sections.append("")
        sections.append("HOW THIS BUDGET FILES SIMILAR SPENDING")
        for example in examples:
            label = example.get("payee_name") or example.get("merchant_key") or "?"
            sections.append(
                f"- {label} -> {example.get('category_name')} "
                f"({_money(int(example.get('amount_cents') or 0), currency)})"
            )

    sections.append("")
    sections.append("CATEGORIES IN THIS BUDGET")
    sections.append(format_category_list(candidates))
    sections.append("")
    sections.append(
        "Reply with the number of the best category, or 0 if none of them fit this merchant."
    )
    return "\n".join(sections)


ALIAS_SYSTEM_PROMPT = """You are a careful bookkeeping assistant for one person's personal budget.

You are given the name of a merchant as it appears on a bank statement or a card notification, and a numbered list of merchants this budget already knows. Decide whether the new name is the same business as one of them.

Rules:
- Answer with the number of the matching merchant, or 0 when none of them is the same business. Answering 0 is correct and useful; a confident wrong match is not.
- The same business means the same company or shop, not the same kind of shop. A coffee chain is not the same business as another coffee chain.
- A card network, a payment processor, or a store number is decoration, not identity.
- Set confidence honestly: above 0.85 only when the names plainly denote one business, below 0.5 when you are guessing.
- Keep the reason to one short sentence."""


def build_alias_prompt(*, merchant: dict[str, Any], candidates: Sequence[str]) -> str:
    """Ask whether an unfamiliar merchant is one the budget already knows by another name."""
    sections = ["MERCHANT"]
    sections.append(f"Name: {merchant.get('label') or merchant.get('key')}")
    descriptions = [item for item in merchant.get("descriptions", []) if item][:3]
    if descriptions:
        sections.append("Bank descriptions: " + " | ".join(descriptions))
    sections.append("")
    sections.append("MERCHANTS THIS BUDGET ALREADY KNOWS")
    for index, candidate in enumerate(candidates, start=1):
        sections.append(f"{index}. {candidate}")
    sections.append("")
    sections.append(
        "Reply with the number of the merchant that is the same business, or 0 if none is."
    )
    return "\n".join(sections)
