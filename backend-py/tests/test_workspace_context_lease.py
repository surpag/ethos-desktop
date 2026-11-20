"""快照接入 run 生命周期后的租约验收（P0-2 前置核验 §1.5 的 ①②③）。

为什么要有这一组：快照的调用点如果落在「取租约之后、`_run` 的 `try` 之前」，
探测失败就会绕过 `finally`——工作区租约永久不释放。用户修好绑定目录后再发一次，
只会一直拿到 409 `workspace_busy`，直到重启进程。后果与"一次报错"完全不同，
所以第三项必须用「失败之后还能再次执行」来证明，而不是只断言 `finally` 跑过了。

装配说明：`SessionLanes` / `WorkspaceLanes` / `SessionStore` / `RunEventLog` 全部
用真货，只替换「配置聚合」与「上下文取数端」。本组要证明的是租约生命周期，
不是模型调用——所以配置聚合直接返回一个最小 `ConfiguredRun` 替身。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

from edith_backend.config import data_root
from edith_backend.internal.agentrun.service import Dependencies, Service
from edith_backend.internal.agentrun.types import Request
from edith_backend.internal.agentstream.types import Event
from edith_backend.internal.session.schema import create_schema
from edith_backend.internal.session.store import SessionStore
from edith_backend.internal.workspace.context import (
    BindingRecord,
    BindingState,
    ProbeReason,
    RootProbe,
    WorkspaceUnavailableError,
    build_context,
)
from edith_backend.internal.workspace.schema import create_schema as create_workspace_schema
from edith_backend.internal.workspace.store import WorkspaceStore
from edith_backend.internal.workspace.volume_service import WorkspaceVolumeService

USER = "u1"
SESSION = "s1"
WORKSPACE = "w1"


class _StopRun(Exception):
    """在快照之后人为打断：用来证明"进了执行流程"而不是"卡在快照前"。"""


class _FakeUsage:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.failed: list[str] = []

    async def start(self, run) -> None:  # noqa: ANN001 - 只用到来电与否
        self.started.append(run.request_id)

    async def fail(self, request_id: str) -> None:
        self.failed.append(request_id)


class _FakeConfigurations:
    """配置聚合的替身：按**传进来的** request 造一个最小 `ConfiguredRun` 形状。

    必须用传入的 request，不能复用构造时的那个——否则 `_run` 里的 request_id
    与实际持有的租约对不上，收尾时会释放错键，测试会误报成"租约泄漏"。
    """

    async def load(self, request: Request, tracer) -> object:  # noqa: ANN001
        return SimpleNamespace(request=request, definition=SimpleNamespace(id="m1"))


class _StoreProxy:
    """真 store 的薄代理：只在 `set_task_status` 上打点/抛错。

    其余属性一律透传，避免为了测试重写一遍 store。
    """

    def __init__(self, inner: SessionStore, *, stop: bool = False) -> None:
        self._inner = inner
        self._stop = stop
        self.statuses: list[str] = []

    async def set_task_status(self, user_id: str, session_id: str, status: str) -> None:
        self.statuses.append(status)
        if self._stop:
            raise _StopRun("stop right after the workspace snapshot")
        await self._inner.set_task_status(user_id, session_id, status)

    def __getattr__(self, name: str):  # noqa: ANN401 - 透传
        return getattr(self._inner, name)


class _Source:
    """上下文取数端替身：按需返回快照或抛 `WorkspaceUnavailableError`。"""

    def __init__(self, *, context=None, error: Exception | None = None) -> None:
        self._context = context
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def capture(self, user_id: str, workspace_id: str):
        self.calls.append((user_id, workspace_id))
        if self._error is not None:
            raise self._error
        return self._context


def _ready_context() -> object:
    return build_context(
        BindingRecord(workspace_id=WORKSPACE, local_root=str(Path("C:/bound/ws1"))),
        RootProbe(ProbeReason.OK),
    )


def _missing_error() -> WorkspaceUnavailableError:
    context = build_context(
        BindingRecord(workspace_id=WORKSPACE, local_root=str(Path("C:/bound/gone"))),
        RootProbe(ProbeReason.NOT_EXISTS, "stat: path does not exist"),
    )
    return WorkspaceUnavailableError(context)


@dataclass
class _Harness:
    service: Service
    store: _StoreProxy
    usage: _FakeUsage
    db: aiosqlite.Connection


async def _harness(*, source: _Source, stop_after_snapshot: bool = False) -> _Harness:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_schema(db)
    store = _StoreProxy(SessionStore(db), stop=stop_after_snapshot)
    usage = _FakeUsage()
    service = Service(
        Dependencies(
            models=SimpleNamespace(),
            settings=SimpleNamespace(),
            providers=SimpleNamespace(),
            store=store,  # type: ignore[arg-type]
            usage=usage,  # type: ignore[arg-type]
            workspace_contexts=source,
        )
    )
    service.configurations = _FakeConfigurations()  # type: ignore[assignment]
    return _Harness(service=service, store=store, usage=usage, db=db)


def _request(request_id: str, *, session_id: str = SESSION) -> Request:
    return Request(
        request_id=request_id,
        user_id=USER,
        session_id=session_id,
        message="看一下这个项目",
        workspace_id=WORKSPACE,
    )


async def _start_and_wait(harness: _Harness, request: Request) -> tuple[object, list[Event]]:
    stream, error = await harness.service.run(request)
    assert error is None, error
    # active 会在 finally 里被摘掉，所以必须先取 task 再 await
    task = harness.service.active[request.request_id].task
    await task
    events: list[Event] = []
    async for event in stream.open(0):  # type: ignore[union-attr]
        if event is None:
            break
        events.append(event)
    return stream, events


def test_snapshot_success_enters_run_and_releases_leases() -> None:
    """① 快照成功后进入执行流程；run 结束（含异常收尾）两级租约都释放。"""

    async def go() -> None:
        harness = await _harness(source=_Source(context=_ready_context()), stop_after_snapshot=True)
        try:
            _, events = await _start_and_wait(harness, _request("req-1"))

            # 进入了执行流程：set_task_status("in_progress") 被调用过一次
            assert harness.store.statuses == ["in_progress"]
            assert any(e.type == "run.started" for e in events)
            # 后续人为失败走的是通用异常收尾，不是 workspace_unavailable
            assert any(
                e.type == "run.error"
                and e.error is not None
                and e.error.type == "internal_error"
                for e in events
            )
            # 租约全部释放
            assert harness.service.lanes.active == {}
            assert harness.service.workspace_lanes.active == {}
        finally:
            await harness.db.close()

    asyncio.run(go())


def test_snapshot_failure_releases_both_leases() -> None:
    """② 快照失败：报 workspace_unavailable，两级租约都释放，且没进 in_progress。"""

    async def go() -> None:
        source = _Source(error=_missing_error())
        harness = await _harness(source=source)
        try:
            _, events = await _start_and_wait(harness, _request("req-1"))

            # 取数端确实被调用过一次（不是走"没有工作区就跳过"的分支）
            assert source.calls == [(USER, WORKSPACE)]

            # 首个事件仍是 run.started：失败本身也是一次 run，前端流要有主事件
            assert events[0].type == "run.started"

            error_event = [e for e in events if e.type == "run.error"]
            assert len(error_event) == 1
            assert error_event[0].error is not None
            assert error_event[0].error.type == "workspace_unavailable"
            # 文案要能让用户动手：说清是哪个状态、该去检查绑定目录
            assert "bound_missing" in error_event[0].error.message
            assert "重新绑定" in error_event[0].error.message

            # 没有把会话留在 in_progress（否则侧边栏会一直显示"还有没跑完的活"）
            assert harness.store.statuses == []

            # 两级租约都释放了
            assert harness.service.lanes.active == {}
            assert harness.service.workspace_lanes.active == {}

            # 用量按失败收尾，不留一条永远 running 的 run 行
            assert harness.usage.failed == ["req-1"]
        finally:
            await harness.db.close()

    asyncio.run(go())


def test_run_can_start_again_after_snapshot_failure() -> None:
    """③ 失败之后同一用户/会话/工作区能再次执行——证明没有残留锁。"""

    async def go() -> None:
        failing = _Source(error=_missing_error())
        # stop=True：第二次也停在快照之后，避免用例依赖更深层的失败点
        harness = await _harness(source=failing, stop_after_snapshot=True)
        try:
            await _start_and_wait(harness, _request("req-1"))
            assert harness.service.lanes.active == {}
            assert harness.service.workspace_lanes.active == {}

            # 第二次：同一个会话 + 同一个工作区，换成能成功的取数端
            harness.service.workspace_contexts = _Source(context=_ready_context())
            stream, error = await harness.service.run(_request("req-2"))
            assert error is None, error  # 不是 session_busy / workspace_busy
            assert stream is not None
            task = harness.service.active["req-2"].task
            await task
            assert harness.service.lanes.active == {}
            assert harness.service.workspace_lanes.active == {}

            # 工作区互斥确实放开：换一个会话来拿同一工作区的租约也应当成功
            # （同会话可重入，用它验证会把"没释放"掩盖成"释放了"）
            assert harness.service.workspace_lanes.acquire(USER, WORKSPACE, "other-session")
            harness.service.workspace_lanes.release(USER, WORKSPACE, "other-session")
        finally:
            await harness.db.close()

    asyncio.run(go())


# ---- 取数端与领域对象的接缝：真 store + 真服务，只有磁盘是临时目录 ----


async def _workspace_db(*, local_root: str | None) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_workspace_schema(db)
    await db.execute(
        "INSERT INTO workspaces (workspace_id, user_id, name, local_root) "
        "VALUES (?, ?, ?, ?)",
        (WORKSPACE, USER, "演示", local_root),
    )
    await db.commit()
    return db


def test_capture_bound_root_missing_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """绑定目录不存在：状态 missing，**根仍是绑定路径**，绝不回退到 Volume。"""
    monkeypatch.setenv("ETHOS_DATA_DIR", str(tmp_path / "data"))

    async def go() -> None:
        gone = tmp_path / "gone"
        db = await _workspace_db(local_root=str(gone))
        try:
            service = WorkspaceVolumeService(WorkspaceStore(db))
            context = await service.capture(USER, WORKSPACE)

            assert context.state is BindingState.BOUND_MISSING
            assert context.executable is False
            assert context.root == str(gone)
            assert "workspace-volumes" not in (context.root or "")
            assert "not_exists" not in context.detail  # detail 是原始错误文本
            assert "stat" in context.detail
            # 探测不创建目录
            assert not gone.exists()

            with pytest.raises(WorkspaceUnavailableError) as info:
                raise WorkspaceUnavailableError(context)
            assert "bound_missing" in str(info.value)
        finally:
            await db.close()

    asyncio.run(go())


def test_capture_unbound_is_unchanged_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    """未绑定：状态 unbound，根仍指向 workspace-volumes/{id}，且此处不创建目录。"""
    data = tmp_path / "data"
    monkeypatch.setenv("ETHOS_DATA_DIR", str(data))

    async def go() -> None:
        db = await _workspace_db(local_root=None)
        try:
            service = WorkspaceVolumeService(WorkspaceStore(db))
            context = await service.capture(USER, WORKSPACE)

            assert context.state is BindingState.UNBOUND
            assert context.executable is True
            assert context.root is not None
            assert Path(context.root) == (data_root() / "workspace-volumes" / WORKSPACE).resolve()
            # 快照只拼字符串：按需创建仍归执行链（S1 不动）
            assert not Path(context.root).exists()
            assert context.detail == ""
        finally:
            await db.close()

    asyncio.run(go())
