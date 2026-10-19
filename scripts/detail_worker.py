"""Stage B one-record orchestration with fail-closed state transitions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from detail_index import DetailClaim, DetailIndex


class DetailFailure(RuntimeError):
    def __init__(self, category: str, *, terminal: bool = False):
        super().__init__(category)
        self.category = category
        self.terminal = terminal


@dataclass(frozen=True)
class ExtractedDetail:
    opportunity_id: str
    identity_record: object
    sanitized_payload: dict


class DetailAdapter(Protocol):
    def extract(self, claim: DetailClaim) -> ExtractedDetail: ...
    def cleanup(self) -> None: ...


class IdentityWriter(Protocol):
    def extraction_upsert(self, record: object) -> str: ...


@dataclass
class WorkerStats:
    attempted: int = 0
    completed: int = 0
    retryable_failures: int = 0
    terminal_failures: int = 0
    mismatches_rejected: int = 0


class DetailWorker:
    def __init__(self, index: DetailIndex, vault: IdentityWriter, adapter: DetailAdapter,
                 run_id: str, retry_delay_seconds: int = 300):
        self.index = index
        self.vault = vault
        self.adapter = adapter
        self.run_id = run_id
        self.retry_delay_seconds = retry_delay_seconds

    def run(self, success_limit: int) -> WorkerStats:
        stats = WorkerStats()
        while stats.completed < success_limit:
            claim = self.index.claim_next(self.run_id)
            if claim is None:
                break
            stats.attempted += 1
            try:
                extracted = self.adapter.extract(claim)
                if extracted.opportunity_id != claim.opportunity_id:
                    raise DetailFailure("opportunity_mismatch", terminal=True)
                identity_key = self.vault.extraction_upsert(extracted.identity_record)
                if identity_key != claim.stable_lead_key:
                    raise DetailFailure("opportunity_mismatch", terminal=True)
                try:
                    self.index.complete(claim, extracted.sanitized_payload, self.run_id)
                except Exception as error:
                    raise DetailFailure("local_write_failed") from error
                stats.completed += 1
            except DetailFailure as error:
                self.index.fail(claim, error.category, terminal=error.terminal,
                                retry_delay_seconds=self.retry_delay_seconds)
                if error.terminal:
                    stats.terminal_failures += 1
                else:
                    stats.retryable_failures += 1
                if error.category == "opportunity_mismatch":
                    stats.mismatches_rejected += 1
            except Exception:
                self.index.fail(claim, "required_content_missing", terminal=False,
                                retry_delay_seconds=self.retry_delay_seconds)
                stats.retryable_failures += 1
            finally:
                self.adapter.cleanup()
        return stats
