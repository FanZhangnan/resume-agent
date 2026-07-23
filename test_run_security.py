"""Offline tests for stateless signed run tokens."""

import run_security as rs

KEY = "test-signing-key-please-rotate"


def test_signed_token_round_trip():
    token, exp = rs.issue_run_token("run-abc", ttl_seconds=600, now=1000.0, key=KEY)
    assert isinstance(token, str) and token
    assert exp == 1600
    assert rs.verify_run_token(token, "run-abc", now=1200.0, key=KEY) is True


def test_token_rejects_expiry():
    token, _ = rs.issue_run_token("run-abc", ttl_seconds=600, now=1000.0, key=KEY)
    assert rs.verify_run_token(token, "run-abc", now=1700.0, key=KEY) is False


def test_token_rejects_run_id_mismatch():
    token, _ = rs.issue_run_token("run-abc", ttl_seconds=600, now=1000.0, key=KEY)
    assert rs.verify_run_token(token, "run-xyz", now=1200.0, key=KEY) is False


def test_token_rejects_tamper():
    token, _ = rs.issue_run_token("run-abc", ttl_seconds=600, now=1000.0, key=KEY)
    # Flip one character in the signature segment.
    payload, sig = token.rsplit(".", 1)
    flipped = "A" if sig[0] != "A" else "B"
    tampered = f"{payload}.{flipped}{sig[1:]}"
    assert rs.verify_run_token(tampered, "run-abc", now=1200.0, key=KEY) is False


def test_token_rejects_wrong_key():
    token, _ = rs.issue_run_token("run-abc", ttl_seconds=600, now=1000.0, key=KEY)
    assert rs.verify_run_token(token, "run-abc", now=1200.0, key="other-key") is False


def test_token_rejects_garbage():
    for junk in ("", "not-a-token", "a.b.c", "@@@.@@@"):
        assert rs.verify_run_token(junk, "run-abc", now=1200.0, key=KEY) is False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} run-security tests passed")
