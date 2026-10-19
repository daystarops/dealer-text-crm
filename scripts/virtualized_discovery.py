"""Pure state machine for conservative virtualized-grid end detection."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScrollObservation:
    identifiers: tuple[str, ...]
    scroll_top: float
    client_height: float
    scroll_height: float
    loading: bool
    new_count: int

    @property
    def near_bottom(self) -> bool:
        return self.scroll_top + self.client_height >= self.scroll_height - 3


class EndDetector:
    """Requires three dry cycles plus an explicit final no-movement probe."""
    def __init__(self):
        self.previous: ScrollObservation | None = None
        self.consecutive_no_new = 0

    def observe(self, current: ScrollObservation) -> None:
        if current.new_count == 0:
            self.consecutive_no_new += 1
        else:
            self.consecutive_no_new = 0
        self.previous = current

    def ready_for_final_probe(self, current: ScrollObservation, prior_top: float) -> bool:
        return (
            self.consecutive_no_new >= 3
            and current.scroll_top <= prior_top + 1
            and current.near_bottom
            and not current.loading
        )

    def confirms_end(self, before: ScrollObservation, after: ScrollObservation) -> bool:
        return (
            self.consecutive_no_new >= 3
            and before.near_bottom and after.near_bottom
            and after.scroll_top <= before.scroll_top + 1
            and after.identifiers == before.identifiers
            and after.new_count == 0
            and not after.loading
        )
