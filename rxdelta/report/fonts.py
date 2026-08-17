"""Vendored web fonts, base64 embedded at render time.

The report has to survive being emailed as one file with no network, so the
faces are inlined rather than linked. Both families are OFL licensed and the
license texts ship next to the woff2 files.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


@dataclass(frozen=True)
class FontFace:
    """One @font-face rule's worth of data."""

    family: str
    weight: int
    file_name: str
    # Metric overrides for the fallback face, so swapping in the real font does
    # not reflow the table. Measured against the fallback named below.
    fallback: str
    size_adjust: str
    ascent_override: str
    descent_override: str
    line_gap_override: str

    @property
    def fallback_family(self) -> str:
        return f"{self.family} Fallback"


FACES: tuple[FontFace, ...] = (
    FontFace(
        family="Public Sans",
        weight=400,
        file_name="public-sans-400.woff2",
        fallback="Helvetica Neue",
        size_adjust="104%",
        ascent_override="90%",
        descent_override="22%",
        line_gap_override="0%",
    ),
    FontFace(
        family="Public Sans",
        weight=600,
        file_name="public-sans-600.woff2",
        fallback="Helvetica Neue",
        size_adjust="104%",
        ascent_override="90%",
        descent_override="22%",
        line_gap_override="0%",
    ),
    FontFace(
        # Roboto Mono, not Plex Mono: Plex draws a dotted zero by default and
        # ships no "zero" feature to switch it off. Roboto Mono's zero is plain.
        family="Roboto Mono",
        weight=400,
        file_name="roboto-mono-400.woff2",
        fallback="Menlo",
        size_adjust="100%",
        ascent_override="92%",
        descent_override="24%",
        line_gap_override="0%",
    ),
)


@dataclass(frozen=True)
class EmbeddedFace:
    family: str
    weight: int
    data_uri: str
    fallback_family: str
    fallback: str
    size_adjust: str
    ascent_override: str
    descent_override: str
    line_gap_override: str


@lru_cache(maxsize=1)
def embedded_faces() -> tuple[EmbeddedFace, ...]:
    """Read every vendored face once and hand back data URIs."""
    out: list[EmbeddedFace] = []
    for face in FACES:
        path = FONT_DIR / face.file_name
        if not path.is_file():
            raise FileNotFoundError(
                f"Vendored font missing: {path}. The report embeds its fonts, so the "
                "woff2 files must ship with the package."
            )
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        out.append(
            EmbeddedFace(
                family=face.family,
                weight=face.weight,
                data_uri=f"data:font/woff2;base64,{encoded}",
                fallback_family=face.fallback_family,
                fallback=face.fallback,
                size_adjust=face.size_adjust,
                ascent_override=face.ascent_override,
                descent_override=face.descent_override,
                line_gap_override=face.line_gap_override,
            )
        )
    return tuple(out)


def embedded_bytes() -> int:
    return sum((FONT_DIR / f.file_name).stat().st_size for f in FACES)
