import pytest

from app.services.zapi import ZApiError, ZApiService


def test_zapi_phone_normalization_adds_brazil_country_code():
    assert ZApiService.normalize_phone("(55) 99999-9999") == "5555999999999"


def test_zapi_phone_normalization_preserves_international_number():
    assert ZApiService.normalize_phone("+55 55 99999-9999") == "5555999999999"


def test_zapi_phone_normalization_rejects_invalid_value():
    with pytest.raises(ZApiError):
        ZApiService.normalize_phone("123")
