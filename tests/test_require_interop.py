"""``--require-interop`` turns a missing ``ikigai`` binary from a skip into a failure."""

from __future__ import annotations

import pytest

from tests import rust_door


class _Request:
    def __init__(self, required: bool):
        self.config = self
        self._required = required

    def getoption(self, name):
        assert name == "--require-interop"
        return self._required


def test_a_missing_binary_skips_without_the_flag(monkeypatch):
    monkeypatch.setattr(rust_door, "IKIGAI", None)
    with pytest.raises(pytest.skip.Exception):
        rust_door.require_ikigai(_Request(False))


def test_a_missing_binary_fails_under_the_flag(monkeypatch):
    monkeypatch.setattr(rust_door, "IKIGAI", None)
    with pytest.raises(pytest.fail.Exception, match="require-interop"):
        rust_door.require_ikigai(_Request(True))


def test_integration_tests_go_through_the_same_helper():
    from tests import test_integration

    assert test_integration.require_ikigai is rust_door.require_ikigai
