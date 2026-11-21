"""A small least recently used memo, so a long running process keeps a bounded
number of expensive results."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class LruMemo(Generic[V]):
    def __init__(self) -> None:
        self._items: OrderedDict[Hashable, V] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable, compute: Callable[[], V], size: int) -> V:
        """The value for key, computed once while it stays among the size most recent."""
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
            value = compute()
            self._items[key] = value
            while len(self._items) > size:
                self._items.popitem(last=False)
            return value

    def __len__(self) -> int:
        return len(self._items)
