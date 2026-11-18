from __future__ import annotations

import io
import json
import random
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from rxdelta.cli import app
from rxdelta.config import Config
from rxdelta.ingest import download

RELEASE = "2026_20260513"
URL = f"https://data.cms.gov/sites/default/files/2026-05/abc/{RELEASE}.zip"
INNER = {
    "basic drugs formulary file  20260531.zip": "formulary rows\n",
    "beneficiary cost file  20260531.zip": "cost rows\n",
    "plan information  20260531.zip": "plan rows\n",
    "insulin beneficiary cost file  20260531.zip": "not read\n",
    "pharmacy networks file  20260531 part 1.zip": random.Random(1).randbytes(300_000).hex(),
    "sample files 20260531.zip": "not read\n",
}


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _release_archive() -> bytes:
    return _zip(
        {
            name: _zip({name.removesuffix(".zip") + ".txt": text.encode()})
            for name, text in INNER.items()
        }
    )


def _transport(archive: bytes, requested: list[tuple[str, int]]) -> httpx.MockTransport:
    catalog = {
        "dataset": [
            {
                "title": "Monthly Prescription Drug Plan Formulary and Pharmacy Network "
                "Information : 2026-05-20",
                "distribution": [{"downloadURL": URL}],
            },
            {"title": "Something else", "distribution": [{"downloadURL": "https://x/y.zip"}]},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.cms.gov/data.json":
            return httpx.Response(200, json=catalog)
        assert str(request.url) == URL
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": str(len(archive))})
        start, end = (int(v) for v in request.headers["Range"].removeprefix("bytes=").split("-"))
        requested.append((request.method, end - start + 1))
        return httpx.Response(206, content=archive[start : end + 1])

    return httpx.MockTransport(handler)


def test_release_month_comes_from_the_release_date() -> None:
    assert download.release_month("2026_20260513") == "2026-05"
    assert download.release_month("2027_20261008") == "2026-10"
    with pytest.raises(download.DownloadError, match="Release IDs look like"):
        download.release_month("may-2026")


def test_fetch_reads_only_the_three_tables(config: Config, tmp_path: Path) -> None:
    archive = _release_archive()
    requested: list[tuple[str, int]] = []
    client = httpx.Client(transport=_transport(archive, requested))
    small_reads = replace(
        config, download=config.download.model_copy(update={"read_block_bytes": 4096})
    )
    fetched = download.fetch_release(small_reads, RELEASE, tmp_path, client=client)

    assert fetched.month == "2026-05" and fetched.url == URL
    target = tmp_path / "2026-05" / RELEASE
    assert sorted(fetched.files) == [
        "basic drugs formulary file  20260531/basic drugs formulary file  20260531.txt",
        "beneficiary cost file  20260531/beneficiary cost file  20260531.txt",
        "plan information  20260531/plan information  20260531.txt",
    ]
    text = target / "plan information  20260531" / "plan information  20260531.txt"
    assert text.read_text() == "plan rows\n"
    record = json.loads((target / download.SOURCE_RECORD).read_text())
    assert record["release"] == RELEASE and record["url"] == URL
    assert record["files"] == fetched.files
    # Only ranges were read, and far less than the whole archive.
    assert all(method == "GET" for method, _ in requested)
    assert sum(size for _, size in requested) < len(archive) / 10


def test_an_unknown_release_is_a_clear_error(config: Config, tmp_path: Path) -> None:
    client = httpx.Client(transport=_transport(_release_archive(), []))
    with pytest.raises(download.DownloadError, match="is not in the catalog"):
        download.fetch_release(config, "2026_20991231", tmp_path, client=client)


def test_fetch_cms_cli_rejects_a_bad_release_id(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["fetch-cms", "--release", "may", "--dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Release IDs look like" in result.output
