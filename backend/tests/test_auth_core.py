from app.services.auth_core import hash_password, make_token, read_token, verify_password


def test_password_roundtrip():
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("x", "garbage")


def test_token_roundtrip_expiry_and_tamper():
    token, exp = make_token("alice", now=1000, ttl=60)
    assert read_token(token, now=1030) == "alice"
    assert read_token(token, now=1061) is None
    body, sig = token.split(".")
    assert read_token(body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B"), now=1030) is None
    assert read_token("nonsense") is None
