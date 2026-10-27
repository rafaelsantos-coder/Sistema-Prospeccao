from app.services.scoring import calculate_sulnet_score, normalize_phone, temperature_from_score


def test_normalize_brazilian_phone():
    assert normalize_phone("(55) 99999-1234") == "5555999991234"
    assert normalize_phone("+55 55 99999-1234") == "5555999991234"
    assert normalize_phone("123") is None


def test_score_prioritizes_coverage_and_intent():
    result = calculate_sulnet_score(
        {
            "phone": "(55) 99999-1234",
            "email": "contato@empresa.com.br",
            "website": "https://empresa.com.br",
            "segment": "Clínica odontológica",
            "city": "Santa Rosa",
            "source": "TRAFEGO_PAGO",
            "reviews_count": 120,
            "intent_signal": True,
        }
    )
    assert result.score == 10.0
    assert result.temperature == "QUENTE"
    assert "cidade com cobertura" in result.reasons


def test_do_not_contact_forces_zero():
    result = calculate_sulnet_score({"phone": "5555999999999", "city": "Santa Rosa", "do_not_contact": True})
    assert result.score == 0
    assert result.temperature == "FRIO"


def test_temperature_thresholds():
    assert temperature_from_score(4.9) == "FRIO"
    assert temperature_from_score(5) == "MORNO"
    assert temperature_from_score(8) == "QUENTE"
