"""Offline policy tests; never invoke an external SIP account or API."""
import pytest

from app.services.lia_voz_pilot import PilotConfig, PilotDenied


def config(**changes):
    values = dict(enabled=True, exclusive=True,
                  allowed_phone="(55) 99999-0000", prefix="8003")
    values.update(changes)
    return PilotConfig(**values)


def test_exact_canonical_destination_and_account():
    assert config().authorize("+55 55 99999-0000", "1804") == "5555999990000"
    assert config().configured
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0001", "1804")
    with pytest.raises(PilotDenied):
        config().authorize("55 99999-0000", "1805")


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"exclusive": False}, {"allowed_phone": ""},
    {"prefix": "9999"}, {"allowed_phone": "190"},
])
def test_missing_or_invalid_config_fails_closed(changes):
    c = config(**changes)
    assert not c.configured
    with pytest.raises(PilotDenied):
        c.authorize("55 99999-0000", "1804")


def test_environment_defaults_off(monkeypatch):
    for name in ("LIA_VOZ_TEST_ENABLED", "LIA_VOZ_TEST_EXCLUSIVE_SIP",
                 "LIA_VOZ_TEST_PHONE", "LIA_VOZ_TEST_NATIVE_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    assert not PilotConfig.from_env().configured
