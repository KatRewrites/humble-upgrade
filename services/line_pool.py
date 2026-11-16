"""Non-repeating line picker (shuffle bag per key).

Every line in a pool is used once before any line repeats, and the same line
never comes up twice in a row (also across bag refills). Changing the pool for a
key (different list contents) starts a fresh bag.
"""
from __future__ import annotations

import random
from typing import Sequence

_last: dict[str, str] = {}
_bags: dict[str, list[str]] = {}
_sigs: dict[str, tuple[str, ...]] = {}


def pick(key: str, lines: Sequence[str], rng: random.Random | None = None) -> str:
    """Pick the next line for ``key`` from a shuffled, non-repeating bag."""
    if not lines:
        return ""
    r = rng or random
    pool = list(lines)
    if len(pool) == 1:
        _last[key] = pool[0]
        return pool[0]
    sig = tuple(pool)
    bag = _bags.get(key)
    if not bag or _sigs.get(key) != sig:
        bag = pool[:]
        r.shuffle(bag)
        last = _last.get(key)
        # bag pops from the end: don't open a new bag with the line we just used
        if bag[-1] == last:
            for i in range(len(bag) - 1):
                if bag[i] != last:
                    bag[i], bag[-1] = bag[-1], bag[i]
                    break
        _bags[key] = bag
        _sigs[key] = sig
    choice = bag.pop()
    _last[key] = choice
    return choice


def reset(key: str | None = None) -> None:
    if key is None:
        _last.clear()
        _bags.clear()
        _sigs.clear()
    else:
        _last.pop(key, None)
        _bags.pop(key, None)
        _sigs.pop(key, None)
