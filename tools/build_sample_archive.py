"""Build the local sample archive under ``sample_archive/``.

The sample is a cumulative quotation ledger: each release version contains all
quotation slips published up to that month.  Corrections between releases are
small RFC 6902 packages, and there are two full base tables so that a release
can be reached by more than one chain.

This script is deterministic: running it again produces byte-identical output.
It is a development aid; the web application only ever reads the archive.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.jcs import canonicalize
from app.jsonpatch import apply_patch

SAMPLE = ROOT / "sample_archive"
TABLES = SAMPLE / "tables"
PATCHES = SAMPLE / "patches"

MARKETS = ["朝阳门菜市场", "海淀批发市场", "丰台农贸市场"]
PRODUCTS = [
    ("大白菜", "元/500克"),
    ("土豆", "元/500克"),
    ("西红柿", "元/500克"),
    ("黄瓜", "元/500克"),
    ("猪五花肉", "元/500克"),
]
MONTHS = ["2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]


def month_quotes(month: str) -> dict[str, Any]:
    year, mon = (int(part) for part in month.split("-"))
    entries: dict[str, Any] = {}
    seed = year * 100 + mon
    for day in (3, 10, 17, 24):
        for market_index, market in enumerate(MARKETS):
            for product_index, (product, unit) in enumerate(PRODUCTS):
                quotation_no = (
                    f"BJ{year}{mon:02d}{day:02d}-{market_index + 1}{product_index + 1:02d}"
                )
                price = (
                    180
                    + (seed + day * 7 + market_index * 37 + product_index * 53) % 900
                )
                entries[quotation_no] = {
                    "market": market,
                    "product": product,
                    "date": f"{year:04d}-{mon:02d}-{day:02d}",
                    "price_fen": price,
                    "unit": unit,
                    "currency": "CNY",
                }
    return entries


def cumulative() -> dict[str, dict[str, Any]]:
    """Cumulative ledgers, with two corrections published in later releases."""

    ledger: dict[str, Any] = {}
    tables: dict[str, dict[str, Any]] = {}
    for month in MONTHS:
        ledger = dict(ledger)
        ledger.update(month_quotes(month))
        if month == "2026-07":
            # A June quotation was keyed at the wrong price; the correction is
            # published with the July release and carried forward.
            ledger["BJ20260603-101"] = dict(
                ledger["BJ20260603-101"],
                price_fen=ledger["BJ20260603-101"]["price_fen"] + 25,
            )
        if month == "2026-09":
            # A September quotation is corrected, but only from October on.
            pass
        tables[month] = dict(ledger)
    # October release corrects a September quotation in addition to October data.
    october = dict(tables["2026-10"])
    october["BJ20260917-205"] = dict(
        october["BJ20260917-205"],
        price_fen=october["BJ20260917-205"]["price_fen"] + 13,
    )
    tables["2026-10"] = october
    return tables


def fixes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for key in sorted(old):
        if key not in new:
            operations.append({"op": "remove", "path": "/" + escape(key)})
        elif canonicalize(old[key]) != canonicalize(new[key]):
            operations.append(
                {"op": "replace", "path": "/" + escape(key), "value": new[key]}
            )
    for key in sorted(new):
        if key not in old:
            operations.append({"op": "add", "path": "/" + escape(key), "value": new[key]})
    return operations


def rewrite(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """A deliberately bulkier but equivalent package over the same range."""

    operations = [{"op": "remove", "path": "/" + escape(key)} for key in sorted(old)]
    operations += [
        {"op": "add", "path": "/" + escape(key), "value": value}
        for key, value in sorted(new.items())
    ]
    return operations


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def digest(document: Any) -> str:
    return hashlib.sha256(canonicalize(document)).hexdigest()


def main() -> int:
    for directory in (TABLES, PATCHES):
        directory.mkdir(parents=True, exist_ok=True)
        for existing in directory.glob("*.json"):
            existing.unlink()

    tables = cumulative()
    for version, table in tables.items():
        dump(TABLES / f"table-{version}.json", table)

    package_specs = [
        ("patch-06-to-07", "2026-06", "2026-07", fixes),
        ("patch-06-to-07-bulk", "2026-06", "2026-07", rewrite),
        ("patch-07-to-08", "2026-07", "2026-08", fixes),
        ("patch-08-to-09", "2026-08", "2026-09", fixes),
        ("patch-09-to-10", "2026-09", "2026-10", fixes),
        ("patch-06-to-08", "2026-06", "2026-08", fixes),
        ("patch-06-to-09", "2026-06", "2026-09", fixes),
        ("patch-06-to-10", "2026-06", "2026-10", fixes),
        ("patch-07-to-10", "2026-07", "2026-10", fixes),
    ]
    patch_entries: list[dict[str, Any]] = []
    for patch_id, from_version, to_version, builder in package_specs:
        path = PATCHES / f"{patch_id}.json"
        dump(path, builder(tables[from_version], tables[to_version]))
        patch_entries.append(
            {
                "id": patch_id,
                "path": str(path.relative_to(SAMPLE)),
                "bytes": path.stat().st_size,
                "from": from_version,
                "to": to_version,
            }
        )

    # Every package generated here really does produce its target version.
    for patch_id, from_version, to_version, _ in package_specs:
        patch_doc = json.loads((PATCHES / f"{patch_id}.json").read_text(encoding="utf-8"))
        produced = apply_patch(tables[from_version], patch_doc)
        assert canonicalize(produced) == canonicalize(tables[to_version]), patch_id

    manifest = {
        "name": "菜价月报底表归档（本地样例）",
        "generated_by": "tools/build_sample_archive.py",
        "note": "版本名使用发布月份；底表为累计报价台账；价格单位为分。",
        "digests": {version: digest(table) for version, table in tables.items()},
        "base_tables": [
            {
                "version": "2026-06",
                "path": "tables/table-2026-06.json",
                "bytes": (TABLES / "table-2026-06.json").stat().st_size,
            },
            {
                "version": "2026-09",
                "path": "tables/table-2026-09.json",
                "bytes": (TABLES / "table-2026-09.json").stat().st_size,
            },
        ],
        "patches": patch_entries,
        "releases": [
            {"version": version, "label": f"{version[:4]} 年 {int(version[5:])} 月月报"}
            for version in MONTHS
        ],
    }
    dump(SAMPLE / "manifest.json", manifest)

    print("sample archive written to", SAMPLE)
    print("canonical digests:")
    for version, value in manifest["digests"].items():
        print(f"  {version}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
