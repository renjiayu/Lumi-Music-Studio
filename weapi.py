"""
网易云音乐 WeAPI 加密 (AES + RSA)
- 接口使用 POST + encrypted params/encSecKey
"""
import base64
import hashlib
import json
import random
import string

from Crypto.Cipher import AES

MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152"
    "b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda9"
    "2557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe48"
    "75d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
EXPONENT = "010001"
IV = b"0102030405060708"
PRESET_KEY = "0CoJUm6Qyw8W8jud"


def _aes_encrypt(text: bytes, key: str) -> bytes:
    """AES-128-CBC with PKCS7 padding, returns raw bytes"""
    pad = 16 - len(text) % 16
    text += bytes([pad] * pad)
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, IV)
    return cipher.encrypt(text)


def _rsa_encrypt(text: str) -> str:
    """RSA encryption: text reversed, encoded, then num ** exp % mod"""
    reversed_bytes = text[::-1].encode("utf-8")
    num = int.from_bytes(reversed_bytes, "big")
    enc = pow(num, int(EXPONENT, 16), int(MODULUS, 16))
    return format(enc, "x").zfill(256)


def _random_key(length: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def encrypt(data: dict) -> tuple:
    """
    WeAPI 加密, 返回 (params, encSecKey)

    两层 AES-CBC:
    1. JSON → AES(PRESET_KEY) → Base64
    2. Base64 字符串 → AES(random_key) → Base64
    3. RSA 加密 random_key
    """
    text = json.dumps(data, separators=(",", ":")).encode("utf-8")
    once_raw = _aes_encrypt(text, PRESET_KEY)
    once_b64 = base64.b64encode(once_raw).decode("utf-8")
    sec_key = _random_key(16)
    params = base64.b64encode(
        _aes_encrypt(once_b64.encode("utf-8"), sec_key)
    ).decode("utf-8")
    enc_sec_key = _rsa_encrypt(sec_key)
    return params, enc_sec_key
