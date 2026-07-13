import unittest

from public_security import (
    decrypt_api_key,
    encrypt_api_key,
    hash_ip,
    issue_session,
    normalize_client_ip,
    verify_session,
)


class SessionSecurityTests(unittest.TestCase):
    def test_session_cookie_round_trip_and_expiry(self):
        cookie, session_hash, expiry = issue_session("signing-key-a", now=1000, ttl=86400)
        self.assertEqual(expiry, 87400)
        self.assertEqual(verify_session(cookie, "signing-key-a", now=1000), session_hash)
        self.assertEqual(verify_session(cookie, "signing-key-a", now=87399), session_hash)
        self.assertIsNone(verify_session(cookie, "signing-key-a", now=87400))
        self.assertRegex(session_hash, r"^[0-9a-f]{64}$")

    def test_cookie_tampering_and_key_rotation_are_rejected(self):
        cookie, _, _ = issue_session("old-signing-key", now=2000, ttl=86400)
        parts = cookie.split(".")
        tampered = ".".join(parts[:-1] + [("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]])
        self.assertIsNone(verify_session(tampered, "old-signing-key", now=2000))
        self.assertIsNone(verify_session(cookie, "new-signing-key", now=2000))

        rotated, rotated_hash, _ = issue_session("new-signing-key", now=2000, ttl=86400)
        self.assertEqual(verify_session(rotated, "new-signing-key", now=2000), rotated_hash)

    def test_malformed_cookie_and_invalid_signing_key_are_rejected(self):
        for value in (None, "", "not-a-cookie", "v1.sid.not-an-int.signature"):
            self.assertIsNone(verify_session(value, "signing-key", now=3000))
        self.assertIsNone(verify_session("v1.sid.9999.signature", "", now=3000))
        with self.assertRaises(ValueError):
            issue_session("", now=3000, ttl=86400)
        with self.assertRaises(ValueError):
            issue_session("signing-key", now=3000, ttl=0)
        with self.assertRaises(ValueError):
            issue_session("signing-key", now=3000, ttl=86401)


class ClientIpSecurityTests(unittest.TestCase):
    def test_forwarded_chain_uses_first_valid_canonical_address(self):
        self.assertEqual(
            normalize_client_ip("unknown, 203.0.113.9, 198.51.100.2", "127.0.0.1"),
            "203.0.113.9",
        )
        self.assertEqual(
            normalize_client_ip(" 2001:0db8:0:0::1, 203.0.113.9", "127.0.0.1"),
            "2001:db8::1",
        )

    def test_client_host_is_fallback_and_invalid_inputs_are_rejected(self):
        self.assertEqual(normalize_client_ip("invalid", "127.0.0.1"), "127.0.0.1")
        with self.assertRaises(ValueError):
            normalize_client_ip("invalid", "also-invalid")

    def test_ip_hash_is_keyed_deterministic_and_contains_no_address(self):
        first = hash_ip("203.0.113.9", "signing-key")
        second = hash_ip("203.0.113.9", "signing-key")
        different = hash_ip("203.0.113.9", "other-key")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotIn("203.0.113.9", first)
        with self.assertRaises(ValueError):
            hash_ip("not-an-ip", "signing-key")


class ApiKeyEncryptionTests(unittest.TestCase):
    def test_aesgcm_round_trip_is_randomized_and_redis_safe(self):
        api_key = "sk-test-秘密-value"
        first = encrypt_api_key(api_key, "signing-key")
        second = encrypt_api_key(api_key, "signing-key")
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9_-]+$")
        self.assertEqual(decrypt_api_key(first, "signing-key"), api_key)
        self.assertEqual(decrypt_api_key(second, "signing-key"), api_key)

    def test_wrong_key_tampering_empty_and_malformed_values_are_rejected(self):
        encrypted = encrypt_api_key("sk-test", "signing-key")
        with self.assertRaises(ValueError):
            decrypt_api_key(encrypted, "wrong-key")

        replacement = "A" if encrypted[-1] != "A" else "B"
        with self.assertRaises(ValueError):
            decrypt_api_key(encrypted[:-1] + replacement, "signing-key")

        for value in (None, "", "not_base64!"):
            with self.assertRaises(ValueError):
                decrypt_api_key(value, "signing-key")
        for api_key in (None, ""):
            with self.assertRaises(ValueError):
                encrypt_api_key(api_key, "signing-key")
        with self.assertRaises(ValueError):
            encrypt_api_key("sk-test", "")


if __name__ == "__main__":
    unittest.main()
