"""Tests for incremental monthly summaries and their four-entry LRU cache."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.archive import Archive, document_digest
from app.config import Settings
from app.jcs import canonicalize
from app.jsonpatch import apply_patch
from app.main import create_app
from app.pricing import build_report, filter_by_month, parse_quotes
from app.summary import (
    MAX_CACHED_SUMMARIES,
    MODE_CACHE_HIT,
    MODE_FULL,
    MODE_INCREMENTAL,
    SummaryCache,
    SummaryService,
    _build_entry,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_archive"


# --------------------------------------------------------------------- fixtures


def _digest(document: object) -> str:
    return document_digest(document)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _slip(market: str, product: str, day: str, price_fen: int) -> dict:
    return {
        "market": market,
        "product": product,
        "date": day,
        "price_fen": price_fen,
        "unit": "元/500克",
    }


def _diff_operations(old: dict, new: dict) -> list[dict]:
    ops: list[dict] = []
    for key in sorted(old):
        if key not in new:
            ops.append({"op": "remove", "path": "/" + key})
        elif canonicalize(old[key]) != canonicalize(new[key]):
            ops.append({"op": "replace", "path": "/" + key, "value": new[key]})
    for key in sorted(new):
        if key not in old:
            ops.append({"op": "add", "path": "/" + key, "value": new[key]})
    return ops


def build_two_version_archive(root: Path, base_table: dict, target_table: dict) -> Path:
    """Create a valid archive with one base (v1) and one patch (v1 -> v2)."""

    root.mkdir(parents=True, exist_ok=True)
    base_path = root / "tables" / "table-v1.json"
    patch_path = root / "patches" / "patch-v1-to-v2.json"
    operations = _diff_operations(base_table, target_table)
    # The operations must really produce the target table.
    assert canonicalize(apply_patch(base_table, operations)) == canonicalize(target_table)
    _write_json(base_path, base_table)
    _write_json(patch_path, operations)
    manifest = {
        "name": "汇总测试归档",
        "digests": {"v1": _digest(base_table), "v2": _digest(target_table)},
        "base_tables": [
            {"version": "v1", "path": "tables/table-v1.json", "bytes": base_path.stat().st_size}
        ],
        "patches": [
            {
                "id": "patch-v1-to-v2",
                "path": "patches/patch-v1-to-v2.json",
                "bytes": patch_path.stat().st_size,
                "from": "v1",
                "to": "v2",
            }
        ],
        "releases": [
            {"version": "v1", "label": "第一版"},
            {"version": "v2", "label": "第二版"},
        ],
    }
    _write_json(root / "manifest.json", manifest)
    return root


def make_service(root: Path) -> tuple[Archive, SummaryService]:
    archive = Archive(root)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    return archive, service


def resolved(archive: Archive, version: str):
    resolution = archive.resolve(version)
    assert resolution.ok, resolution.reason
    return resolution


def group_map(result) -> dict[str, tuple]:
    return {
        g.label: (g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
        for g in result.groups
    }


# ----------------------------------------------------------------- full vs. part


def test_full_recompute_matches_pricing_report(tmp_path):
    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    resolution = resolved(archive, "2026-10")
    result = service.summarize(
        manifest, "2026-10", "2026-10", resolution.document, resolution.chain
    )
    assert result.mode == MODE_FULL
    assert result.checked_quotation_count == len(resolution.document)

    quotes = filter_by_month(parse_quotes(resolution.document), "2026-10")
    expected = {
        g.label: (g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
        for g in build_report(quotes).by_market
    }
    assert group_map(result) == expected


def test_incremental_equals_full_for_every_sample_pair():
    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    months = ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    for month in months:
        for version in months:
            resolution = resolved(archive, version)
            result = service.summarize(
                manifest, version, month, resolution.document, resolution.chain
            )
            expected_entry = _build_entry(
                version,
                month,
                resolution.document,
                tuple(step.sha256 for step in resolution.chain),
            )
            assert [(g.label, g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
                    for g in result.groups] == [
                (g.label, g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
                for g in expected_entry.groups
            ]


# ------------------------------------------------------------- partial updates


def test_replace_add_remove_update_aggregates_incrementally(tmp_path):
    base = {
        "S1": _slip("甲市场", "白菜", "2026-06-01", 100),
        "S2": _slip("甲市场", "土豆", "2026-06-02", 200),
        "S3": _slip("乙市场", "黄瓜", "2026-06-03", 300),
        "S4": _slip("乙市场", "西红柿", "2026-07-01", 400),  # other month
    }
    target = {
        # price corrected
        "S1": _slip("甲市场", "白菜", "2026-06-01", 150),
        "S2": _slip("甲市场", "土豆", "2026-06-02", 200),
        "S3": _slip("乙市场", "黄瓜", "2026-06-03", 300),
        # new slip in the selected month
        "S5": _slip("乙市场", "茄子", "2026-06-05", 500),
        # S4 removed, but it belongs to July so June aggregates do not change
    }
    root = build_two_version_archive(tmp_path / "archive", base, target)
    archive, service = make_service(root)
    manifest = archive.load_manifest()

    first = resolved(archive, "v1")
    r1 = service.summarize(manifest, "v1", "2026-06", first.document, first.chain)
    assert r1.mode == MODE_FULL
    assert group_map(r1) == {
        "甲市场": (2, 300, "1.5000", 100, 200),
        "乙市场": (1, 300, "3.0000", 300, 300),
    }

    second = resolved(archive, "v2")
    r2 = service.summarize(manifest, "v2", "2026-06", second.document, second.chain)
    assert r2.mode == MODE_INCREMENTAL
    # S1, S4, S5 are the distinct top-level slips the correction package touches.
    assert r2.checked_quotation_count == 3
    assert r2.base_version == "v1"
    assert group_map(r2) == {
        "甲市场": (2, 350, "1.7500", 150, 200),
        "乙市场": (2, 800, "4.0000", 300, 500),
    }
    # A repeated request is served entirely from the cache.
    r3 = service.summarize(manifest, "v2", "2026-06", second.document, second.chain)
    assert r3.mode == MODE_CACHE_HIT
    assert r3.checked_quotation_count == 0
    assert group_map(r3) == group_map(r2)


def test_market_and_month_change_moves_between_groups(tmp_path):
    base = {
        "A1": _slip("甲市场", "白菜", "2026-06-01", 100),
        "A2": _slip("甲市场", "土豆", "2026-06-02", 200),
        "B1": _slip("乙市场", "黄瓜", "2026-06-03", 300),
    }
    target = {
        # A1 moves to another market; A2 moves to the following month.
        "A1": _slip("乙市场", "白菜", "2026-06-01", 100),
        "A2": _slip("甲市场", "土豆", "2026-07-02", 200),
        "B1": _slip("乙市场", "黄瓜", "2026-06-03", 300),
    }
    root = build_two_version_archive(tmp_path / "archive", base, target)
    archive, service = make_service(root)
    manifest = archive.load_manifest()
    first = resolved(archive, "v1")
    service.summarize(manifest, "v1", "2026-06", first.document, first.chain)
    second = resolved(archive, "v2")
    result = service.summarize(
        manifest, "v2", "2026-06", second.document, second.chain
    )
    assert result.mode == MODE_INCREMENTAL
    assert group_map(result) == {
        # 甲市场 loses both slips (A1 moved market, A2 moved month)
        "乙市场": (2, 400, "2.0000", 100, 300),
    }
    # The full recomputation of the target table must say exactly the same.
    full = _build_entry(
        "v2", "2026-06", second.document, tuple(s.sha256 for s in second.chain)
    )
    assert [g.label for g in result.groups] == [g.label for g in full.groups]
    assert group_map(result) == {
        g.label: (g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
        for g in full.groups
    }


def test_deleting_the_extremum_keeps_min_and_max_exact(tmp_path):
    base = {
        "Q1": _slip("甲市场", "白菜", "2026-06-01", 100),  # minimum
        "Q2": _slip("甲市场", "土豆", "2026-06-02", 250),
        "Q3": _slip("甲市场", "黄瓜", "2026-06-03", 400),
        "Q4": _slip("甲市场", "西红柿", "2026-06-04", 900),  # maximum
    }
    target = {
        "Q2": _slip("甲市场", "土豆", "2026-06-02", 250),
        "Q3": _slip("甲市场", "黄瓜", "2026-06-03", 400),
    }
    root = build_two_version_archive(tmp_path / "archive", base, target)
    archive, service = make_service(root)
    manifest = archive.load_manifest()
    first = resolved(archive, "v1")
    service.summarize(manifest, "v1", "2026-06", first.document, first.chain)
    second = resolved(archive, "v2")
    result = service.summarize(
        manifest, "v2", "2026-06", second.document, second.chain
    )
    assert result.mode == MODE_INCREMENTAL
    assert group_map(result) == {
        "甲市场": (2, 650, "3.2500", 250, 400),
    }


def test_empty_month_is_represented_without_groups(tmp_path):
    base = {"Q1": _slip("甲市场", "白菜", "2026-06-01", 100)}
    root = build_two_version_archive(tmp_path / "archive", base, dict(base))
    archive, service = make_service(root)
    manifest = archive.load_manifest()
    resolution = resolved(archive, "v1")
    result = service.summarize(
        manifest, "v1", "2026-08", resolution.document, resolution.chain
    )
    assert result.groups == []
    assert result.checked_quotation_count == 1


# ---------------------------------------------------------------- cache semantics


def test_service_retains_the_injected_cache():
    # Regression: an empty cache is falsy (__len__ == 0), so "cache or ..."
    # would silently replace it; the service must keep the instance it is given.
    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    cache = SummaryCache()
    service = SummaryService(archive, cache)
    assert service.cache is cache
    resolution = resolved(archive, "2026-10")
    service.summarize(
        manifest, "2026-10", "2026-10", resolution.document, resolution.chain
    )
    assert cache.keys() == [("2026-10", "2026-10")]


def test_poisoned_digest_forces_full_recompute_then_recaches():
    from app.summary import SummaryEntry, MODE_CACHE_HIT

    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    cache = SummaryCache()
    service = SummaryService(archive, cache)
    resolution = resolved(archive, "2026-10")
    service.summarize(
        manifest, "2026-10", "2026-10", resolution.document, resolution.chain
    )
    good = cache.get("2026-10", "2026-10")
    cache.put(
        SummaryEntry(
            version="2026-10",
            month="2026-10",
            groups=good.groups,
            chain_digests=("0" * 64,),
            total_quote_count=good.total_quote_count,
        )
    )
    poisoned = service.summarize(
        manifest, "2026-10", "2026-10", resolution.document, resolution.chain
    )
    assert poisoned.mode == MODE_FULL
    rebuilt = service.summarize(
        manifest, "2026-10", "2026-10", resolution.document, resolution.chain
    )
    assert rebuilt.mode == MODE_CACHE_HIT
    assert group_map(rebuilt) == group_map(poisoned)


def test_cache_keeps_at_most_four_entries_lru():
    cache = SummaryCache()
    assert cache.capacity == MAX_CACHED_SUMMARIES == 4
    for index in range(6):
        cache.put(
            _build_entry(
                f"v{index}",
                "2026-06",
                {},
                (f"d{index}",),
            )
        )
    assert len(cache) == 4
    # The two oldest entries were evicted; newest four remain.
    assert cache.keys() == [
        ("v2", "2026-06"),
        ("v3", "2026-06"),
        ("v4", "2026-06"),
        ("v5", "2026-06"),
    ]
    # Touching an entry refreshes its LRU position.
    cache.get("v2", "2026-06")
    cache.put(_build_entry("v6", "2026-06", {}, ("d6",)))
    assert ("v2", "2026-06") in cache.keys()
    assert ("v3", "2026-06") not in cache.keys()


def test_cache_reused_only_when_digests_match_sample_chain():
    archive = Archive(SAMPLE)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    # September summary on the chain based on the September full table.
    sep = resolved(archive, "2026-09")
    r_sep = service.summarize(
        manifest, "2026-09", "2026-09", sep.document, sep.chain
    )
    assert r_sep.mode == MODE_FULL
    # October adds one corrected September slip plus 60 October slips; the
    # September summary only needs the corrected slip, so it is incremental
    # with a small checked count instead of all 300 slips.
    october = resolved(archive, "2026-10")
    r_oct = service.summarize(
        manifest, "2026-10", "2026-09", october.document, october.chain
    )
    assert r_oct.mode == MODE_INCREMENTAL
    assert r_oct.base_version == "2026-09"
    assert r_oct.checked_quotation_count < len(october.document)
    full = _build_entry(
        "2026-10",
        "2026-09",
        october.document,
        tuple(step.sha256 for step in october.chain),
    )
    assert group_map(r_oct) == {
        g.label: (g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
        for g in full.groups
    }


def test_tampered_chain_rejects_cached_summary_and_full_recomputes(tmp_path):
    root = tmp_path / "archive"
    shutil.copytree(SAMPLE, root)
    archive = Archive(root)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    october = resolved(archive, "2026-10")
    first = service.summarize(
        manifest, "2026-10", "2026-10", october.document, october.chain
    )
    assert first.mode == MODE_FULL

    # Tamper with the September base table: verification fails on that chain and
    # the resolver switches to the June-based chain, whose digests differ.
    table_path = root / "tables" / "table-2026-09.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    first_key = min(table)
    table[first_key]["price_fen"] += 1
    table_path.write_text(
        json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    fresh_archive = Archive(root)
    fresh_manifest = fresh_archive.load_manifest()
    fresh_service = SummaryService(fresh_archive, service.cache)
    switched = resolved(fresh_archive, "2026-10")
    assert switched.chain[0].package == "tables/table-2026-06.json"
    result = fresh_service.summarize(
        fresh_manifest, "2026-10", "2026-10", switched.document, switched.chain
    )
    # Digest subsequence no longer matches -> no reuse, full recompute.
    assert result.mode == MODE_FULL
    assert result.checked_quotation_count == len(switched.document)
    expected = _build_entry(
        "2026-10",
        "2026-10",
        switched.document,
        tuple(step.sha256 for step in switched.chain),
    )
    assert group_map(result) == {
        g.label: (g.count, g.total_fen, g.average_yuan, g.min_fen, g.max_fen)
        for g in expected.groups
    }


def test_failed_resolution_never_touches_the_cache(tmp_path):
    root = tmp_path / "archive"
    shutil.copytree(SAMPLE, root)
    archive = Archive(root)
    manifest = archive.load_manifest()
    service = SummaryService(archive, SummaryCache())
    ok = resolved(archive, "2026-09")
    service.summarize(manifest, "2026-09", "2026-09", ok.document, ok.chain)
    assert len(service.cache) == 1

    # Break every route to October and confirm resolution fails without
    # creating any additional cache entry.
    (root / "tables/table-2026-09.json").unlink()
    for patch in (root / "patches").glob("*.json"):
        patch.unlink()
    broken = Archive(root)
    bad = broken.resolve("2026-10")
    assert not bad.ok
    assert len(service.cache) == 1
    assert service.cache.keys() == [("2026-09", "2026-09")]


def test_root_level_patch_forces_full_recompute(tmp_path, monkeypatch):
    base = {"Q1": _slip("甲市场", "白菜", "2026-06-01", 100)}
    target = dict(base)
    target["Q2"] = _slip("甲市场", "土豆", "2026-06-02", 200)
    root = build_two_version_archive(tmp_path / "archive", base, target)
    archive, service = make_service(root)
    manifest = archive.load_manifest()
    first = resolved(archive, "v1")
    service.summarize(manifest, "v1", "2026-06", first.document, first.chain)
    second = resolved(archive, "v2")

    # Simulate a package that cannot be applied slip-by-slip (e.g. it mutates
    # the document root): incremental replay must give way to a full recompute.
    import app.summary as summary_module

    monkeypatch.setattr(summary_module, "_affected_slips", lambda operations: None)
    result = service.summarize(
        manifest, "v2", "2026-06", second.document, second.chain
    )
    assert result.mode == MODE_FULL
    assert result.checked_quotation_count == 2
    assert group_map(result) == {
        "甲市场": (2, 300, "1.5000", 100, 200),
    }


def test_affected_slips_classification():
    from app.summary import _affected_slips

    # test operations do not mutate anything.
    assert _affected_slips([{"op": "test", "path": ""}]) == set()
    assert _affected_slips([{"op": "add", "path": "/Q2", "value": {}}]) == {"Q2"}
    assert _affected_slips([{"op": "move", "from": "/Q1", "path": "/Q2"}]) == {
        "Q1",
        "Q2",
    }
    # Document-root or malformed pointers cannot be mapped to a single slip.
    assert _affected_slips([{"op": "remove", "path": ""}]) is None
    assert _affected_slips([{"op": "copy", "from": "", "path": "/Q2"}]) is None


# ----------------------------------------------------------------------- web


def make_client(archive_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(archive_dir=archive_dir)))


def test_summary_page_modes_and_checked_count():
    client = make_client(SAMPLE)
    page = client.get("/release/2026-10/summary?month=2026-10")
    assert page.status_code == 200
    assert "全量复算" in page.text
    assert "本次为汇总检查了 <strong>300</strong> 个不同单号" in page.text
    again = client.get("/release/2026-10/summary?month=2026-10")
    assert "缓存命中" in again.text
    assert "检查了 <strong>0</strong> 个不同单号" in again.text


def test_summary_quick_entry_is_on_release_page():
    client = make_client(SAMPLE)
    page = client.get("/release/2026-10")
    assert "/release/2026-10/summary" in page.text
    assert "同月汇总快捷核对" in page.text


def test_summary_rejects_bad_or_missing_month():
    client = make_client(SAMPLE)
    assert client.get("/release/2026-10/summary").status_code == 400
    assert client.get("/release/2026-10/summary?month=2026-13").status_code == 400
    assert client.get("/release/2026-10/summary?month=nope").status_code == 400


def test_summary_unknown_release_is_404():
    client = make_client(SAMPLE)
    assert client.get("/release/2026-01/summary?month=2026-10").status_code == 404


def test_summary_on_failed_resolution_only_reports_reason(tmp_path):
    root = tmp_path / "archive"
    shutil.copytree(SAMPLE, root)
    (root / "tables/table-2026-09.json").unlink()
    for patch in (root / "patches").glob("*.json"):
        patch.unlink()
    client = make_client(root)
    response = client.get("/release/2026-10/summary?month=2026-10")
    assert response.status_code == 422
    assert "目标版未通过核对，只报原因，不出汇总" in response.text
    assert "各市场报价汇总" not in response.text


def test_summary_page_tampered_archive_falls_back(tmp_path):
    root = tmp_path / "archive"
    shutil.copytree(SAMPLE, root)
    client = make_client(root)
    assert client.get("/release/2026-10/summary?month=2026-10").status_code == 200
    table_path = root / "tables" / "table-2026-09.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    table[min(table)]["price_fen"] += 1
    table_path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    response = client.get("/release/2026-10/summary?month=2026-10")
    assert response.status_code == 200
    # New process-level client -> empty cache; chain switched, still full mode.
    assert "全量复算" in response.text
    assert "摘要不符" in response.text  # attempts panel shows the broken base
