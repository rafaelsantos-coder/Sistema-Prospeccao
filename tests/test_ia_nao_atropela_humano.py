from datetime import timedelta
from types import SimpleNamespace

from app.services import ai_sdr
from app.timeutils import utcnow


class _Db:
    def __init__(self, messages, ai_users=()):
        self.messages = messages
        self.ai_users = list(ai_users)
        self.calls = 0

    def scalars(self, _stmt):
        self.calls += 1
        if self.calls == 1:
            return self.ai_users
        if self.calls == 2:
            return self.messages
        return []


def _msg(sender, body="Seria 650Mb 99,90"):
    return SimpleNamespace(sender_user_id=sender, body=body, created_at=utcnow() - timedelta(minutes=5))


def test_humano_escreveu_manualmente_a_ia_se_cala():
    db = _Db([_msg(7)], ai_users=[99])
    assert ai_sdr.human_stepped_in(db, SimpleNamespace(id=1))


def test_mensagem_da_propria_ia_nao_conta():
    db = _Db([_msg(99)], ai_users=[99])
    assert not ai_sdr.human_stepped_in(db, SimpleNamespace(id=1))


def test_sem_mensagem_manual_a_ia_segue():
    assert not ai_sdr.human_stepped_in(_Db([]), SimpleNamespace(id=1))
