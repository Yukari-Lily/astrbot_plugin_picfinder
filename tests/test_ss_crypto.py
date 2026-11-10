"""内置 Shadowsocks 加密层（AES-GCM / ChaCha20-Poly1305 / HKDF）单元测试。

用标准向量验收纯 Python 实现，再与 ``cryptography`` 后端做交叉验证。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from proxy.ss_crypto import (  # noqa: E402
    AeadCipher,
    BACKEND,
    METHOD_AES_128_GCM,
    METHOD_AES_256_GCM,
    METHOD_CHACHA20_POLY1305,
    _PureAESGCM,
    _PureChaCha20Poly1305,
    hkdf_sha1,
    is_supported_method,
    key_length,
)


class TestKdf(unittest.TestCase):
    def test_hkdf_sha1_rfc5869_case4(self):
        okm = hkdf_sha1(
            b"\x0b" * 11,
            bytes.fromhex("000102030405060708090a0b0c"),
            bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"),
            42,
        )
        self.assertEqual(
            okm.hex(),
            "085a01ea1b10f36933068b56efa5ad81"
            "a4f14b822f5b091568a9cdd4f155fda2"
            "c22e422478d305f3f896",
        )

    def test_hkdf_sha1_respects_length(self):
        self.assertEqual(len(hkdf_sha1(b"k" * 16, b"s" * 16, length=16)), 16)
        self.assertEqual(len(hkdf_sha1(b"k" * 16, b"s" * 16, length=32)), 32)

    def test_evp_bytes_to_key_md5(self):
        from proxy.shadowsocks import evp_bytes_to_key

        # MD5("test")
        self.assertEqual(
            evp_bytes_to_key("test", 16).hex(), "098f6bcd4621d373cade4e832627b4f6"
        )
        self.assertEqual(len(evp_bytes_to_key("test", 32)), 32)


class TestCryptoVectors(unittest.TestCase):
    def test_aes_gcm_nist_case1(self):
        cipher = _PureAESGCM(b"\x00" * 16)
        self.assertEqual(
            cipher.encrypt(b"\x00" * 12, b"").hex(),
            "58e2fccefa7e3061367f1d57a4e7455a",
        )

    def test_aes_gcm_nist_case2(self):
        cipher = _PureAESGCM(b"\x00" * 16)
        out = cipher.encrypt(b"\x00" * 12, b"\x00" * 16)
        self.assertEqual(
            out.hex(),
            "0388dace60b6a392f328c2b971b2fe78ab6e47d42cec13bdf53a67b21257bddf",
        )
        self.assertEqual(cipher.decrypt(b"\x00" * 12, out), b"\x00" * 16)

    def test_aes_gcm_tamper_detected(self):
        cipher = _PureAESGCM(b"\x01" * 32)
        out = bytearray(cipher.encrypt(b"\x02" * 12, b"hello world"))
        out[0] ^= 0xFF
        with self.assertRaises(ValueError):
            cipher.decrypt(b"\x02" * 12, bytes(out))

    def test_chacha20_poly1305_rfc8439(self):
        key = bytes.fromhex(
            "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
        )
        nonce = bytes.fromhex("070000004041424344454647")
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        plaintext = (
            b"Ladies and Gentlemen of the class of '99: If I could offer you "
            b"only one tip for the future, sunscreen would be it."
        )
        cipher = _PureChaCha20Poly1305(key)
        out = cipher.encrypt(nonce, plaintext, aad)
        self.assertEqual(out[-16:].hex(), "1ae10b594f09e26a7e902ecbd0600691")
        self.assertEqual(cipher.decrypt(nonce, out, aad), plaintext)

    def test_pure_matches_backend(self):
        if BACKEND != "cryptography":
            self.skipTest("未安装 cryptography，跳过交叉验证")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305

        key = os.urandom(32)
        nonce = os.urandom(12)
        payload = os.urandom(777)
        self.assertEqual(
            _PureAESGCM(key).encrypt(nonce, payload), AESGCM(key).encrypt(nonce, payload, b"")
        )
        self.assertEqual(
            _PureChaCha20Poly1305(key).encrypt(nonce, payload),
            ChaCha20Poly1305(key).encrypt(nonce, payload, b""),
        )


class TestAeadCipher(unittest.TestCase):
    def test_key_length_validation(self):
        self.assertEqual(key_length(METHOD_AES_128_GCM), 16)
        self.assertEqual(key_length(METHOD_AES_256_GCM), 32)
        self.assertEqual(key_length(METHOD_CHACHA20_POLY1305), 32)
        with self.assertRaises(ValueError):
            key_length("rc4-md5")
        self.assertFalse(is_supported_method("rc4-md5"))
        with self.assertRaises(ValueError):
            AeadCipher(METHOD_AES_128_GCM, b"\x00" * 32)

    def test_nonce_increments_little_endian(self):
        cipher = AeadCipher(METHOD_AES_128_GCM, b"\x11" * 16)
        first = cipher.encrypt(b"a")
        self.assertEqual(bytes(cipher.nonce), b"\x01" + b"\x00" * 11)
        second = cipher.encrypt(b"a")
        self.assertNotEqual(first, second)
        self.assertEqual(bytes(cipher.nonce), b"\x02" + b"\x00" * 11)

    def test_roundtrip_stream(self):
        key = b"\x22" * 32
        sender = AeadCipher(METHOD_CHACHA20_POLY1305, key)
        receiver = AeadCipher(METHOD_CHACHA20_POLY1305, key)
        for payload in (b"", b"x", os.urandom(1000)):
            self.assertEqual(receiver.decrypt(sender.encrypt(payload)), payload)


if __name__ == "__main__":
    unittest.main()
