"""图片字节的本机存储。

上游把图片放在腾讯云 COS：浏览器拿预签名 URL 直传，服务端只存 object_key，
读取时再签一个短期 GET URL。桌面端没有对象存储，也不需要预签名——字节就落在
`data_root()/images/` 下，服务端自己收、自己发。

**保留「预约 → 写入 → 确认」三段式**，不是因为要兼容 COS，而是因为三段各自
守着不同的失败面：预约管类型与声明大小，写入管真实字节（超限/中断/伪装），
确认管「文件真的在盘上且与预约一致」。合并成一步的话，「字节写了一半」这个
中间态就没有地方安放，只能靠记录已 confirmed 来假装成功。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

# 嗅探所需的头部长度：WebP 的 "WEBP" 在第 8..12 字节，取 16 足够。
_SNIFF_BYTES = 16


class ImageTooLarge(Exception):
    """写入的字节数超过上限。"""


class ImageCorrupt(Exception):
    """字节内容与声明的图片类型不符（或根本不是受支持的图片）。"""


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    mime_type: str


def sniff_mime(head: bytes) -> str | None:
    """按魔数判断图片类型；不认识的返回 None。

    只信字节，不信 Content-Type：浏览器提交的头部由客户端说了算，把 HTML
    伪装成 image/png 存下来再原样吐回去，就是一个存储型 XSS。
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class LocalStorage:
    """把 object_key 映射为目录下的一个文件。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def target(self, key: str) -> Path:
        """object_key → 落盘路径。

        只取最后一段：历史记录里的 `chat-images/<id>` 与现在的 `images/<id>`
        会解析到同一个文件，因此换布局不需要数据迁移。反过来说，key 里带
        `..` 或分隔符也没有逃逸空间——落盘位置由 id 决定，不由 key 决定。
        """
        name = key.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name in {".", ".."}:
            raise ValueError(f'invalid object key "{key}"')
        return self._root / name

    def stat(self, key: str) -> StoredObject:
        path = self.target(key)
        try:
            with path.open("rb") as handle:
                head = handle.read(_SNIFF_BYTES)
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"object {key} not found") from exc
        mime = sniff_mime(head)
        if mime is None:
            raise ImageCorrupt(f'object {key} is not a supported image')
        return StoredObject(size_bytes=size, mime_type=mime)

    def read(self, key: str) -> tuple[bytes, str]:
        path = self.target(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"object {key} not found") from exc
        mime = sniff_mime(data[:_SNIFF_BYTES])
        if mime is None:
            raise ImageCorrupt(f'object {key} is not a supported image')
        return data, mime

    async def write(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
        expect_mime: str | None = None,
    ) -> StoredObject:
        """把字节流写到 key，边写边计数。

        先写 `.part` 再 `os.replace`：`os.replace` 在同一文件系统内是原子的，
        所以读取方永远看不到半截文件。超限时立刻中止，而不是读完整流再看长度
        ——否则一个声明 1 KB 的请求可以先把磁盘写满。

        `expect_mime` 给定时要求魔数与之一致：声明 png 实际传 jpeg 的请求在
        写入阶段就该被挡掉，而不是等到确认阶段——那时候 client 已经在等
        「上传成功」的下一步了，错误信息离现场越远越难查。
        """
        path = self.target(key)
        partial = path.with_name(path.name + ".part")
        total = 0
        head = b""
        try:
            with partial.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageTooLarge(f"image exceeds {max_bytes} bytes")
                    if len(head) < _SNIFF_BYTES:
                        head += chunk[:_SNIFF_BYTES - len(head)]
                    handle.write(chunk)
            if total == 0:
                raise ImageCorrupt("uploaded image is empty")
            mime = sniff_mime(head)
            if mime is None:
                raise ImageCorrupt("uploaded bytes are not a supported image")
            if expect_mime is not None and mime != expect_mime:
                raise ImageCorrupt(
                    f'uploaded bytes are {mime}, not the reserved {expect_mime}'
                )
            os.replace(partial, path)
        except BaseException:
            _unlink_quietly(partial)
            raise
        return StoredObject(size_bytes=total, mime_type=mime)


    def delete(self, key: str) -> None:
        _unlink_quietly(self.target(key))


def _unlink_quietly(path: Path) -> None:
    """删除失败只跳过。

    收尾清理吞掉的只能是 `OSError`：安全软件/环境护栏抛的其它异常若被吞，
    会把真正的故障藏起来；若往上抛，又会把「删除失败」升级成「本次请求失败」
    ——而它的语义只是「尽力而为」。
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
