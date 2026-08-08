"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from make_fixtures import (  # noqa: E402
    UNICODE_USER_PASSWORD,
    USER_PASSWORD,
    ensure_fixtures,
)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return ensure_fixtures()


@pytest.fixture(scope="session")
def fixture_password() -> str:
    return USER_PASSWORD


@pytest.fixture(scope="session")
def unicode_fixture_password() -> str:
    return UNICODE_USER_PASSWORD


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    target = tmp_path / "out"
    target.mkdir()
    return target
