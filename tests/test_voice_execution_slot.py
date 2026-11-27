"""Verify shared slot semantics without making calls."""
import pytest

from app.services import voice_execution_slot as slots


def test_local_slot_excludes_nested_claim_and_releases():
    with slots.voice_execution_slot() as owned:
        assert owned and slots.slot_healthy(required=True)
        with slots.voice_execution_slot() as second:
            assert not second
    assert not slots.slot_healthy(required=True)
    with slots.voice_execution_slot() as owned:
        assert owned


def test_slot_released_after_exception():
    with pytest.raises(RuntimeError):
        with slots.voice_execution_slot() as owned:
            assert owned
            raise RuntimeError("test")
    with slots.voice_execution_slot() as owned:
        assert owned
