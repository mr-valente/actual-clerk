"""What Clerk has learned about where a merchant's spending belongs.

The memory is rebuilt from the user's own categorized history on every run and
merged with the decisions Clerk itself has applied. Nothing here calls a model:
most transactions are repeat visits to merchants the budget already knows, and
answering those from evidence is both cheaper and more accurate than asking.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from actual_clerk.domain.merchants import keys_related

# A category chosen 9 months ago still describes the merchant, but a more
# recent choice describes it better. Half the weight per 9 months.
_HALF_LIFE_DAYS = 270.0
# An explicit correction is a statement of intent, not another data point.
_CORRECTION_WEIGHT = 3.0


@dataclass(frozen=True)
class MemoryMatch:
    category_id: str
    category_name: str
    confidence: float
    observations: float
    share: float
    matched_key: str
    exact: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "category_id": self.category_id,
            "category_name": self.category_name,
            "confidence": round(self.confidence, 4),
            "observations": round(self.observations, 2),
            "share": round(self.share, 4),
            "matched_key": self.matched_key,
            "exact": self.exact,
        }


@dataclass
class _Bucket:
    """Evidence for one merchant, counted twice on purpose.

    `weights` are recency-discounted and answer *which* category the merchant
    belongs to now. `counts` are raw sightings and answer *how much* evidence
    there is at all -- a number that must not shrink just because the spending
    happened a while ago.
    """

    weights: dict[str, float] = field(default_factory=dict)
    counts: dict[str, float] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    def add(self, category_id: str, category_name: str, weight: float, count: float) -> None:
        self.weights[category_id] = self.weights.get(category_id, 0.0) + weight
        self.counts[category_id] = self.counts.get(category_id, 0.0) + count
        if category_name:
            self.names[category_id] = category_name

    def merge(self, other: _Bucket) -> None:
        for category_id, weight in other.weights.items():
            self.add(
                category_id,
                other.names.get(category_id, ""),
                weight,
                other.counts.get(category_id, 0.0),
            )


def decay_weight(observed: datetime.date, today: datetime.date) -> float:
    """Exponentially discount an observation by its age, never below a floor."""
    age = max(0, (today - observed).days)
    return max(0.05, math.pow(0.5, age / _HALF_LIFE_DAYS))


class MerchantMemory:
    """Weighted merchant -> category evidence with prefix-tolerant lookup."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def __len__(self) -> int:
        return len(self._buckets)

    @property
    def keys(self) -> list[str]:
        return sorted(self._buckets)

    def add(
        self,
        merchant_key: str,
        category_id: str,
        category_name: str,
        weight: float = 1.0,
        count: float = 1.0,
    ) -> None:
        if not merchant_key or not category_id or weight <= 0:
            return
        self._buckets.setdefault(merchant_key, _Bucket()).add(
            category_id, category_name or "", weight, count
        )

    def add_transactions(
        self, rows: Iterable[dict], today: datetime.date | None = None
    ) -> MerchantMemory:
        """Absorb categorized history rows shaped `{merchant_key, category_id, ...}`."""
        today = today or datetime.date.today()
        for row in rows:
            observed = row.get("date") or today
            self.add(
                str(row.get("merchant_key") or ""),
                str(row.get("category_id") or ""),
                str(row.get("category_name") or ""),
                decay_weight(observed, today),
                1.0,
            )
        return self

    def add_stored(self, rows: Iterable[dict], today: datetime.date | None = None) -> MerchantMemory:
        """Absorb Clerk's own persisted decisions, honouring user corrections."""
        today = today or datetime.date.today()
        for row in rows:
            hits = float(row.get("hits") or 0)
            corrections = float(row.get("corrections") or 0)
            if hits <= 0 and corrections <= 0:
                continue
            last_seen = row.get("last_seen_date") or today
            count = hits + corrections * _CORRECTION_WEIGHT
            self.add(
                str(row.get("merchant_key") or ""),
                str(row.get("category_id") or ""),
                str(row.get("category_name") or ""),
                count * decay_weight(last_seen, today),
                count,
            )
        return self

    def lookup(
        self,
        merchant_key: str,
        *,
        min_observations: float = 2.0,
        min_confidence: float = 0.75,
        allowed_categories: set[str] | None = None,
    ) -> MemoryMatch | None:
        """Return the dominant category for a merchant, or None when unclear.

        Exact key evidence is preferred. Only when a merchant has never been
        seen under this exact key do related keys -- the same merchant carrying
        a store location on one statement and not another -- get pooled.
        """

        if not merchant_key:
            return None
        bucket = self._buckets.get(merchant_key)
        exact = bucket is not None
        if bucket is None:
            bucket = self._pooled(merchant_key)
            if bucket is None:
                return None
        return self._resolve(
            bucket,
            merchant_key,
            exact=exact,
            min_observations=min_observations,
            min_confidence=min_confidence,
            allowed_categories=allowed_categories,
        )

    def _pooled(self, merchant_key: str) -> _Bucket | None:
        pooled = _Bucket()
        found = False
        for candidate, bucket in self._buckets.items():
            if keys_related(candidate, merchant_key):
                pooled.merge(bucket)
                found = True
        return pooled if found else None

    def _resolve(
        self,
        bucket: _Bucket,
        merchant_key: str,
        *,
        exact: bool,
        min_observations: float,
        min_confidence: float,
        allowed_categories: set[str] | None,
    ) -> MemoryMatch | None:
        weights = bucket.weights
        if allowed_categories is not None:
            weights = {
                category_id: weight
                for category_id, weight in weights.items()
                if category_id in allowed_categories
            }
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return None
        category_id, weight = max(weights.items(), key=lambda item: (item[1], item[0]))
        share = weight / total_weight
        sightings = sum(bucket.counts.get(key, 0.0) for key in weights)
        confidence = share * saturation(sightings)
        # A pooled match rests on a normalization guess as well as on history.
        if not exact:
            confidence *= 0.95
        if sightings < min_observations or confidence < min_confidence:
            return None
        return MemoryMatch(
            category_id=category_id,
            category_name=bucket.names.get(category_id, ""),
            confidence=confidence,
            observations=sightings,
            share=share,
            matched_key=merchant_key,
            exact=exact,
        )

    def evidence(self, merchant_key: str) -> list[dict[str, object]]:
        """All categories seen for a merchant, strongest first, for the UI."""
        bucket = self._buckets.get(merchant_key) or self._pooled(merchant_key)
        if bucket is None:
            return []
        total = sum(bucket.weights.values()) or 1.0
        return [
            {
                "category_id": category_id,
                "category_name": bucket.names.get(category_id, ""),
                "sightings": round(bucket.counts.get(category_id, 0.0), 2),
                "share": round(weight / total, 4),
            }
            for category_id, weight in sorted(
                bucket.weights.items(), key=lambda item: item[1], reverse=True
            )
        ]


def saturation(sightings: float) -> float:
    """How much a body of evidence deserves to be trusted on its size alone.

    One sighting is suggestive, two are convincing, and the curve flattens
    after that so a merchant visited weekly does not outrank the calibration of
    the share itself.
    """

    if sightings <= 0:
        return 0.0
    return sightings / (sightings + 0.5)


def token_similarity(left: str, right: str) -> float:
    """Jaccard overlap between two merchant keys, used to pick model examples."""
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    if not intersection:
        return 0.0
    return len(intersection) / len(left_tokens | right_tokens)


def similar_examples(
    merchant_key: str,
    history: Sequence[dict],
    *,
    limit: int = 8,
) -> list[dict]:
    """Pick the user's own transactions that best illustrate this merchant.

    The model is far more accurate when it is shown how *this* budget files
    comparable spending than when it is asked to reason from category names
    alone. Exact and near matches lead; the remainder is filled with a spread of
    distinct categories so the examples teach the shape of the whole budget
    rather than one corner of it.
    """

    if limit <= 0:
        return []
    scored: list[tuple[float, dict]] = []
    for row in history:
        key = str(row.get("merchant_key") or "")
        if not key or not row.get("category_id"):
            continue
        score = 1.0 if key == merchant_key else token_similarity(key, merchant_key)
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("date") or "")), reverse=False)

    chosen: list[dict] = []
    seen_categories: set[str] = set()
    seen_keys: set[str] = set()
    # First pass: the closest merchants, one example per merchant key.
    for score, row in scored:
        if score <= 0:
            break
        key = str(row.get("merchant_key"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        seen_categories.add(str(row.get("category_id")))
        chosen.append(row)
        if len(chosen) >= limit:
            return chosen
    # Second pass: broaden the category coverage with unrelated merchants.
    for _, row in scored:
        category_id = str(row.get("category_id"))
        key = str(row.get("merchant_key"))
        if category_id in seen_categories or key in seen_keys:
            continue
        seen_categories.add(category_id)
        seen_keys.add(key)
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen
