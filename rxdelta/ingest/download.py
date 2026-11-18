"""`rxdelta fetch-cms`: download one monthly CMS release, only the tables read.

A release is one zip of about 2.3GB, nearly all of it the pharmacy networks
file. The zip format keeps its directory at the end of the file, and the CMS
server honours HTTP range requests, so this reads the directory, then reads and
unpacks only the inner archives whose names match the [source.files] patterns:
about 10MB per month. The release URL comes from the data.cms.gov catalog.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from rxdelta.config import Config
from rxdelta.types import RxdeltaError

RELEASE_PATTERN = re.compile(r"^(\d{4})_(\d{4})(\d{2})(\d{2})$")
SOURCE_RECORD = "SOURCE.json"


class DownloadError(RxdeltaError):
    """A release could not be found or read."""


def release_month(release: str) -> str:
    """The snapshot month a release belongs to: 2026_20260513 is 2026-05."""
    found = RELEASE_PATTERN.match(release)
    if not found:
        raise DownloadError(
            f"Release IDs look like 2026_20260513 (contract year, then release date), "
            f"got {release!r}."
        )
    return f"{found.group(2)}-{found.group(3)}"


class RangeFile(io.RawIOBase):
    """A remote file read through HTTP range requests, so zipfile can seek it."""

    def __init__(self, client: httpx.Client, url: str, *, retries: int) -> None:
        self._client = client
        self._url = url
        self._retries = retries
        self._pos = 0
        self.size = int(self._request("HEAD", None).headers["Content-Length"])

    def _request(self, method: str, byte_range: str | None) -> httpx.Response:
        headers = {"Range": f"bytes={byte_range}"} if byte_range else {}
        delay = 1.0
        for attempt in range(self._retries + 1):
            try:
                response = self._client.request(method, self._url, headers=headers)
                if response.status_code < 500:
                    response.raise_for_status()
                    return response
            except httpx.TransportError:
                pass
            except httpx.HTTPStatusError as exc:
                raise DownloadError(f"HTTP {exc.response.status_code} for {self._url}") from exc
            if attempt < self._retries:
                time.sleep(delay)
                delay *= 2
        raise DownloadError(f"Could not read {self._url} after {self._retries + 1} tries")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = base + offset
        return self._pos

    def readinto(self, buffer: Any) -> int:
        if self._pos >= self.size:
            return 0
        end = min(self._pos + len(buffer), self.size) - 1
        data = self._request("GET", f"{self._pos}-{end}").content
        buffer[: len(data)] = data
        self._pos += len(data)
        return len(data)


def find_release_url(client: httpx.Client, config: Config, release: str) -> str:
    """The archive URL of one release, from the data.cms.gov catalog."""
    settings = config.download
    response = client.get(settings.catalog_url)
    response.raise_for_status()
    suffix = f"/{release}.zip"
    for dataset in response.json().get("dataset", []):
        if not str(dataset.get("title", "")).startswith(settings.dataset_title):
            continue
        for distribution in dataset.get("distribution", []):
            url = str(distribution.get("downloadURL") or "")
            if url.endswith(suffix):
                return url
    raise DownloadError(
        f"Release {release} is not in the catalog at {settings.catalog_url}. Release IDs "
        "are the archive names, for example 2026_20260513."
    )


def _wanted(config: Config, inner_name: str) -> bool:
    stem = inner_name.lower().removesuffix(".zip")
    if any(fnmatch.fnmatch(stem, p.lower()) for p in config.source.exclude_dir_patterns):
        return False
    return any(
        fnmatch.fnmatch(stem, pattern.lower())
        for patterns in config.source.patterns.values()
        for pattern in patterns
    )


@dataclass(frozen=True)
class FetchedRelease:
    release: str
    month: str
    url: str
    directory: Path
    files: dict[str, str]  # path relative to the release folder: sha256


def fetch_release(
    config: Config,
    release: str,
    root: Path,
    *,
    month: str | None = None,
    client: httpx.Client | None = None,
) -> FetchedRelease:
    """Download one release's tables to root/<month>/<release>/<table folder>/."""
    month = month or release_month(release)
    settings = config.download
    own_client = client is None
    http = client or httpx.Client(timeout=settings.request_timeout_seconds, follow_redirects=True)
    try:
        url = find_release_url(http, config, release)
        outer = zipfile.ZipFile(
            io.BufferedReader(
                RangeFile(http, url, retries=settings.max_retries),
                buffer_size=settings.read_block_bytes,
            )
        )
        target = root / month / release
        files: dict[str, str] = {}
        for info in outer.infolist():
            if not _wanted(config, info.filename):
                continue
            folder = target / info.filename.removesuffix(".zip")
            inner = zipfile.ZipFile(io.BytesIO(outer.read(info.filename)))
            for member in inner.infolist():
                if member.is_dir():
                    continue
                path = folder / Path(member.filename).name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(inner.read(member))
                files[str(path.relative_to(target))] = hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        if own_client:
            http.close()
    if not files:
        raise DownloadError(f"Release {release} has no inner archive matching [source.files].")
    record = {
        "release": release,
        "month": month,
        "url": url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": dict(sorted(files.items())),
    }
    (target / SOURCE_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return FetchedRelease(release=release, month=month, url=url, directory=target, files=files)
