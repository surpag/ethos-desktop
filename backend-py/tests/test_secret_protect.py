"""凭据保护：系统密钥加密 + 旧格式兼容。

守三件事：

1. 新写入是 `enc:v2:`，且**不再使用**源码里的混淆密钥（那是"看起来加密"）；
2. v1 旧数据仍能读（升级不炸），历史明文原样返回；
3. v2 的密文不受 `EDITH_SECRET_KEY` 影响——它由系统密钥（Windows DPAPI /
   本地主密钥）保护，不是环境变量里的字符串。
"""

from __future__ import annotations

import base64

from edith_backend.internal.userconfig import secret

PLAIN = "sk-live-abcdef0123456789"


def _legacy_v1(plain: str) -> str:
    """按 v1 规则手工构造旧值：base64(xor(utf8, 内置密钥按长度展开))。"""
    data = plain.encode("utf-8")
    key = b"edith-local-obfuscation-key-v1"
    mask = (key * (len(data) // len(key) + 1))[: len(data)]
    raw = bytes(b ^ m for b, m in zip(data, mask))
    return "enc:v1:" + base64.b64encode(raw).decode("ascii")


def test_roundtrip() -> None:
    stored = secret.protect(PLAIN)
    assert secret.unprotect(stored) == PLAIN


def test_new_format_is_v2_and_hides_plaintext() -> None:
    stored = secret.protect(PLAIN)
    assert stored.startswith(secret.V2_PREFIX)
    assert PLAIN not in stored
    # 内置 fallback key 一旦参与保护，就等于没加密：它不能出现在新格式里
    assert "edith-local-obfuscation-key-v1" not in stored


def test_ciphertext_is_not_deterministic() -> None:
    """同一明文两次加密结果不同：说明有随机量（DPAPI / nonce），不是可重放的映射。"""
    assert secret.protect(PLAIN) != secret.protect(PLAIN)


def test_empty_stays_empty() -> None:
    # 调用方靠"空串"判断有没有配置 key，不能被加密成一段密文
    assert secret.protect("") == ""
    assert secret.unprotect("") == ""


def test_legacy_v1_is_still_readable() -> None:
    assert secret.unprotect(_legacy_v1(PLAIN)) == PLAIN


def test_legacy_plaintext_passthrough() -> None:
    assert secret.unprotect("sk-legacy-plaintext") == "sk-legacy-plaintext"


def test_v2_does_not_depend_on_env_key(monkeypatch) -> None:
    """v2 由系统密钥保护：换掉 EDITH_SECRET_KEY 不该影响已存数据。"""
    stored = secret.protect(PLAIN)
    monkeypatch.setenv("EDITH_SECRET_KEY", "some-other-key")
    assert secret.unprotect(stored) == PLAIN


def test_corrupted_v2_returns_empty_instead_of_crashing() -> None:
    """解不开时不抛到界面上：设置页要能打开，只是显示"未配置"。"""
    assert secret.unprotect(secret.V2_PREFIX + "bm90LWEtcmVhbC1ibG9i") == ""


def test_corrupted_v1_is_returned_as_is() -> None:
    # 长度不是 4 的倍数：b64decode 抛异常，走"原样返回"分支
    broken = secret.V1_PREFIX + "aaa"
    assert secret.unprotect(broken) == broken
