"""只依赖 ctypes 的 Windows DACL 精确读写。

为什么不用 `icacls` 做判定（`win_acl` 之前的做法）：

1. `icacls` 的输出是**本地化文本**，只能按主体名字符串和 `(N)` 片段匹配 —— 不校验
   SID、权限掩码、继承标志。同名不同 SID（账户删了重建）、`(DENY)(S,DC)` 这种形态
   都会误判（实测 `.config\\gcloud` 明明有继承 DENY 却被判成"未加固"）。
2. `icacls /remove:d <账户>` 会删掉该主体**全部** DENY ACE，包括别的程序加的。
   要"只撤销自己创建的那一条"，必须按 ACE 三要素精确匹配后重建 ACL。

这里只做三件事：读、精确删一条、加一条。全部走 `advapi32`，不引入 pywin32。

安全约定：
- 只删除**非继承**的 ACE（继承来的 ACE 由父目录决定，单独删会被系统拒绝或造成不一致）；
- 删除前要求匹配到的 ACE **恰好一条**，多于一条直接报错（宁可不动手）；
- 所有失败抛 `WinDaclError`，由调用方决定 fail-closed。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
from dataclasses import dataclass

if sys.platform != "win32":  # pragma: no cover - 模块只在 Windows 有意义
    raise ImportError("win_dacl 仅支持 Windows")

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000

ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1

OBJECT_INHERIT_ACE = 0x01
CONTAINER_INHERIT_ACE = 0x02
NO_PROPAGATE_INHERIT_ACE = 0x04
INHERIT_ONLY_ACE = 0x08
INHERITED_ACE = 0x10

SE_DACL_PROTECTED = 0x1000
ACL_REVISION = 2
ACL_REVISION_DS = 4
_ACL_INFORMATION_CLASS_SIZE = 2

ACL_MASK_ALL = 0x1F01FF  # FILE_ALL_ACCESS，仅用于测试/加固


class WinDaclError(RuntimeError):
    """DACL 读写失败。`code` 是 Win32 错误码（0 表示逻辑错误）。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(f"{message} (win32={code})" if code else message)
        self.code = code


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _ACCESS_ACE(ctypes.Structure):
    _fields_ = [
        ("Header", _ACE_HEADER),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _ACL(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_ushort),
        ("AceCount", ctypes.c_ushort),
        ("Sbz2", ctypes.c_ushort),
    ]


_PSID = ctypes.c_void_p
_PACL = ctypes.c_void_p
_PSECURITY_DESCRIPTOR = ctypes.c_void_p

_advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(_PSID),
    ctypes.POINTER(_PSID),
    ctypes.POINTER(_PACL),
    ctypes.POINTER(_PACL),
    ctypes.POINTER(_PSECURITY_DESCRIPTOR),
]
_advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
_advapi32.SetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    _PSID,
    _PSID,
    _PACL,
    _PACL,
]
_advapi32.GetAclInformation.restype = wintypes.BOOL
_advapi32.GetAclInformation.argtypes = [
    _PACL,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_int,
]
_advapi32.GetAce.restype = wintypes.BOOL
_advapi32.GetAce.argtypes = [_PACL, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.InitializeAcl.restype = wintypes.BOOL
_advapi32.InitializeAcl.argtypes = [_PACL, wintypes.DWORD, wintypes.DWORD]
_advapi32.AddAce.restype = wintypes.BOOL
_advapi32.AddAce.argtypes = [_PACL, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
_advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
_advapi32.GetSecurityDescriptorControl.argtypes = [
    _PSECURITY_DESCRIPTOR,
    ctypes.POINTER(wintypes.WORD),
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.LookupAccountNameW.restype = wintypes.BOOL
_advapi32.LookupAccountNameW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.LocalFree.restype = ctypes.c_void_p
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]


@dataclass(frozen=True)
class Ace:
    """一条 DACL ACE。`mask`/`ace_flags` 是精确匹配需要的原始值。"""

    index: int
    ace_type: int
    mask: int
    ace_flags: int
    sid: str

    @property
    def is_deny(self) -> bool:
        return self.ace_type == ACCESS_DENIED_ACE_TYPE

    @property
    def is_inherited(self) -> bool:
        return bool(self.ace_flags & INHERITED_ACE)

    @property
    def inheritance_flags(self) -> int:
        return self.ace_flags & (
            OBJECT_INHERIT_ACE
            | CONTAINER_INHERIT_ACE
            | NO_PROPAGATE_INHERIT_ACE
            | INHERIT_ONLY_ACE
        )


@dataclass(frozen=True)
class DaclView:
    aces: tuple[Ace, ...]
    protected: bool
    revision: int

    def deny_aces_for(self, sid: str) -> tuple[Ace, ...]:
        return tuple(a for a in self.aces if a.is_deny and a.sid.casefold() == sid.casefold())


def _check(ok: bool, what: str) -> None:
    if not ok:
        raise WinDaclError(what, ctypes.get_last_error())


def _sid_to_str(sid_ptr: int) -> str:
    out = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(out)):
        raise WinDaclError("ConvertSidToStringSidW 失败", ctypes.get_last_error())
    try:
        return out.value or ""
    finally:
        _kernel32.LocalFree(out)


def sid_from_string(sid: str) -> int:
    ptr = ctypes.c_void_p()
    if not _advapi32.ConvertStringSidToSidW(sid, ctypes.byref(ptr)):
        raise WinDaclError(f"无效的 SID: {sid}", ctypes.get_last_error())
    return int(ptr.value or 0)


def lookup_sid(account: str) -> str | None:
    """`MACHINE\\user` → SID 字符串；账户不存在或查询失败返回 None。

    用 SID 而不是账户名做匹配：账户删掉重建后名字相同但 SID 不同，
    按名字匹配会误判成"已加固"。
    """
    sid_buf = ctypes.create_string_buffer(68)
    sid_size = wintypes.DWORD(len(sid_buf))
    domain = ctypes.create_unicode_buffer(256)
    domain_size = wintypes.DWORD(256)
    use = wintypes.DWORD()
    ok = _advapi32.LookupAccountNameW(
        None,
        account,
        ctypes.addressof(sid_buf),
        ctypes.byref(sid_size),
        domain,
        ctypes.byref(domain_size),
        ctypes.byref(use),
    )
    if not ok:
        return None
    return _sid_to_str(ctypes.addressof(sid_buf))


def read_dacl(path: str) -> DaclView:
    """读取对象的 DACL（只读）。不存在的对象抛 `WinDaclError`。"""
    dacl = _PACL()
    sd = _PSECURITY_DESCRIPTOR()
    rc = _advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(sd),
    )
    if rc != 0:
        raise WinDaclError(f"读取 DACL 失败: {path}", rc)
    try:
        if not dacl.value:
            return DaclView(aces=(), protected=False, revision=ACL_REVISION)

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        protected = False
        if _advapi32.GetSecurityDescriptorControl(
            sd, ctypes.byref(control), ctypes.byref(revision)
        ):
            protected = bool(control.value & SE_DACL_PROTECTED)

        size_info = _ACL_SIZE_INFORMATION()
        _check(
            bool(
                _advapi32.GetAclInformation(
                    dacl,
                    ctypes.byref(size_info),
                    ctypes.sizeof(size_info),
                    _ACL_INFORMATION_CLASS_SIZE,
                )
            ),
            "GetAclInformation 失败",
        )

        aces: list[Ace] = []
        for index in range(size_info.AceCount):
            ace_ptr = ctypes.c_void_p()
            _check(bool(_advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr))), "GetAce 失败")
            header = ctypes.cast(ace_ptr, ctypes.POINTER(_ACE_HEADER)).contents
            body = ctypes.cast(ace_ptr, ctypes.POINTER(_ACCESS_ACE)).contents
            sid_ptr = int(ace_ptr.value or 0) + _ACCESS_ACE.SidStart.offset
            aces.append(
                Ace(
                    index=index,
                    ace_type=header.AceType,
                    mask=body.Mask,
                    ace_flags=header.AceFlags,
                    sid=_sid_to_str(sid_ptr),
                )
            )
        return DaclView(aces=tuple(aces), protected=protected, revision=ACL_REVISION)
    finally:
        _kernel32.LocalFree(ctypes.c_void_p(sd.value))


def _rewrite_dacl(path: str, skip: tuple[int, ...] = (), add_deny: tuple[str, int, int] | None = None) -> None:
    """重建 DACL：跳过 `skip` 里的下标，可选追加一条显式 DENY ACE。

    只改 DACL，不碰 Owner / Group / SACL；继承标志按原样保留（`protected` 也保留）。
    """
    dacl = _PACL()
    sd = _PSECURITY_DESCRIPTOR()
    rc = _advapi32.GetNamedSecurityInfoW(
        str(path),
        SE_FILE_OBJECT,
        DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(sd),
    )
    if rc != 0:
        raise WinDaclError(f"读取 DACL 失败: {path}", rc)
    try:
        if not dacl.value:
            raise WinDaclError(f"对象没有 DACL，拒绝猜测重建: {path}")

        size_info = _ACL_SIZE_INFORMATION()
        _check(
            bool(
                _advapi32.GetAclInformation(
                    dacl,
                    ctypes.byref(size_info),
                    ctypes.sizeof(size_info),
                    _ACL_INFORMATION_CLASS_SIZE,
                )
            ),
            "GetAclInformation 失败",
        )

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        protected = False
        if _advapi32.GetSecurityDescriptorControl(
            sd, ctypes.byref(control), ctypes.byref(revision)
        ):
            protected = bool(control.value & SE_DACL_PROTECTED)

        entries: list[tuple[int, int]] = []
        total = 0
        for index in range(size_info.AceCount):
            ace_ptr = ctypes.c_void_p()
            _check(bool(_advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr))), "GetAce 失败")
            header = ctypes.cast(ace_ptr, ctypes.POINTER(_ACE_HEADER)).contents
            if index in skip:
                continue
            entries.append((int(ace_ptr.value or 0), header.AceSize))
            total += header.AceSize

        if add_deny is not None:
            sid, mask, flags = add_deny
            sid_ptr = sid_from_string(sid)
            try:
                ace_size = 4 + 4 + 8  # header + mask + SID（S-1-5-21-... 不超过 8 字节）
                total += ace_size
            finally:
                _kernel32.LocalFree(ctypes.c_void_p(sid_ptr))

        # 新 ACL 用 ACL_REVISION（不含对象 ACE，文件 ACE 都适用）
        capacity = total + 64
        new_acl = ctypes.create_string_buffer(capacity)
        _check(
            bool(_advapi32.InitializeAcl(ctypes.byref(new_acl), capacity, ACL_REVISION)),
            "InitializeAcl 失败",
        )
        for ace_addr, ace_size in entries:
            _check(
                bool(
                    _advapi32.AddAce(
                        ctypes.byref(new_acl), ACL_REVISION, 0xFFFFFFFF, ctypes.c_void_p(ace_addr), ace_size
                    )
                ),
                "AddAce 失败",
            )

        if add_deny is not None:
            sid, mask, flags = add_deny
            sid_ptr = sid_from_string(sid)
            try:
                sid_len = 8 + 4 * int(
                    (ctypes.cast(ctypes.c_void_p(sid_ptr), ctypes.POINTER(ctypes.c_ubyte))[1])
                )
            finally:
                _kernel32.LocalFree(ctypes.c_void_p(sid_ptr))
            ace_buf = ctypes.create_string_buffer(8 + sid_len)
            header = ctypes.cast(ace_buf, ctypes.POINTER(_ACE_HEADER)).contents
            header.AceType = ACCESS_DENIED_ACE_TYPE
            header.AceFlags = flags
            header.AceSize = 8 + sid_len
            body = ctypes.cast(ace_buf, ctypes.POINTER(_ACCESS_ACE)).contents
            body.Mask = mask
            sid_ptr = sid_from_string(sid)
            try:
                ctypes.memmove(ctypes.addressof(ace_buf) + 8, sid_ptr, sid_len)
            finally:
                _kernel32.LocalFree(ctypes.c_void_p(sid_ptr))
            _check(
                bool(
                    _advapi32.AddAce(
                        ctypes.byref(new_acl),
                        ACL_REVISION,
                        0xFFFFFFFF,
                        ctypes.addressof(ace_buf),
                        8 + sid_len,
                    )
                ),
                "AddAce(deny) 失败",
            )

        info = DACL_SECURITY_INFORMATION
        if protected:
            info |= PROTECTED_DACL_SECURITY_INFORMATION
        rc = _advapi32.SetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            info,
            None,
            None,
            ctypes.byref(new_acl),
            None,
        )
        if rc != 0:
            raise WinDaclError(f"写回 DACL 失败: {path}", rc)
    finally:
        _kernel32.LocalFree(ctypes.c_void_p(sd.value))


def add_deny_ace(path: str, sid: str, *, mask: int = ACL_MASK_ALL, flags: int = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE) -> None:
    """追加一条显式 DENY ACE（不检查重复，调用方负责幂等）。"""
    _rewrite_dacl(path, add_deny=(sid, mask, flags))


def _matching(view: DaclView, sid: str, ace_type: int, mask: int, ace_flags: int) -> list[Ace]:
    return [
        a
        for a in view.aces
        if a.sid.casefold() == sid.casefold()
        and a.ace_type == ace_type
        and a.mask == mask
        and a.ace_flags == ace_flags
    ]


def remove_ace_exact(
    path: str,
    *,
    sid: str,
    ace_type: int,
    mask: int,
    ace_flags: int,
    all_matches: bool = False,
) -> int:
    """删除与三要素完全匹配的 ACE，返回删除条数。

    匹配条件：SID（大小写不敏感）+ ACE 类型 + 掩码 + 继承标志（含 INHERITED 位）。
    `all_matches=False` 时要求匹配**恰好一条**（多条直接报错，宁可不动手）；
    继承来的 ACE 一律拒绝删除（`WinDaclError`）——它们由父目录决定。
    """
    view = read_dacl(path)
    hits = _matching(view, sid, ace_type, mask, ace_flags)
    if not hits:
        return 0
    if len(hits) > 1 and not all_matches:
        raise WinDaclError(f"匹配到 {len(hits)} 条相同 ACE，拒绝盲删: {path}")
    if any(a.is_inherited for a in hits):
        raise WinDaclError(f"目标是继承 ACE，不能单独删除: {path}")
    skip = tuple(a.index for a in hits) if all_matches else (hits[0].index,)
    _rewrite_dacl(path, skip=skip)
    return len(skip)


def dedupe_matching(path: str, *, sid: str, ace_type: int, mask: int, ace_flags: int) -> int:
    """把完全相同的多条 ACE 收敛成一条，返回删除条数。

    用于修复历史遗留：早期版本对文件也写 `(OI)(CI)` 标志，系统把它规范化成 0，
    幂等判定匹配不上就又加了一条。这些 ACE 形状完全一致，删掉多余的是安全的。
    """
    view = read_dacl(path)
    hits = _matching(view, sid, ace_type, mask, ace_flags)
    if len(hits) <= 1:
        return 0
    if any(a.is_inherited for a in hits[1:]):
        raise WinDaclError(f"命中继承 ACE，拒绝去重: {path}")
    _rewrite_dacl(path, skip=tuple(a.index for a in hits[1:]))
    return len(hits) - 1
