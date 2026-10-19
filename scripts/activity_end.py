"""Pure multi-signal end detector for activity-pane fixtures and drivers."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaneObservation:
    identifiers: tuple[str, ...]
    scroll_top: float
    client_height: float
    scroll_height: float
    loading: bool
    new_count: int
    pager_available: bool = False

    @property
    def near_bottom(self) -> bool:
        return self.scroll_top + self.client_height >= self.scroll_height - 3


class PaneEndDetector:
    def __init__(self):
        self.no_new_cycles = 0

    def observe(self, observation: PaneObservation) -> None:
        self.no_new_cycles = self.no_new_cycles + 1 if observation.new_count == 0 else 0

    def ready_for_final_probe(self, observation: PaneObservation, prior_top: float) -> bool:
        return (self.no_new_cycles >= 3 and observation.near_bottom
                and observation.scroll_top <= prior_top + 1 and not observation.loading
                and not observation.pager_available)

    def confirms_end(self, before: PaneObservation, after: PaneObservation) -> bool:
        return (self.no_new_cycles >= 3 and before.near_bottom and after.near_bottom
                and after.scroll_top <= before.scroll_top + 1
                and after.identifiers == before.identifiers and after.new_count == 0
                and not after.loading and not after.pager_available)
