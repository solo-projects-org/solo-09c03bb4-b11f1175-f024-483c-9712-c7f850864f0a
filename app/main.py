"""FastAPI application: pick an archived release, verify it, download it."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .archive import Archive, Manifest, ManifestError, Resolution, document_digest
from .config import Settings
from .pricing import (
    TableError,
    build_report,
    filter_by_month,
    format_fen,
    month_bounds,
    parse_quotes,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class AppState:
    settings: Settings
    archive: Archive


def create_app(settings: Settings | None = None, archive: Archive | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    archive = archive or Archive(settings.archive_dir, settings.manifest_name)
    state = AppState(settings=settings, archive=archive)

    app = FastAPI(title="菜价月报底表归档复核", docs_url=None, redoc_url=None)
    app.state.price_archive = state
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    TEMPLATES.env.filters["fen"] = format_fen
    TEMPLATES.env.filters["urlquote"] = lambda value: quote(str(value), safe="")

    @app.get("/healthz", response_class=JSONResponse)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        try:
            manifest = state.archive.load_manifest()
        except ManifestError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "error.html",
                {"title": "归档清单不可用", "message": str(exc), "archive": state.archive},
                status_code=503,
            )
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "title": manifest.name,
                "manifest": manifest,
                "archive": state.archive,
            },
        )

    @app.get("/release/{version}", response_class=HTMLResponse)
    def release(request: Request, version: str, month: str | None = None) -> Response:
        try:
            manifest = state.archive.load_manifest()
        except ManifestError as exc:
            return TEMPLATES.TemplateResponse(
                request,
                "error.html",
                {"title": "归档清单不可用", "message": str(exc), "archive": state.archive},
                status_code=503,
            )
        release_meta = next((r for r in manifest.releases if r.version == version), None)
        if release_meta is None:
            raise HTTPException(status_code=404, detail=f"发布版 {version} 不在归档清单中")

        if month is not None:
            try:
                month_bounds(month)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        resolution = state.archive.resolve(version)
        context: dict[str, Any] = {
            "title": f"{manifest.name} · {release_meta.label}",
            "manifest": manifest,
            "archive": state.archive,
            "release": release_meta,
            "resolution": resolution,
            "month": month or "",
            "month_options": [],
            "report": None,
            "table_error": None,
            "rows": [],
            "rows_truncated": False,
            "max_detail_rows": settings.max_detail_rows,
            "download_sha256": None,
        }
        if resolution.ok and resolution.document is not None:
            try:
                quotes = parse_quotes(resolution.document)
            except TableError as exc:
                context["table_error"] = str(exc)
            else:
                context["month_options"] = sorted(
                    {q.quoted_on.isoformat()[:7] for q in quotes}, reverse=True
                )
                selected = filter_by_month(quotes, month)
                context["report"] = build_report(selected)
                context["rows"] = selected[: settings.max_detail_rows]
                context["rows_truncated"] = len(selected) > settings.max_detail_rows
                context["download_sha256"] = resolution.chain[-1].sha256 if resolution.chain else None
        status = 200 if resolution.ok else 422
        return TEMPLATES.TemplateResponse(request, "release.html", context, status_code=status)

    @app.get("/release/{version}/download")
    def download(version: str) -> Response:
        manifest = _manifest_or_503(state.archive)
        if not any(r.version == version for r in manifest.releases):
            raise HTTPException(status_code=404, detail=f"发布版 {version} 不在归档清单中")
        resolution = state.archive.resolve(version)
        if not resolution.ok or resolution.document is None:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "目标版未通过核对，无法提供下载",
                    "version": version,
                    "reason": resolution.reason,
                    "issues": [issue.as_dict() for issue in resolution.issues],
                },
            )
        digest = document_digest(resolution.document)
        # Re-verify immediately before serving: the archive is untrusted input.
        expected = manifest.digests.get(version)
        if expected is None or digest.lower() != expected.lower():
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "下载前复核摘要不符，已拒绝提供文件",
                    "version": version,
                    "expected": expected,
                    "actual": digest,
                },
            )
        payload = json.dumps(
            resolution.document, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", version) or "release"
        return Response(
            content=payload,
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="price-table-{safe_name}.json"',
                "X-Canonical-SHA256": digest,
                "X-Packages-Used": ",".join(step.package for step in resolution.chain),
            },
        )

    @app.get("/release/{version}/resolution.json", response_class=JSONResponse)
    def resolution_json(version: str) -> Response:
        manifest = _manifest_or_503(state.archive)
        if not any(r.version == version for r in manifest.releases):
            raise HTTPException(status_code=404, detail=f"发布版 {version} 不在归档清单中")
        resolution = state.archive.resolve(version)
        return JSONResponse(
            status_code=200 if resolution.ok else 422,
            content=resolution_payload(resolution),
        )

    return app


def resolution_payload(resolution: Resolution) -> dict[str, Any]:
    return {
        "version": resolution.target,
        "ok": resolution.ok,
        "reason": resolution.reason,
        "package_bytes": resolution.package_bytes,
        "chain": [
            {
                "kind": step.kind,
                "package": step.package,
                "package_id": step.package_id,
                "from": step.from_version,
                "to": step.to_version,
                "declared_bytes": step.declared_bytes,
                "actual_bytes": step.actual_bytes,
                "sha256": step.sha256,
                "verified": step.verified,
            }
            for step in resolution.chain
        ],
        "attempts": [
            {
                "planned_versions": attempt.planned_versions,
                "ok": attempt.ok,
                "reason": attempt.reason,
                "issues": [issue.as_dict() for issue in attempt.issues],
            }
            for attempt in resolution.attempts
        ],
        "issues": [issue.as_dict() for issue in resolution.issues],
    }


def _manifest_or_503(archive: Archive) -> Manifest:
    try:
        return archive.load_manifest()
    except ManifestError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


app = create_app()
