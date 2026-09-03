"""Turn noisy bank descriptors into a stable merchant key.

Card networks and payment processors decorate the same merchant differently on
every charge: `SQ *BLUE BOTTLE 4471`, `TST* Blue Bottle Coffee`, and
`BLUE BOTTLE COFFEE #4471 OAKLAND CA` are one merchant. Everything Clerk knows
about a merchant -- learned categories, cadence, typical amount -- hangs off the
key produced here, so normalization has to be aggressive about decoration and
conservative about real words.

Normalization deliberately stops short of guessing: a key that still carries a
store city is corrected by the prefix matching in `domain.memory`, whereas a key
that has been over-trimmed has lost identity that nothing downstream can
recover.
"""

from __future__ import annotations

import re
import unicodedata

# Processor and channel prefixes. Matched at the start of the descriptor only,
# because these words also appear inside genuine merchant names.
_PREFIXES = (
    "sq *",
    "sq*",
    "tst*",
    "tst *",
    "sp *",
    "sp*",
    "pp*",
    "pp *",
    "paypal *",
    "paypal*",
    "pypl *",
    "sumup *",
    "iz *",
    "wl *",
    "gopay*",
    "toast*",
    "clv*",
    "chk*",
    "pos debit",
    "pos purchase",
    "pos ",
    "debit card purchase",
    "credit card purchase",
    "check card purchase",
    "check card",
    "checkcard",
    "visa purchase",
    "visa dda pur",
    "mastercard purchase",
    "ach debit",
    "ach credit",
    "ach pmt",
    "ach transaction",
    "dbt crd",
    "dbt purchase",
    "recurring payment",
    "purchase authorized on",
    "purchase auth",
    "withdrawal",
    "electronic payment",
    "external withdrawal",
    "preauthorized debit",
    "web pmt",
    "web payment",
    "mobile purchase",
    "card purchase",
    "online payment",
)

# Trailing noise: channel suffixes that describe the charge, not the shop.
_SUFFIXES = (
    "purchase",
    "payment",
    "recurring",
    "debit",
    "credit",
    "card",
    "pending",
)

_US_STATES = frozenset(
    ["al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc", "pr"]
)

# Words that carry no identity but survive the token filters below.
_STOPWORDS = frozenset(
    ["the", "a", "an", "of", "and", "inc", "llc", "ltd", "co", "corp", "company", "usa", "us", "com", "net", "org", "store", "stores", "shop", "online", "payments", "payment", "purchase", "pmt", "bill", "billpay", "autopay", "auto", "transaction", "trans", "ref", "id", "auth", "web", "epay", "dda", "pur", "crd", "intl"]
)

# A short list of descriptor abbreviations that no amount of normalization can
# reconcile with the merchant's ordinary name. Keeping it small is deliberate:
# the learned memory handles everything else after a single observation.
_ALIASES = {
    "amzn mktp": "amazon",
    "amzn digital": "amazon",
    "amzn": "amazon",
    "amazon mktpl": "amazon",
    "amazon mktplace": "amazon",
    "amazon prime": "amazon prime",
    "wholefds": "whole foods",
    "wholefds mkt": "whole foods",
    "wm supercenter": "walmart",
    "wal mart": "walmart",
    "walmart com": "walmart",
    "sbux": "starbucks",
    "mcdonald s": "mcdonalds",
    "google youtubepremium": "youtube premium",
    "googleyoutubepremium": "youtube premium",
    "dd doordash": "doordash",
    "tfl travel": "tfl",
}

_URL_SUFFIX = re.compile(r"\b(?:www\.|https?://)?([a-z0-9-]+)\.(?:com|net|org|co|io|app|shop)\b")
_DATE_LIKE = re.compile(r"\b\d{1,4}[-/]\d{1,2}(?:[-/]\d{1,4})?\b")
_TIME_LIKE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_PHONE_LIKE = re.compile(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")
_LONG_DIGITS = re.compile(r"\b[#x*]?\d{3,}\b")
_MASKED = re.compile(r"\b[x*]{2,}\d*\b")
_HASH_NUMBER = re.compile(r"#\s*\w+")
# `7-eleven` and `h-e-b` are one word wearing a hyphen; `blue-bottle` is two.
# Only join across the hyphen when a digit sits on one side of it.
_DIGIT_HYPHEN = re.compile(r"(?<=\d)-(?=[a-z])|(?<=[a-z])-(?=\d)")
# A token mixing letters with two or more digits is a store or terminal id,
# never a name: `mktp8s41n`, `store0455`. `7eleven` and `h3` survive.
_ALNUM_ID = re.compile(r"^(?=.*[a-z])(?=(?:\D*\d){2,}).*$")
_NON_WORD = re.compile(r"[^a-z0-9&]+")
_MAX_TOKENS = 3


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _strip_prefixes(value: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix) :].lstrip(" -*:")
                changed = True
                break
    return value


def normalize_merchant(*descriptors: str | None) -> str:
    """Return a stable merchant key for the first descriptor that yields one.

    Callers pass their sources in order of trust -- usually the Actual payee
    name, then the raw imported description -- and get back the first key that
    survives normalization, or an empty string when none do.
    """

    for descriptor in descriptors:
        key = _normalize_one(descriptor)
        if key:
            return _ALIASES.get(key, key)
    return ""


def _normalize_one(descriptor: str | None) -> str:
    if not descriptor:
        return ""
    value = _strip_accents(str(descriptor)).casefold().strip()
    if not value:
        return ""

    # A bare domain identifies the merchant better than the rest of the line.
    domain = _URL_SUFFIX.search(value)
    if domain and len(domain.group(1)) > 2:
        value = domain.group(1)

    value = _strip_prefixes(value)
    value = value.replace("'", "").replace("’", "")
    value = _DIGIT_HYPHEN.sub("", value)
    value = _PHONE_LIKE.sub(" ", value)
    value = _TIME_LIKE.sub(" ", value)
    value = _DATE_LIKE.sub(" ", value)
    value = _HASH_NUMBER.sub(" ", value)
    value = _MASKED.sub(" ", value)
    value = _LONG_DIGITS.sub(" ", value)
    value = _NON_WORD.sub(" ", value)

    tokens = [token for token in value.split() if token]
    while tokens and (tokens[-1] in _US_STATES or tokens[-1] in _SUFFIXES):
        tokens.pop()

    kept: list[str] = []
    for token in tokens:
        if token in _STOPWORDS or token.isdigit() or _ALNUM_ID.match(token):
            continue
        kept.append(token)
        if len(kept) >= _MAX_TOKENS:
            break

    if not kept:
        # Everything looked like decoration. Preserve otherwise opaque tokens
        # that still contain a letter so identical descriptors such as `86st`
        # can be matched exactly next time. Pure numbers remain anonymous, and
        # keys_related will not broaden this into a fuzzy match.
        kept = [
            token
            for token in tokens
            if any(character.isalpha() for character in token)
        ][:_MAX_TOKENS]
    return " ".join(kept)


def merchant_label(*descriptors: str | None) -> str:
    """A human-facing name for a merchant key, preserving the original words."""
    for descriptor in descriptors:
        if descriptor and str(descriptor).strip():
            return " ".join(str(descriptor).split())[:80]
    return ""


def keys_related(left: str, right: str) -> bool:
    """True when two keys describe the same merchant at different detail.

    `starbucks` and `starbucks seattle` differ only by a store location that
    survived normalization on one descriptor and not the other. Requiring the
    shared part to be at least two tokens, or one token of real length, keeps
    `bp` from matching `bp fuel station` by accident.
    """

    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens, right_tokens = left.split(), right.split()
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    if longer[: len(shorter)] != shorter:
        return False
    return len(shorter) >= 2 or len(shorter[0]) >= 5


def rule_match_value(*descriptors: str | None) -> str:
    """The verbatim substring that a promoted Actual rule can match on.

    Actual evaluates promoted rules against the payee with a `contains`
    operator, so the value has to appear verbatim in the displayed merchant
    descriptor rather than in the normalized key. An empty result means no rule
    can be promoted for this merchant -- the alias table and the hyphen joining
    both produce keys that were never written in the payee -- and the learned
    memory keeps handling it instead.

    Very short matches are refused as well: a rule containing `UBER` would
    swallow every ride and every meal delivery alike.
    """

    for descriptor in descriptors:
        if not descriptor:
            continue
        tokens = _normalize_one(descriptor).split()
        if not tokens:
            continue
        original = " ".join(str(descriptor).split())
        haystack = _strip_accents(original).casefold()
        for length in range(min(len(tokens), 3), 0, -1):
            candidate = " ".join(tokens[:length])
            start = haystack.find(candidate)
            if start < 0:
                continue
            matched = original[start : start + len(candidate)].strip()
            if len(matched) >= 6 or (length >= 2 and len(matched) >= 5):
                return matched
    return ""
