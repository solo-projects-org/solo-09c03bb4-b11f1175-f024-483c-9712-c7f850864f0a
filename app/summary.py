"""Incremental, cacheable per-market monthly summaries.

When proofreading two releases of the same price month, unchanged quotations
can be reused from a previous summary instead of recomputing every slip.  This
module implements that reuse while guaranteeing that its output is identical to
a full recomputation of the target table:

* A cached summary is reused only when the base table digest and the digests of
  every chain step leading to it match the manifest of the chain that actually
  verified the target release ("底表摘要相符").
* Otherwise the service starts from a full recomputation, or from the newest
  matching cached version and replays only the correction packages that remain
  between that version and the target, updating only the quotation slips the
  packages touch.  A slip whose market or month changed is subtracted from the
  old group before being added to the new one.
* Per-market minima and maxima are maintained as price frequency maps, so
  deleting the quotation that currently holds an extreme still yields exact
  extrema.
* Replay happens on local working copies.  A failed chain attempt never writes
  the cache and never affects a page: any replay problem falls back to a full
  recomputation from the verified target document.
* The process keeps at most :data:`MAX_CACHED_SUMMARIES` summary entries
  (per (version, month)); the least recently used entry is evicted.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .archive import (
    Archive,
    BaseTable,
    ChainStep,
    Manifest,
    PatchPackage,
    document_digest,
)
from .jsonpatch import PatchError, apply_patch, parse_pointer
from .pricing import (
    GroupSummary,
    TableError,
    average_yuan,
    filter_by_month,
    month_bounds,
    parse_quote,
    parse_quotes,
)

MAX_CACHED_SUMMARIES = 4

# Replay modes reported to the page (and to machine-readable callers/tests).
MODE_FULL = "full"  # 全量复算
MODE_INCREMENTAL = "incremental"  # 沿用汇总 + 局部更新
MODE_CACHE_HIT = "cache_hit"  # 完全命中缓存，本次未检查任何单号


class SummaryError(ValueError):
    """A monthly summary cannot be produced from the verified chain."""


@dataclass
class _MarketAgg:
    """Mutable running aggregate for one market inside one month."""

    label: str
    count: int = 0
    total_fen: int = 0
    # price_fen -> number of quotations at that price; keeps extrema exact
    # even after the quotation holding the min/max is removed.
    price_counts: dict[int, int] = field(default_factory=dict)

    def add_price(self, price_fen: int) -> None:
        self.count += 1
        self.total_fen += price_fen
        self.price_counts[price_fen] = self.price_counts.get(price_fen, 0) + 1

    def remove_price(self, price_fen: int) -> None:
        occurrences = self.price_counts.get(price_fen, 0)
        if occurrences <= 0:
            raise KeyError(price_fen)
        self.count -= 1
        self.total_fen -= price_fen
        if occurrences == 1:
            del self.price_counts[price_fen]
        else:
            self.price_counts[price_fen] = occurrences - 1

    @property
    def min_fen(self) -> int:
        return min(self.price_counts)

    @property
    def max_fen(self) -> int:
        return max(self.price_counts)

    def to_group(self) -> GroupSummary:
        if self.count == 0:
            raise ValueError("empty market aggregate cannot be rendered")
        return GroupSummary(
            label=self.label,
            count=self.count,
            total_fen=self.total_fen,
            average_yuan=average_yuan(self.total_fen, self.count),
            min_fen=self.min_fen,
            max_fen=self.max_fen,
        )


@dataclass(frozen=True)
class SummaryEntry:
    """One cached monthly summary.

    ``chain_digests`` holds the RFC 8785 digest of every version on the chain
    that produced the summary, starting with the base table digest.  A reuse is
    valid only when the chain that verifies the target release reproduces every
    one of these digests.
    """

    version: str
    month: str
    groups: tuple[GroupSummary, ...]
    chain_digests: tuple[str, ...]
    total_quote_count: int


@dataclass
class SummaryResult:
    version: str
    month: str
    groups: list[GroupSummary]
    mode: str
    checked_quotation_count: int
    base_version: str | None
    total_quote_count: int
    month_options: list[str]


class SummaryCache:
    """Process-wide LRU cache of at most :data:`MAX_CACHED_SUMMARIES` entries."""

    def __init__(self, capacity: int = MAX_CACHED_SUMMARIES):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._entries: OrderedDict[tuple[str, str], SummaryEntry] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def key(version: str, month: str) -> tuple[str, str]:
        return version, month

    def get(self, version: str, month: str) -> SummaryEntry | None:
        with self._lock:
            key = self.key(version, month)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, entry: SummaryEntry) -> None:
        with self._lock:
            key = self.key(entry.version, entry.month)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._entries.keys())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _build_entry(
    version: str, month: str, document: Any, chain_digests: tuple[str, ...]
) -> SummaryEntry:
    """Full recomputation of one month's per-market summary."""

    quotes = filter_by_month(parse_quotes(document), month)
    aggs: dict[str, _MarketAgg] = {}
    for quote in quotes:
        agg = aggs.setdefault(quote.market, _MarketAgg(label=quote.market))
        agg.add_price(quote.price_fen)
    groups = tuple(
        aggs[name].to_group() for name in sorted(aggs, key=lambda value: str(value))
    )
    return SummaryEntry(
        version=version,
        month=month,
        groups=groups,
        chain_digests=chain_digests,
        total_quote_count=len(document),
    )


def _slip_token(pointer: str) -> str | None:
    """Return the quotation number addressed by a top-level table pointer."""

    if not isinstance(pointer, str):
        return None
    try:
        tokens = parse_pointer(pointer)
    except PatchError:
        return None
    if not tokens:
        return None
    return tokens[0]


def _affected_slips(operations: list[Any]) -> set[str] | None:
    """Distinct quotation numbers a correction package may modify.

    Only operations that mutate the table are considered; ``test`` operations
    change nothing.  A pointer that does not address a single top-level slip
    (e.g. the document root) cannot be applied incrementally and forces a full
    recomputation, signalled by ``None``.
    """

    affected: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            return None
        op = operation.get("op")
        if op == "test":
            continue
        if op not in {"add", "remove", "replace", "move", "copy"}:
            return None
        token = _slip_token(operation.get("path", ""))
        if token is None:
            return None
        affected.add(token)
        if op in {"move", "copy"}:
            source = _slip_token(operation.get("from", ""))
            if source is None:
                return None
            affected.add(source)
    return affected


class SummaryService:
    """Produces monthly per-market summaries, reusing cached work when safe."""

    def __init__(self, archive: Archive, cache: SummaryCache | None = None):
        self.archive = archive
        self.cache = cache if cache is not None else SummaryCache()

    # ---------------------------------------------------------------- public API
    def summarize(
        self,
        manifest: Manifest,
        target_version: str,
        month: str,
        target_document: Any,
        chain: list[ChainStep],
    ) -> SummaryResult:
        month = month.strip()
        month_bounds(month)  # raises ValueError on malformed months
        chain_digests = tuple(step.sha256 for step in chain)
        if not chain_digests or document_digest(target_document) != chain_digests[-1]:
            # The document handed to us must be the one digest verification
            # already proved; anything else is a programming error upstream.
            raise SummaryError("目标版文档与已核对摘要不一致，拒绝出汇总")

        month_options = sorted(
            {q.quoted_on.isoformat()[:7] for q in parse_quotes(target_document)},
            reverse=True,
        )

        # 1) Exact cache hit: nothing new to check.
        cached = self.cache.get(target_version, month)
        if cached is not None and self._digests_match(cached.chain_digests, chain_digests):
            return SummaryResult(
                version=target_version,
                month=month,
                groups=list(cached.groups),
                mode=MODE_CACHE_HIT,
                checked_quotation_count=0,
                base_version=target_version,
                total_quote_count=cached.total_quote_count,
                month_options=month_options,
            )

        # 2) Incremental update from the newest compatible cached version.
        try:
            outcome = self._incremental(
                manifest, target_version, month, target_document, chain, chain_digests
            )
        except (PatchError, TableError, SummaryError, KeyError, ValueError):
            outcome = None
        if outcome is not None:
            return SummaryResult(
                version=target_version,
                month=month,
                month_options=month_options,
                **outcome,
            )

        # 3) No usable summary: full recomputation.
        entry = _build_entry(target_version, month, target_document, chain_digests)
        self.cache.put(entry)
        return SummaryResult(
            version=target_version,
            month=month,
            groups=list(entry.groups),
            mode=MODE_FULL,
            checked_quotation_count=len(target_document),
            base_version=target_version,
            total_quote_count=len(target_document),
            month_options=month_options,
        )

    # ------------------------------------------------------------- incremental
    @staticmethod
    def _digests_match(
        cached_digests: tuple[str, ...], chain_digests: tuple[str, ...]
    ) -> bool:
        """Every cached digest must appear, in order, on the verified chain."""

        if not cached_digests or len(cached_digests) > len(chain_digests):
            return False
        cursor = 0
        for expected in cached_digests:
            while cursor < len(chain_digests) and chain_digests[cursor] != expected:
                cursor += 1
            if cursor >= len(chain_digests):
                return False
            cursor += 1
        return True

    def _incremental(
        self,
        manifest: Manifest,
        target_version: str,
        month: str,
        target_document: Any,
        chain: list[ChainStep],
        chain_digests: tuple[str, ...],
    ) -> dict[str, Any] | None:
        # Pick the newest cached entry (same month) whose verified digests are a
        # subsequence of this target's verified chain.
        candidate: SummaryEntry | None = None
        candidate_step_index = -1
        digest_index = {digest: index for index, digest in enumerate(chain_digests)}
        for cached_version, cached_month in list(self.cache.keys()):
            if cached_month != month:
                continue
            entry = self.cache.get(cached_version, cached_month)
            if entry is None or not self._digests_match(entry.chain_digests, chain_digests):
                continue
            end_index = digest_index.get(entry.chain_digests[-1], -1)
            if end_index > candidate_step_index:
                candidate, candidate_step_index = entry, end_index
        if candidate is None:
            return None

        remaining_steps = chain[candidate_step_index + 1 :]
        if not remaining_steps:
            return {
                "groups": list(candidate.groups),
                "mode": MODE_CACHE_HIT,
                "checked_quotation_count": 0,
                "base_version": candidate.version,
                "total_quote_count": candidate.total_quote_count,
            }

        # The cached groups only retain their extrema, not full price
        # frequencies; rebuild running aggregates from the verified document at
        # the cached version, then apply only the remaining packages.
        start, end = month_bounds(month)
        document = self._verified_document_at(manifest, chain, candidate_step_index)
        aggs: dict[str, _MarketAgg] = {}
        for quotation_no, raw_entry in document.items():
            quote = parse_quote(quotation_no, raw_entry)
            if start <= quote.quoted_on <= end:
                agg = aggs.setdefault(quote.market, _MarketAgg(label=quote.market))
                agg.add_price(quote.price_fen)

        checked: set[str] = set()
        for step in remaining_steps:
            patch = self._patch_for_step(manifest, step)
            operations, actual_bytes = self.archive.read_patch(patch)
            if actual_bytes != step.actual_bytes:
                raise SummaryError("更正包字节数与已核对链不一致")
            affected = _affected_slips(operations)
            if affected is None:
                return None  # package cannot be applied slip-by-slip
            before = document
            document = apply_patch(document, operations)
            if document_digest(document) != step.sha256:
                raise SummaryError(f"版本 {step.to_version} 局部重放摘要不符")
            for quotation_no in affected:
                self._update_slip(aggs, quotation_no, before, document, start, end)
            checked |= affected

        if document_digest(document) != chain_digests[-1]:
            raise SummaryError("局部更新后的文档与目标版摘要不符")

        groups = [
            aggs[name].to_group()
            for name in sorted(aggs, key=lambda value: str(value))
            if aggs[name].count > 0
        ]
        entry = SummaryEntry(
            version=target_version,
            month=month,
            groups=tuple(groups),
            chain_digests=chain_digests,
            total_quote_count=len(document),
        )
        self.cache.put(entry)
        return {
            "groups": groups,
            "mode": MODE_INCREMENTAL,
            "checked_quotation_count": len(checked),
            "base_version": candidate.version,
            "total_quote_count": len(document),
        }

    # ------------------------------------------------------------- replay helpers
    def _verified_document_at(
        self, manifest: Manifest, chain: list[ChainStep], step_index: int
    ) -> Any:
        """Reconstruct the verified document at ``chain[step_index]``.

        Base tables are served from the archive's verified-table cache, so a
        repeated read is cheap and remains covered by its mtime/size guard.
        """

        base = self._base_for_step(manifest, chain[0])
        document, _ = self.archive.read_base_table(base)
        if document_digest(document) != chain[0].sha256:
            raise SummaryError("局部重放起点底表摘要不符")
        for step in chain[1 : step_index + 1]:
            patch = self._patch_for_step(manifest, step)
            operations, _ = self.archive.read_patch(patch)
            document = apply_patch(document, operations)
            if document_digest(document) != step.sha256:
                raise SummaryError(f"版本 {step.to_version} 局部重放摘要不符")
        return document

    @staticmethod
    def _update_slip(
        aggs: dict[str, _MarketAgg],
        quotation_no: str,
        before: Any,
        after: Any,
        start: Any,
        end: Any,
    ) -> None:
        old_entry = before.get(quotation_no) if isinstance(before, dict) else None
        new_entry = after.get(quotation_no) if isinstance(after, dict) else None
        if old_entry is not None:
            old_quote = parse_quote(quotation_no, old_entry)
            if start <= old_quote.quoted_on <= end:
                old_agg = aggs.get(old_quote.market)
                if old_agg is None:
                    raise KeyError(old_quote.market)
                old_agg.remove_price(old_quote.price_fen)
        if new_entry is not None:
            new_quote = parse_quote(quotation_no, new_entry)
            if start <= new_quote.quoted_on <= end:
                new_agg = aggs.setdefault(
                    new_quote.market, _MarketAgg(label=new_quote.market)
                )
                new_agg.add_price(new_quote.price_fen)

    def _base_for_step(self, manifest: Manifest, step: ChainStep) -> BaseTable:
        for base in manifest.bases:
            if base.package.path == step.package:
                return base
        raise SummaryError(f"清单中找不到底表 {step.package}")

    def _patch_for_step(self, manifest: Manifest, step: ChainStep) -> PatchPackage:
        for patch in manifest.patches:
            if patch.package.path == step.package:
                return patch
        raise SummaryError(f"清单中找不到更正包 {step.package}")
