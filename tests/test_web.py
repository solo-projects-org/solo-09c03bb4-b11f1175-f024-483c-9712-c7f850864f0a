import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.archive import document_digest
from app.config import Settings
from app.main import create_app
from app.pricing import average_yuan, build_report, filter_by_month, parse_quotes

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "sample_archive"


def make_client(archive_dir: Path = SAMPLE) -> TestClient:
    app = create_app(Settings(archive_dir=archive_dir))
    return TestClient(app)


@pytest.fixture(scope="module")
def client():
    return make_client()


def test_index_lists_releases_and_packages(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    for version in ("2026-06", "2026-07", "2026-08", "2026-09", "2026-10"):
        assert f"/release/{version}" in body
    assert "patch-09-to-10" in body
    assert "15224" in body


def test_release_page_reports_verified_chain(client):
    response = client.get("/release/2026-10")
    assert response.status_code == 200
    body = response.text
    assert "目标版已核对通过" in body
    assert "tables/table-2026-09.json" in body
    assert "patches/patch-09-to-10.json" in body
    assert "实际用到的包" in body
    assert "均价" in body


def test_release_page_shows_average(client):
    response = client.get("/release/2026-06")
    assert response.status_code == 200
    table = json.loads((SAMPLE / "tables/table-2026-06.json").read_text(encoding="utf-8"))
    quotes = parse_quotes(table)
    report = build_report(quotes)
    assert report.quote_count == 60
    assert report.average_yuan in response.text


def test_month_filter_recomputes_average(client):
    response = client.get("/release/2026-06?month=2026-06")
    assert response.status_code == 200
    table = json.loads((SAMPLE / "tables/table-2026-06.json").read_text(encoding="utf-8"))
    quotes = filter_by_month(parse_quotes(table), "2026-06")
    assert build_report(quotes).average_yuan in response.text

    empty = client.get("/release/2026-06?month=2026-02")
    assert empty.status_code == 200
    assert "0.0000" in empty.text


def test_bad_month_is_rejected(client):
    assert client.get("/release/2026-06?month=2026-13").status_code == 400
    assert client.get("/release/2026-06?month=nope").status_code == 400


def test_unknown_release_is_404(client):
    assert client.get("/release/2026-01").status_code == 404
    assert client.get("/release/2026-01/download").status_code == 404


def test_download_matches_manifest_digest(client):
    response = client.get("/release/2026-10/download")
    assert response.status_code == 200
    document = json.loads(response.content.decode("utf-8"))
    manifest = json.loads((SAMPLE / "manifest.json").read_text(encoding="utf-8"))
    assert document_digest(document) == manifest["digests"]["2026-10"]
    assert response.headers["X-Canonical-SHA256"] == manifest["digests"]["2026-10"]
    assert "attachment" in response.headers["content-disposition"]


def test_resolution_json_lists_attempts_and_issues(tmp_path):
    # A broken September base forces one failed attempt before success.
    root = tmp_path / "archive"
    import shutil

    shutil.copytree(SAMPLE, root)
    (root / "tables/table-2026-09.json").write_text("{}", encoding="utf-8")
    client = make_client(root)
    response = client.get("/release/2026-10/resolution.json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["attempts"][0]["ok"] is False
    assert payload["attempts"][0]["issues"][0]["package"] == "tables/table-2026-09.json"
    assert payload["attempts"][0]["issues"][0]["kind"] == "摘要不符"
    assert payload["attempts"][-1]["ok"] is True


def test_no_chain_disables_download(tmp_path):
    import shutil

    root = tmp_path / "archive"
    shutil.copytree(SAMPLE, root)
    # Break every package and remove the newer base table.
    (root / "tables/table-2026-09.json").unlink()
    for patch in (root / "patches").glob("*.json"):
        patch.unlink()

    client = make_client(root)
    page = client.get("/release/2026-10")
    assert page.status_code == 422
    assert "不提供下载" in page.text
    assert "缺包" in page.text

    download = client.get("/release/2026-10/download")
    assert download.status_code == 422
    assert "未通过核对" in download.json()["detail"]

    resolution = client.get("/release/2026-10/resolution.json")
    assert resolution.status_code == 422
    assert resolution.json()["ok"] is False


def test_missing_archive_shows_error_page(tmp_path):
    client = make_client(tmp_path / "does-not-exist")
    response = client.get("/")
    assert response.status_code == 503
    assert "归档清单不可用" in response.text
    assert client.get("/release/2026-06").status_code == 503


def test_average_yuan_rounding():
    assert average_yuan(100, 3) == "0.3333"
    assert average_yuan(1, 3) == "0.0033"
    assert average_yuan(0, 3) == "0.0000"
    assert average_yuan(101, 1) == "1.0100"
    assert average_yuan(1, 2) == "0.0050"
