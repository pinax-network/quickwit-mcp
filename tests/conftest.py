"""Pytest configuration for quickwit-mcp."""

import os

import pytest


@pytest.fixture(scope="session")
def quickwit_base_url() -> str | None:
    return os.getenv("QUICKWIT_BASE_URL")
