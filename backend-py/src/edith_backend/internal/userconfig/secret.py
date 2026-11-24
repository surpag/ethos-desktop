"""凭据保护：落盘凭据由**系统密钥**保护，不再是源码内置密钥的异或混淆。

格式（新写入只有 v2）：

- `enc:v2:<base64>` —— 载荷由系统密钥保护：
  * **Windows**：DPAPI（`CryptProtectData`，绑定当前用户 + 当前机器）。
    换机器、换用户账号都解不开——这正是本地凭据想要的属性。
  * **其他平台**：AES-256-GCM，主密钥来自 `<data_root>/secrets/master.key`（0600）。
    上 macOS / Linux 时应替换为 Keychain / Secret Service，接口不变。
- `enc:v1:<base64>` —— 历史异或混淆，**只读**兼容。下次写入同一字段自动升级。
- 无前缀 —— 更早的历史明文，原样返回（旧数据不破坏）。

**为什么删掉内置 fallback key**：v1 的密钥写在源码里，拿到数据库文件的人照源码
还原即可——把它当加密是自欺。现在宁可首次使用失败（抛 `SecretUnavailable`），
也不静默降级；`EDITH_SECRET_KEY` 仅保留用于解 v1 旧数据。
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

from ...config import data_root

logger = logging.getLogger(__name__)

V2_PREFIX = "enc:v2:"
V1_PREFIX = "enc:v1:"
#: v1 的混淆密钥。**只用于读旧数据**，新写入不再使用它。
_V1_FALLBACK_KEY = "edith-local-obfuscation-key-v1"
#: DPAPI 附加熵：让同一用户下别的应用即使调用 DPAPI 也解不开本应用的密文。
_DPAPI_ENTROPY = b"ethos-userconfig-v2"
#: CRYPTPROTECT_UI_FORBIDDEN：静默失败而不是弹系统对话框。
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_MASTER_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretUnavailable(RuntimeError):
    """系统密钥不可用（DPAPI 调用失败 / 主密钥建不出来 / 文件损坏）。

    这是**配置或环境问题**，不是"没有值"：调用方应把它当成显式错误上报，
    而不是退回到某种弱加密。
    """


# --------------------------------------------------------------------------- #
# Windows：DPAPI
# --------------------------------------------------------------------------- #


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:
    """调用 DPAPI 加/解密一段字节。仅 Windows 可用。"""
    if sys.platform != "win32":  # pragma: no cover - 由调用方分支保证
        raise SecretUnavailable("DPAPI 仅在 Windows 上可用")

    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 缓冲区必须活到调用结束：只传指针的话 Python 会在入参转换后回收它。
    source_buffer = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    source = DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_char))
    )
    entropy = DataBlob(
        len(_DPAPI_ENTROPY),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)),
    )
    output = DataBlob()

    if encrypt:
        description: object = ctypes.c_wchar_p("ETHOS credential")
        func = crypt32.CryptProtectData
    else:
        description = None  # CryptUnprotectData 的第二参是 LPWSTR*，传 None 表示不需要
        func = crypt32.CryptUnprotectData

    ok = func(
        ctypes.byref(source),
        description,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output),
    )
    if not ok:
        raise SecretUnavailable(
            f"DPAPI {'加密' if encrypt else '解密'}失败：{ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


# --------------------------------------------------------------------------- #
# 其他平台：AES-256-GCM + 本地主密钥
# --------------------------------------------------------------------------- #


def _master_key_path() -> Path:
    return data_root() / "secrets" / "master.key"


def _master_key() -> bytes:
    """读取（首次生成）AES 主密钥。原子写入，避免并发首次启动读到半截文件。"""
    path = _master_key_path()
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) != _MASTER_KEY_BYTES:
            raise SecretUnavailable(f"主密钥文件损坏（长度 {len(raw)}）：{path}")
        return raw

    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(_MASTER_KEY_BYTES)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # 非 POSIX 或权限受限：文件已经落下，不因为改权限失败而放弃
        logger.warning("chmod 0600 failed for %s", path)
    return key


def _aes_encrypt(data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_master_key()).encrypt(nonce, data, None)


def _aes_decrypt(payload: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(payload) <= _NONCE_BYTES:
        raise SecretUnavailable("密文长度不合法")
    nonce, ciphertext = payload[:_NONCE_BYTES], payload[_NONCE_BYTES:]
    return AESGCM(_master_key()).decrypt(nonce, ciphertext, None)


def _protect_bytes(data: bytes) -> bytes:
    return _dpapi(data, encrypt=True) if sys.platform == "win32" else _aes_encrypt(data)


def _unprotect_bytes(payload: bytes) -> bytes:
    return (
        _dpapi(payload, encrypt=False) if sys.platform == "win32" else _aes_decrypt(payload)
    )


# --------------------------------------------------------------------------- #
# v1 兼容（只读）
# --------------------------------------------------------------------------- #


def _v1_mask(raw: bytes) -> bytes:
    key = (os.environ.get("EDITH_SECRET_KEY") or _V1_FALLBACK_KEY).encode("utf-8")
    return (key * (len(raw) // len(key) + 1))[: len(raw)]


def _v1_unprotect(stored: str) -> str:
    try:
        raw = base64.b64decode(stored[len(V1_PREFIX) :])
    except Exception:  # noqa: BLE001 - 损坏数据按原样返回，不阻塞使用
        return stored
    return bytes(b ^ m for b, m in zip(raw, _v1_mask(raw))).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #


def protect(plain: str) -> str:
    """保护明文；空串直接返回空串（保持调用方 has_key / 非空判断的语义）。"""
    if not plain:
        return plain
    return V2_PREFIX + base64.b64encode(_protect_bytes(plain.encode("utf-8"))).decode("ascii")


def unprotect(stored: str) -> str:
    """还原密文。v1 旧值与历史明文都兼容；v2 解不开时返回空串并告警。"""
    if not stored:
        return stored
    if stored.startswith(V2_PREFIX):
        try:
            payload = base64.b64decode(stored[len(V2_PREFIX) :])
            return _unprotect_bytes(payload).decode("utf-8")
        except Exception:  # noqa: BLE001
            # 典型原因：数据库被拷到另一台机器/另一个用户下（DPAPI 按用户隔离）。
            # 返回空串而不是抛错：界面上显示"未配置"比整个设置页打不开更可解释。
            logger.warning("credential cannot be unprotected (wrong user/machine?)", exc_info=True)
            return ""
    if stored.startswith(V1_PREFIX):
        return _v1_unprotect(stored)
    return stored
