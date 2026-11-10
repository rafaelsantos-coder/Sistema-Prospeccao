from decimal import Decimal
from types import SimpleNamespace

from app.services import ai_seller


def _state(label):
    return SimpleNamespace(plan_label=label, offer_price=Decimal("99.90"))


def test_650_com_mes_gratis_leva_tag_black():
    assert ai_seller._resid_black_offer(_state("Sulnet Resid Fibra 650 Mega"), {"freeMonths": 1})
    assert ai_seller._resid_black_offer(_state("Sulnet Resid Fibra 800 Mega"), {"freeMonths": 1})


def test_sem_mes_gratis_ou_outro_plano_nao_leva():
    assert not ai_seller._resid_black_offer(_state("Sulnet Resid Fibra 650 Mega"), {"freeMonths": 0})
    assert not ai_seller._resid_black_offer(_state("Sulnet Resid Fibra 650 Mega"), {})
    assert not ai_seller._resid_black_offer(_state("Corp. 650 Mega"), {"freeMonths": 1})
    assert not ai_seller._resid_black_offer(_state("Telefonia Móvel 10GB"), {"freeMonths": 1})
