"""images 模块：本机存储的上传/确认/读取全流程与各类拒绝分支。"""

from __future__ import annotations

import asyncio

import pytest

from edith_backend.internal.images.agent_input import (
    image_id_from_reference,
    reference,
    to_data_url,
)
from edith_backend.internal.images.service import (
    MAX_IMAGE_BYTES,
    ImageTooLarge,
    max_image_bytes,
    supported_mime_type,
)
from edith_backend.internal.images.storage import ImageCorrupt, LocalStorage, sniff_mime
from edith_backend.internal.images.types import UploadInput

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4

USER = "u-images"
SESSION = "s-images"


def test_supported_mime_types() -> None:
    assert supported_mime_type("image/jpeg")
    assert supported_mime_type("image/png")
    assert supported_mime_type("image/webp")
    assert not supported_mime_type("image/gif")
    assert not supported_mime_type("text/plain")


def test_max_image_bytes() -> None:
    assert max_image_bytes() == 10 << 20 == MAX_IMAGE_BYTES


def test_reference_roundtrip() -> None:
    marker = reference("img_abc")
    assert marker == "edith-image://img_abc"
    image_id, ok = image_id_from_reference(marker)
    assert ok
    assert image_id == "img_abc"


def test_image_id_from_reference_rejects_plain() -> None:
    _image_id, ok = image_id_from_reference("img_abc")
    assert not ok


# --- 魔数嗅探 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp"),
     (b"<html>", None), (b"", None)],
)
def test_sniff_mime(data: bytes, expected: str | None) -> None:
    assert sniff_mime(data) == expected


# --- 落盘路径 ---------------------------------------------------------------


def test_storage_key_uses_last_segment(tmp_path) -> None:
    """历史 key 带 `chat-images/` 前缀，新 key 带 `images/`：同一个 id 落到同一个文件。"""
    storage = LocalStorage(tmp_path)
    assert storage.target("images/img_1") == storage.target("chat-images/img_1")
    assert storage.target("img_1") == tmp_path / "img_1"


@pytest.mark.parametrize("key", ["", "/", "images/..", "a/.."])
def test_storage_key_rejects_escape(tmp_path, key: str) -> None:
    with pytest.raises(ValueError):
        LocalStorage(tmp_path).target(key)


def _chunks(data: bytes, size: int = 4):
    async def generator():
        for start in range(0, len(data), size):
            yield data[start:start + size]
    return generator()


def test_write_is_atomic_on_reject(tmp_path) -> None:
    """超限时既不能留下正式文件，也不能留下 `.part` 残渣。"""
    storage = LocalStorage(tmp_path)
    with pytest.raises(ImageTooLarge):
        asyncio.run(storage.write("images/img_1", _chunks(b"\x00" * 64), 32))
    assert list(tmp_path.iterdir()) == []


def test_write_rejects_non_image(tmp_path) -> None:
    storage = LocalStorage(tmp_path)
    with pytest.raises(ImageCorrupt):
        asyncio.run(storage.write("images/img_1", _chunks(b"<html>" * 4), 1024))
    assert list(tmp_path.iterdir()) == []


def test_data_url() -> None:
    assert to_data_url(b"ab", "image/png") == "data:image/png;base64,YWI="


# --- HTTP 全流程 ------------------------------------------------------------


def _reserve(client, *, mime: str = "image/png", size: int = len(PNG), user: str = USER,
             session: str = SESSION) -> str:
    response = client.post("/internal/images", json={
        "userId": user, "sessionId": session, "mimeType": mime, "sizeBytes": size,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    image_id = body["image"]["id"]
    assert body["uploadUrl"] == f"/api/images/{image_id}/blob"
    return image_id


def _put(client, image_id: str, data: bytes, *, user: str = USER,
         content_type: str = "image/png"):
    return client.put(
        f"/internal/images/{image_id}/blob", params={"userId": user},
        content=data, headers={"Content-Type": content_type},
    )


def _complete(client, image_id: str, *, user: str = USER):
    return client.post(f"/internal/images/{image_id}/complete", json={"userId": user})


def test_upload_flow_end_to_end(client) -> None:
    image_id = _reserve(client)

    assert _put(client, image_id, PNG).status_code == 204
    # 未确认之前不可读：中间态不该被当成可用的图。
    assert client.get(f"/internal/images/{image_id}", params={"userId": USER}).status_code == 404

    completed = _complete(client, image_id)
    assert completed.status_code == 200, completed.text
    assert completed.json()["image"] == {"id": image_id, "mimeType": "image/png"}

    read = client.get(f"/internal/images/{image_id}", params={"userId": USER})
    assert read.status_code == 200
    assert read.content == PNG
    assert read.headers["content-type"] == "image/png"
    assert "immutable" in read.headers["cache-control"]


def test_reserve_rejects_unsupported_mime(client) -> None:
    response = client.post("/internal/images", json={
        "userId": USER, "sessionId": SESSION, "mimeType": "image/gif", "sizeBytes": 10,
    })
    assert response.status_code == 400
    assert "unsupported image mime" in response.json()["message"]


def test_reserve_rejects_oversize_declaration(client) -> None:
    response = client.post("/internal/images", json={
        "userId": USER, "sessionId": SESSION, "mimeType": "image/png",
        "sizeBytes": MAX_IMAGE_BYTES + 1,
    })
    assert response.status_code == 400


def test_put_rejects_disguised_bytes(client) -> None:
    """声明 png、实际是 jpeg：按魔数拒绝，别指望改后缀能混过去。"""
    image_id = _reserve(client)
    assert _put(client, image_id, JPEG).status_code == 400
    # 拒绝后记录被清掉：这张 id 不该停在 pending，也不该还能被确认。
    assert _complete(client, image_id).status_code == 404


def test_put_rejects_content_type_mismatch(client) -> None:
    image_id = _reserve(client)
    assert _put(client, image_id, PNG, content_type="text/html").status_code == 400
    assert _complete(client, image_id).status_code == 400


def test_put_rejects_oversize_stream(client) -> None:
    image_id = _reserve(client)
    too_big = PNG + b"\x00" * MAX_IMAGE_BYTES
    assert _put(client, image_id, too_big).status_code == 413


def test_complete_requires_matching_size(client) -> None:
    image_id = _reserve(client, size=len(PNG) + 1)
    assert _put(client, image_id, PNG).status_code == 204
    response = _complete(client, image_id)
    assert response.status_code == 400
    assert "does not match reservation" in response.json()["message"]


def test_complete_without_upload_discards_record(client) -> None:
    image_id = _reserve(client)
    assert _complete(client, image_id).status_code == 400  # 没有字节
    assert _complete(client, image_id).status_code == 404  # 记录已随失败删除，不是 500


def test_complete_is_idempotent(client) -> None:
    image_id = _reserve(client)
    _put(client, image_id, PNG)
    assert _complete(client, image_id).status_code == 200
    again = _complete(client, image_id)
    assert again.status_code == 200
    assert again.json()["image"]["id"] == image_id


def test_repair_after_failed_complete(client) -> None:
    """大小不符被丢弃后，重新预约 + 重传应当成功（失败不该留下毒记录）。"""
    first = _reserve(client, size=len(PNG) + 5)
    _put(client, first, PNG)
    assert _complete(client, first).status_code == 400

    second = _reserve(client)
    assert _put(client, second, PNG).status_code == 204
    assert _complete(client, second).status_code == 200


def test_read_other_user_is_not_found(client) -> None:
    image_id = _reserve(client)
    _put(client, image_id, PNG)
    _complete(client, image_id)
    assert client.get(f"/internal/images/{image_id}", params={"userId": "u-other"}).status_code == 404


def test_put_other_user_rejected(client) -> None:
    image_id = _reserve(client)
    assert _put(client, image_id, PNG, user="u-other").status_code == 404
