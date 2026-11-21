"""Build throwaway archives that exercise the chain-switching paths.

Used by ``tools/selftest.sh`` so the behaviour can be demonstrated against a
real HTTP server, not only unit tests.  Never writes inside ``sample_archive``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def copy_sample(destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ROOT / "sample_archive", destination)
    return destination


def break_missing_patch(destination: Path) -> None:
    """Delete the cheapest October patch: the resolver must switch chains."""

    (destination / "patches/patch-09-to-10.json").unlink()


def break_every_route(destination: Path) -> None:
    """Remove the newer base and every package: no chain can succeed."""

    (destination / "tables/table-2026-09.json").unlink()
    for patch in (destination / "patches").glob("*.json"):
        patch.unlink()


def corrupt_both_bases(destination: Path) -> None:
    """Make every base table parseable but with the wrong digest."""

    for name in ("table-2026-06.json", "table-2026-09.json"):
        path = destination / "tables" / name
        table = json.loads(path.read_text(encoding="utf-8"))
        first_key = min(table)
        table[first_key]["price_fen"] += 1
        path.write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


SCENARIOS = {
    "missing-patch": break_missing_patch,
    "no-chain": break_every_route,
    "bad-bases": corrupt_both_bases,
}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in SCENARIOS:
        print(
            f"usage: {argv[0]} {{{'|'.join(SCENARIOS)}}} DESTINATION",
            file=sys.stderr,
        )
        return 2
    destination = copy_sample(Path(argv[2]).resolve())
    SCENARIOS[argv[1]](destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
