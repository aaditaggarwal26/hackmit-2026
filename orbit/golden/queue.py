"""Priority queue, protocol.md §5.3: the behaviour of rtl/priority_queue.v as a list.
Head first, score descending, earlier insert first among equals."""

from __future__ import annotations

from orbit import config as params

NO_FRAME = 0xFFFF


class PriorityQueue:
    def __init__(self, limit: int = params.QUEUE_DEPTH):
        self.limit = limit
        self.cells: list[tuple[int, int]] = []  # (score, frame_id)
        self.evicted = 0

    def __len__(self) -> int:
        return len(self.cells)

    @property
    def has_data(self) -> bool:
        return bool(self.cells)

    @property
    def top(self) -> tuple[int, int]:
        return self.cells[0] if self.cells else (0, NO_FRAME)

    def insert(self, score: int, frame_id: int) -> int:
        """Returns the id of the frame lost by this insert, or NO_FRAME."""
        lost = NO_FRAME
        if len(self.cells) >= self.limit:
            self.evicted = (self.evicted + 1) & 0xFFFF
            if score <= self.cells[-1][0]:
                return frame_id  # not better than the tail: the newcomer is the one lost
            lost = self.cells.pop()[1]
        i = 0
        while i < len(self.cells) and self.cells[i][0] >= score:
            i += 1
        self.cells.insert(i, (score, frame_id))
        return lost

    def pop(self) -> tuple[int, int]:
        return self.cells.pop(0)

    def set_limit(self, limit: int) -> int:
        """CONFIG_SET may shrink the queue; entries drop from the tail. Returns how many."""
        self.limit = limit
        dropped = max(0, len(self.cells) - limit)
        if dropped:
            del self.cells[limit:]
            self.evicted = (self.evicted + dropped) & 0xFFFF
        return dropped


if __name__ == "__main__":  # ponytail: smallest check that fails if the ordering rules break
    q = PriorityQueue(3)
    assert [q.insert(5, 1), q.insert(9, 2), q.insert(5, 3)] == [NO_FRAME] * 3
    assert q.cells == [(9, 2), (5, 1), (5, 3)]  # equal scores keep insert order
    assert q.insert(4, 4) == 4 and len(q) == 3  # not better than the tail: the newcomer is lost
    assert q.insert(7, 5) == 3 and q.cells == [(9, 2), (7, 5), (5, 1)]  # tail evicted
    assert q.pop() == (9, 2) and q.top == (7, 5) and q.evicted == 2
    print("ok")
