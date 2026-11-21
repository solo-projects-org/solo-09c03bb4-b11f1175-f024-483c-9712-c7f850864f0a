"""Archive model: manifest parsing, package verification and chain resolution.

The archive is a read-only directory containing one manifest, one or more
full base tables ("底表") and RFC 6902 correction packages.  A release is
reconstructed by starting from a verified base table and applying patches
along a chain whose total package size (as declared by the manifest) is
minimal.

Integrity model
---------------
* The manifest lists every package's byte size and the ``from``/``to``
  version of every correction package.
* The manifest lists, for every version, the SHA-256 of that version's table
  after RFC 8785 canonicalization.
* A package is read only when a candidate chain needs it.  Every resulting
  version is canonicalized and its digest compared with the manifest.  A
  missing file, malformed JSON/patch or digest mismatch invalidates the whole
  candidate chain; the resolver then tries the next-cheapest chain, reusing
  only base tables whose digest was already verified.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jcs import CanonicalizationError, canonicalize
from .jsonpatch import PatchError, apply_patch

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_CHAIN_ATTEMPTS = 200
MAX_SEARCH_STEPS = 200_000


class ArchiveError(Exception):
    """Base class for archive problems."""


class ManifestError(ArchiveError):
    """The manifest is missing or does not have the expected shape."""


class PackageError(ArchiveError):
    """A package is missing, unreadable or malformed at the file level."""

    def __init__(self, package: str, kind: str, detail: str):
        super().__init__(f"{package}: {kind}: {detail}")
        self.package = package
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class PackageRef:
    path: str
    bytes: int


@dataclass(frozen=True)
class BaseTable:
    version: str
    package: PackageRef
    sha256: str | None


@dataclass(frozen=True)
class PatchPackage:
    id: str
    package: PackageRef
    from_version: str
    to_version: str
    sha256: str | None = None


@dataclass(frozen=True)
class Release:
    version: str
    label: str


@dataclass
class Manifest:
    name: str
    bases: list[BaseTable]
    patches: list[PatchPackage]
    digests: dict[str, str]
    releases: list[Release]
    warnings: list[str] = field(default_factory=list)

    @property
    def versions(self) -> list[str]:
        known = set(self.digests)
        known |= {b.version for b in self.bases}
        known |= {p.from_version for p in self.patches}
        known |= {p.to_version for p in self.patches}
        return sorted(known, key=natural_key)

    def package_count(self) -> int:
        return len(self.bases) + len(self.patches)

    def total_bytes(self) -> int:
        return sum(b.package.bytes for b in self.bases) + sum(
            p.package.bytes for p in self.patches
        )


@dataclass
class ChainStep:
    kind: str  # "base" | "patch"
    package: str
    package_id: str
    declared_bytes: int
    actual_bytes: int | None
    from_version: str
    to_version: str
    sha256: str
    verified: bool = False

    @property
    def size_mismatch(self) -> bool:
        return self.actual_bytes is not None and self.actual_bytes != self.declared_bytes


@dataclass
class Issue:
    package: str
    package_id: str
    kind: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "package_id": self.package_id,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass
class Attempt:
    chain: list[ChainStep]
    ok: bool
    reason: str = ""
    issues: list[Issue] = field(default_factory=list)
    document: Any = None
    planned_versions: list[str] = field(default_factory=list)

    @property
    def cost(self) -> int:
        return sum(s.declared_bytes for s in self.chain)


@dataclass
class Resolution:
    target: str
    ok: bool
    chain: list[ChainStep] = field(default_factory=list)
    package_bytes: int = 0
    issues: list[Issue] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    reason: str = ""
    document: Any = None
    search_truncated: bool = False

    @property
    def used_packages(self) -> list[ChainStep]:
        return [step for step in self.chain if step.verified]


@dataclass
class _Plan:
    base: BaseTable
    patches: tuple[PatchPackage, ...]
    cost: int


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_digest(document: Any) -> str:
    return sha256_hex(canonicalize(document))


def normalize_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    text = text.removeprefix("sha256:")
    return text if HEX64.match(text) else None


class Archive:
    """A mounted, read-only archive."""

    def __init__(
        self, root: str | os.PathLike[str], manifest_name: str = "manifest.json"
    ):
        self.root = Path(root).resolve()
        self.manifest_name = manifest_name
        self._manifest: Manifest | None = None
        self._manifest_error: str | None = None
        # Verified base tables, keyed by (version, package path).  Only fully
        # verified entries are ever inserted.  The stored stat signature lets
        # a later call notice that the file changed on disk.
        self._verified_bases: dict[
            tuple[str, str], tuple[Any, str, int, tuple[int, int]]
        ] = {}
        self._failed_bases: dict[tuple[str, str], Issue] = {}
        self._failed_patches: dict[tuple[str, str], Issue] = {}

    # ------------------------------------------------------------------ manifest
    def load_manifest(self, *, refresh: bool = False) -> Manifest:
        if self._manifest is not None and not refresh:
            return self._manifest
        self._manifest = None
        self._manifest_error = None
        try:
            path = self._resolve_manifest_path()
            raw = self._read_json_file(path)
            manifest = parse_manifest(raw, manifest_name=self.manifest_name)
        except (ArchiveError, OSError, ValueError) as exc:
            self._manifest_error = str(exc)
            raise ManifestError(self._manifest_error) from exc
        self._manifest = manifest
        return manifest

    @property
    def manifest_error(self) -> str | None:
        return self._manifest_error

    def _resolve_manifest_path(self) -> Path:
        if not self.root.is_dir():
            raise ManifestError(f"归档目录不存在: {self.root}")
        candidate = (self.root / self.manifest_name).resolve()
        self._ensure_within_root(candidate)
        if not candidate.is_file():
            raise ManifestError(f"找不到清单文件: {self.manifest_name}")
        return candidate

    def _ensure_within_root(self, candidate: Path) -> None:
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PackageError(candidate.name, "路径无效", "路径越出归档目录") from exc

    def _package_path(self, relative: str) -> Path:
        if not relative or relative.startswith(("/", "\\")):
            raise PackageError(relative or "<空>", "路径无效", "包路径必须是相对路径")
        candidate = (self.root / relative).resolve()
        self._ensure_within_root(candidate)
        return candidate

    # ------------------------------------------------------------------ package IO
    def _read_package_bytes(self, relative: str) -> bytes:
        path = self._package_path(relative)
        if not path.is_file():
            raise PackageError(relative, "缺包", "文件不存在")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PackageError(relative, "无法读取", str(exc)) from exc

    def _read_json_file(self, path: Path) -> Any:
        if not path.is_file():
            raise ManifestError(f"找不到清单: {path.name}")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ManifestError(f"无法读取清单: {exc}") from exc
        return parse_json_bytes(raw, label=path.name)

    def read_base_table(self, base: BaseTable) -> tuple[Any, int]:
        raw = self._read_package_bytes(base.package.path)
        return parse_json_bytes(raw, label=base.package.path), len(raw)

    def read_patch(self, patch: PatchPackage) -> tuple[list[Any], int]:
        raw = self._read_package_bytes(patch.package.path)
        document = parse_json_bytes(raw, label=patch.package.path)
        if isinstance(document, dict):
            for key in ("patch", "operations", "ops"):
                if isinstance(document.get(key), list):
                    document = document[key]
                    break
            else:
                raise PatchError("更正包不是 RFC 6902 操作数组")
        if not isinstance(document, list):
            raise PatchError("更正包不是 RFC 6902 操作数组")
        return document, len(raw)

    # ------------------------------------------------------------------ resolution
    def resolve(self, target: str) -> Resolution:
        try:
            manifest = self.load_manifest()
        except ManifestError as exc:
            return Resolution(target=str(target), ok=False, reason=str(exc))

        # Failures are cached only for the duration of one resolution so that
        # a repaired archive is picked up by the next request.  Verified base
        # tables remain usable: re-reading them is unnecessary.
        self._failed_bases.clear()
        self._failed_patches.clear()

        target = str(target)
        resolution = Resolution(target=target, ok=False)
        if target not in manifest.versions:
            resolution.reason = f"清单中没有版本 {target}。"
            return resolution

        plans = self._iter_plans(manifest, target)
        for plan in plans:
            if len(resolution.attempts) >= MAX_CHAIN_ATTEMPTS:
                resolution.search_truncated = True
                resolution.reason = (
                    f"已尝试 {MAX_CHAIN_ATTEMPTS} 条候选链，仍未找到可用链。"
                    f" 最后一个问题：{resolution.reason}"
                )
                return resolution
            attempt = self._execute_plan(plan)
            resolution.attempts.append(attempt)
            resolution.issues.extend(attempt.issues)
            if attempt.ok:
                resolution.ok = True
                resolution.chain = attempt.chain
                resolution.package_bytes = sum(
                    step.declared_bytes for step in attempt.chain
                )
                resolution.document = attempt.document
                if resolution.issues:
                    resolution.reason = (
                        f"已换链：先前 {len(resolution.issues)} 个问题包未采用。"
                    )
                return resolution
            resolution.reason = attempt.reason

        if not resolution.attempts:
            resolution.reason = self._no_chain_reason(manifest, target)
        else:
            problem = self._summarize_issues(resolution.issues)
            resolution.reason = (
                f"已尝试 {len(resolution.attempts)} 条候选链，均不可用。"
                + (f" 问题包：{problem}。" if problem else "")
                + f" 最后原因：{resolution.reason}"
            )
        return resolution

    def _summarize_issues(self, issues: list[Issue]) -> str:
        """A compact, de-duplicated list of the packages that failed."""

        seen: list[str] = []
        for issue in issues:
            label = f"{issue.package}（{issue.kind}）"
            if label not in seen:
                seen.append(label)
        return "、".join(seen)

    def _iter_plans(self, manifest: Manifest, target: str) -> Iterator[_Plan]:
        """Yield reconstruction plans in nondecreasing total package bytes.

        The search is Dijkstra-style over versions, with a per-path visited set
        so cycles are not followed.  Ties are broken by patch count and a
        stable key so results are deterministic.
        """

        edges: dict[str, list[PatchPackage]] = {}
        for patch in manifest.patches:
            edges.setdefault(patch.from_version, []).append(patch)
        for patches in edges.values():
            patches.sort(key=lambda p: (p.package.bytes, p.id, p.package.path))

        counter = itertools.count()
        queue: list[tuple[int, int, int, str, frozenset[str], BaseTable, tuple[PatchPackage, ...]]] = []
        for base in sorted(
            manifest.bases, key=lambda b: (b.package.bytes, b.version, b.package.path)
        ):
            heapq.heappush(
                queue,
                (
                    base.package.bytes,
                    0,
                    next(counter),
                    base.version,
                    frozenset({base.version}),
                    base,
                    (),
                ),
            )

        steps = 0
        while queue and steps < MAX_SEARCH_STEPS:
            steps += 1
            cost, count, _, version, visited, base, path = heapq.heappop(queue)
            if version == target:
                yield _Plan(base=base, patches=path, cost=cost)
                continue
            for patch in edges.get(version, ()):
                if patch.to_version in visited:
                    continue
                heapq.heappush(
                    queue,
                    (
                        cost + patch.package.bytes,
                        count + 1,
                        next(counter),
                        patch.to_version,
                        visited | {patch.to_version},
                        base,
                        path + (patch,),
                    ),
                )

    def _no_chain_reason(self, manifest: Manifest, target: str) -> str:
        if not manifest.bases:
            return "清单中没有全量底表，无法起步。"
        reachable: set[str] = {b.version for b in manifest.bases}
        changed = True
        while changed:
            changed = False
            for patch in manifest.patches:
                if patch.from_version in reachable and patch.to_version not in reachable:
                    reachable.add(patch.to_version)
                    changed = True
        if target in reachable:
            return f"版本 {target} 的候选链均无法执行。"
        starts = "、".join(sorted({b.version for b in manifest.bases}, key=natural_key))
        return (
            f"清单中的所有全量底表（{starts}）都无法通过更正包到达版本 {target}。"
        )

    def _expected_digest(
        self, manifest: Manifest, version: str, fallback: str | None = None
    ) -> str | None:
        return normalize_digest(manifest.digests.get(version, fallback))

    def _stat_signature(self, base: BaseTable) -> tuple[int, int]:
        """A cheap change detector for cached, already-verified base tables."""

        try:
            stat = self._package_path(base.package.path).stat()
        except (OSError, PackageError):
            return (-1, -1)
        return (stat.st_mtime_ns, stat.st_size)

    def _execute_plan(self, plan: _Plan) -> Attempt:
        manifest = self._manifest
        assert manifest is not None
        attempt = Attempt(chain=[], ok=False)
        attempt.planned_versions = [plan.base.version] + [
            patch.to_version for patch in plan.patches
        ]
        base = plan.base
        base_key = (base.version, base.package.path)

        expected_base = self._expected_digest(manifest, base.version, base.sha256)
        if expected_base is None:
            attempt.issues.append(
                Issue(
                    base.package.path,
                    f"base:{base.version}",
                    "摘要缺失",
                    f"清单未记录版本 {base.version} 的 SHA-256",
                )
            )
            attempt.reason = f"版本 {base.version} 的底表没有清单摘要，无法核对"
            return attempt
        if base_key in self._failed_bases:
            attempt.issues.append(self._failed_bases[base_key])
            attempt.reason = f"底表 {base.package.path} 已确认不可用"
            return attempt

        if base_key in self._verified_bases:
            document, digest, actual_bytes, signature = self._verified_bases[base_key]
            if digest != expected_base or self._stat_signature(base) != signature:
                # The manifest or the file changed under us; force a re-read.
                del self._verified_bases[base_key]
                return self._execute_plan(plan)
        else:
            try:
                document, actual_bytes = self.read_base_table(base)
                digest = document_digest(document)
            except (PackageError, PatchError, CanonicalizationError, ValueError, OSError) as exc:
                issue = _issue_from_exception(exc, base.package.path, f"base:{base.version}")
                self._failed_bases[base_key] = issue
                attempt.issues.append(issue)
                attempt.reason = f"全量底表 {base.package.path} 不可用（{issue.kind}）"
                return attempt
            if digest != expected_base:
                issue = Issue(
                    base.package.path,
                    f"base:{base.version}",
                    "摘要不符",
                    f"版本 {base.version} 期望 {expected_base}，实际 {digest}",
                )
                self._failed_bases[base_key] = issue
                attempt.issues.append(issue)
                attempt.reason = f"全量底表 {base.package.path} 摘要不符"
                return attempt
            if actual_bytes != base.package.bytes:
                issue = Issue(
                    base.package.path,
                    f"base:{base.version}",
                    "字节数不符",
                    f"清单声明 {base.package.bytes} 字节，实际 {actual_bytes} 字节",
                )
                self._failed_bases[base_key] = issue
                attempt.issues.append(issue)
                attempt.reason = f"全量底表 {base.package.path} 字节数与清单不符"
                return attempt
            self._verified_bases[base_key] = (
                document,
                digest,
                actual_bytes,
                self._stat_signature(base),
            )

        base_step = ChainStep(
            kind="base",
            package=base.package.path,
            package_id=f"base:{base.version}",
            declared_bytes=base.package.bytes,
            actual_bytes=actual_bytes,
            from_version=base.version,
            to_version=base.version,
            sha256=expected_base,
            verified=True,
        )
        attempt.chain.append(base_step)

        current_version = base.version
        for patch in plan.patches:
            expected = self._expected_digest(manifest, patch.to_version)
            if expected is None:
                attempt.issues.append(
                    Issue(
                        patch.package.path,
                        patch.id,
                        "摘要缺失",
                        f"清单未记录版本 {patch.to_version} 的 SHA-256",
                    )
                )
                attempt.reason = f"版本 {patch.to_version} 缺少清单摘要，无法核对"
                return attempt
            key = (patch.from_version + "->" + patch.to_version, patch.package.path)
            if key in self._failed_patches:
                attempt.issues.append(self._failed_patches[key])
                attempt.reason = f"更正包 {patch.package.path} 已确认不可用"
                return attempt
            try:
                operations, actual_bytes = self.read_patch(patch)
                patched = apply_patch(document, operations)
                digest = document_digest(patched)
            except (PackageError, PatchError, CanonicalizationError, ValueError, OSError) as exc:
                issue = _issue_from_exception(exc, patch.package.path, patch.id)
                self._failed_patches[key] = issue
                attempt.issues.append(issue)
                attempt.reason = (
                    f"更正包 {patch.package.path} 不可用（{issue.kind}），"
                    f"未能得到版本 {patch.to_version}"
                )
                return attempt
            if digest != expected:
                issue = Issue(
                    patch.package.path,
                    patch.id,
                    "摘要不符",
                    f"版本 {patch.to_version} 期望 {expected}，实际 {digest}",
                )
                self._failed_patches[key] = issue
                attempt.issues.append(issue)
                attempt.reason = (
                    f"更正包 {patch.package.path} 应用后版本 {patch.to_version} 摘要不符"
                )
                return attempt
            if actual_bytes != patch.package.bytes:
                issue = Issue(
                    patch.package.path,
                    patch.id,
                    "字节数不符",
                    f"清单声明 {patch.package.bytes} 字节，实际 {actual_bytes} 字节",
                )
                self._failed_patches[key] = issue
                attempt.issues.append(issue)
                attempt.reason = (
                    f"更正包 {patch.package.path} 字节数与清单不符"
                )
                return attempt

            step = ChainStep(
                kind="patch",
                package=patch.package.path,
                package_id=patch.id,
                declared_bytes=patch.package.bytes,
                actual_bytes=actual_bytes,
                from_version=patch.from_version,
                to_version=patch.to_version,
                sha256=digest,
                verified=True,
            )
            attempt.chain.append(step)
            document = patched
            current_version = patch.to_version

        if plan.patches and current_version != plan.patches[-1].to_version:
            attempt.reason = "候选链内部不一致"
            return attempt
        attempt.ok = True
        attempt.document = document
        return attempt


def _issue_from_exception(exc: Exception, package: str, package_id: str) -> Issue:
    if isinstance(exc, PackageError):
        kind, detail = exc.kind, exc.detail
    elif isinstance(exc, PatchError):
        kind, detail = "坏包", f"更正包无效: {exc}"
    elif isinstance(exc, CanonicalizationError):
        kind, detail = "坏包", f"无法规范化: {exc}"
    else:
        kind, detail = "坏包", str(exc)
    return Issue(package, package_id, kind, detail)


# ---------------------------------------------------------------------- parsing


def _first(mapping: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _coerce_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManifestError(f"{label} 必须是非负整数")
    return value


def _package_ref(entry: dict[str, Any], label: str) -> PackageRef:
    path = _first(entry, ("path", "file", "filename", "package_file"))
    if not isinstance(path, str) or not path.strip():
        raise ManifestError(f"{label} 缺少 path")
    size = _first(entry, ("bytes", "size", "byte_size", "byteLength", "length"))
    if size is None:
        raise ManifestError(f"{label} 缺少字节数")
    return PackageRef(path=path.strip(), bytes=_coerce_int(size, f"{label} 字节数"))


def parse_manifest(raw: Any, manifest_name: str = "manifest.json") -> Manifest:
    if not isinstance(raw, dict):
        raise ManifestError("清单必须是 JSON 对象")

    name = raw.get("name") or raw.get("title") or manifest_name
    if not isinstance(name, str):
        name = manifest_name

    warnings: list[str] = []
    digests: dict[str, str] = {}

    digest_source = _first(raw, ("digests", "version_digests", "sha256_by_version"))
    if isinstance(digest_source, dict):
        for version, digest in digest_source.items():
            normalized = normalize_digest(digest)
            if normalized is None:
                raise ManifestError(f"版本 {version} 的 SHA-256 格式无效")
            digests[str(version)] = normalized
    elif digest_source is not None:
        raise ManifestError("digests 必须是版本到 SHA-256 的对象")

    versions_source = raw.get("versions")
    if isinstance(versions_source, dict):
        for version, entry in versions_source.items():
            digest = (
                entry
                if isinstance(entry, str)
                else (
                    _first(entry, ("sha256", "digest", "canonical_sha256", "hash"))
                    if isinstance(entry, dict)
                    else None
                )
            )
            normalized = normalize_digest(digest)
            if normalized is None:
                raise ManifestError(f"版本 {version} 的 SHA-256 格式无效")
            digests.setdefault(str(version), normalized)
    elif isinstance(versions_source, list):
        for index, entry in enumerate(versions_source):
            if not isinstance(entry, dict):
                raise ManifestError(f"versions[{index}] 必须是对象")
            version = _first(entry, ("version", "name", "id"))
            if version is None:
                raise ManifestError(f"versions[{index}] 缺少 version")
            digest = normalize_digest(
                _first(entry, ("sha256", "digest", "canonical_sha256", "hash"))
            )
            if digest is None:
                raise ManifestError(f"versions[{index}] 的 SHA-256 格式无效")
            digests.setdefault(str(version), digest)
    elif versions_source is not None:
        raise ManifestError("versions 必须是对象或数组")

    base_source = _first(raw, ("base_tables", "bases", "full_tables", "baseTables"))
    if not isinstance(base_source, list) or not base_source:
        raise ManifestError("清单缺少全量底表列表 base_tables")
    bases: list[BaseTable] = []
    for index, entry in enumerate(base_source):
        if not isinstance(entry, dict):
            raise ManifestError(f"base_tables[{index}] 必须是对象")
        version = _first(entry, ("version", "release", "id"))
        if version is None:
            raise ManifestError(f"base_tables[{index}] 缺少 version")
        version = str(version)
        ref = _package_ref(entry, f"base_tables[{index}]")
        digest = normalize_digest(
            _first(entry, ("sha256", "digest", "canonical_sha256", "hash"))
        )
        if digest is not None:
            existing = digests.get(version)
            if existing is not None and existing != digest:
                raise ManifestError(f"版本 {version} 的 SHA-256 前后不一致")
            digests[version] = digest
        bases.append(BaseTable(version=version, package=ref, sha256=digest))

    patch_source = _first(
        raw, ("patches", "corrections", "correction_packages", "patch_packages")
    )
    if patch_source is None:
        patch_source = []
    if not isinstance(patch_source, list):
        raise ManifestError("patches 必须是数组")
    patches: list[PatchPackage] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(patch_source):
        if not isinstance(entry, dict):
            raise ManifestError(f"patches[{index}] 必须是对象")
        from_version = _first(
            entry, ("from", "from_version", "fromVersion", "base_version", "source")
        )
        to_version = _first(
            entry, ("to", "to_version", "toVersion", "target_version", "target")
        )
        if from_version is None or to_version is None:
            raise ManifestError(f"patches[{index}] 缺少起止版本")
        from_version, to_version = str(from_version), str(to_version)
        ref = _package_ref(entry, f"patches[{index}]")
        patch_id = _first(entry, ("id", "package", "package_id", "name"))
        if patch_id is None:
            patch_id = ref.path
        patch_id = str(patch_id)
        if patch_id in seen_ids:
            raise ManifestError(f"更正包 id 重复: {patch_id}")
        seen_ids.add(patch_id)
        digest = normalize_digest(
            _first(entry, ("sha256", "digest", "canonical_sha256", "hash"))
        )
        patches.append(
            PatchPackage(
                id=patch_id,
                package=ref,
                from_version=from_version,
                to_version=to_version,
                sha256=digest,
            )
        )

    releases: list[Release] = []
    release_source = _first(raw, ("releases", "published", "published_versions"))
    if isinstance(release_source, list):
        for entry in release_source:
            if isinstance(entry, str):
                releases.append(Release(version=entry, label=entry))
            elif isinstance(entry, dict):
                version = _first(entry, ("version", "id", "name"))
                if version is None:
                    warnings.append("releases 中有缺少 version 的条目，已忽略")
                    continue
                label = _first(entry, ("label", "title", "display_name"), str(version))
                digest = normalize_digest(
                    _first(entry, ("sha256", "digest", "canonical_sha256", "hash"))
                )
                if digest is not None:
                    digests.setdefault(str(version), digest)
                releases.append(Release(version=str(version), label=str(label)))
            else:
                warnings.append("releases 中有无法识别的条目，已忽略")
    elif isinstance(release_source, dict):
        for version, label in release_source.items():
            releases.append(Release(version=str(version), label=str(label)))

    all_versions = set(digests) | {b.version for b in bases} | {
        p.from_version for p in patches
    } | {p.to_version for p in patches}
    if not releases:
        releases = [Release(version=v, label=v) for v in all_versions]
    else:
        for release in releases:
            if release.version not in all_versions:
                warnings.append(f"发布版 {release.version} 不在清单的版本列表中")
        releases = [r for r in releases if r.version in all_versions]
    releases.sort(key=lambda r: natural_key(r.version))

    return Manifest(
        name=name,
        bases=bases,
        patches=patches,
        digests=digests,
        releases=releases,
        warnings=warnings,
    )


def natural_key(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for chunk in re.split(r"(\d+)", value):
        if chunk.isdigit():
            parts.append((1, int(chunk), ""))
        else:
            parts.append((0, 0, chunk))
    return tuple(parts)


def parse_json_bytes(raw: bytes, label: str = "JSON") -> Any:
    """Parse JSON strictly: no duplicate names, NaN or Infinity."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复的对象键 {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"不允许的数值 {value}")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} 不是 UTF-8: {exc}") from exc
    try:
        return json.loads(
            text, object_pairs_hook=no_duplicates, parse_constant=reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} 不是合法 JSON: {exc}") from exc
