from app.security import hash_password, verify_password


def test_password_roundtrip():
    encoded = hash_password("SenhaSegura@2026")
    assert verify_password("SenhaSegura@2026", encoded)
    assert not verify_password("senha-errada", encoded)
