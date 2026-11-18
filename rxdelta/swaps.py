"""Package code swaps: the one rule the diff, the tools and gold share.

CMS lists one NDC per RXCUI per formulary and changes which NDC stands for a
drug from month to month. Compared NDC by NDC, a swap looks like one drug
removed and another added although the drug never left the formulary. This
module decides which removed and added NDC rows are really the same drug: rows
that share a key (a formulary pair and an RXCUI) pair up as one before and one
after. A removed row with no partner is a removal only if the RXCUI is gone from
the later formulary, and an added row only if the RXCUI was absent before.

It imports nothing from the diff, the tools or gold, so each of them can use it
without depending on the others. Classifying a pair stays with the caller.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

R = TypeVar("R")
A = TypeVar("A")
K = TypeVar("K", bound=Hashable)


@dataclass
class SwapResult(Generic[R, A]):
    # A removed row and an added row for the same drug: compare them as one.
    pairs: list[tuple[R, A]] = field(default_factory=list)
    # Rows whose drug really left, or really joined, the formulary.
    removals: list[R] = field(default_factory=list)
    additions: list[A] = field(default_factory=list)


def pair_swaps(
    removed: Iterable[R],
    added: Iterable[A],
    *,
    removed_key: Callable[[R], K],
    added_key: Callable[[A], K],
    listed_after: Callable[[K], bool],
    listed_before: Callable[[K], bool],
) -> SwapResult[R, A]:
    """Split NDC level removals and additions into swaps and real changes.

    `listed_after(key)` says whether the drug is on the later formulary at all,
    under any NDC; `listed_before(key)` the same for the earlier one. When one
    drug has several removed and added NDCs, every combination is paired, so
    the outcome does not depend on row order.
    """
    removed_by_key: dict[K, list[R]] = defaultdict(list)
    added_by_key: dict[K, list[A]] = defaultdict(list)
    for row in removed:
        removed_by_key[removed_key(row)].append(row)
    for new in added:
        added_by_key[added_key(new)].append(new)

    result: SwapResult[R, A] = SwapResult()
    for key, olds in removed_by_key.items():
        news = added_by_key.get(key, [])
        if news:
            result.pairs.extend((old, new) for old in olds for new in news)
        elif not listed_after(key):
            result.removals.extend(olds)
    for key, news in added_by_key.items():
        if key not in removed_by_key and not listed_before(key):
            result.additions.extend(news)
    return result
