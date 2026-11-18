"""Where the API key comes from: the environment, optionally filled from .env."""

from __future__ import annotations

import os
from pathlib import Path

from rxdelta.types import RxdeltaError

KEY_VAR = "ANTHROPIC_API_KEY"
MISSING_KEY = (
    f"{KEY_VAR} is not set: export it in your shell or put {KEY_VAR}=<your key> in a .env "
    "file at the repo root."
)


class MissingKeyError(RxdeltaError):
    """No API key in the environment."""


def api_key(env_file: Path | None = Path(".env")) -> str:
    """The key from the environment. A .env file fills it in but never overrides it."""
    if env_file is not None and env_file.is_file():
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        raise MissingKeyError(MISSING_KEY)
    return key
