"""pytest 共享 fixture。

app 是全局单例，其 lifespan（创建模块 + 注册路由 + 打开数据库）只应运行一次。
因此用 session 级 fixture 让所有测试文件共享同一个 TestClient 和同一个数据库，
避免多个 module fixture 重复触发 lifespan 导致路由重复注册、闭包捕获已关闭的连接。

各测试通过独立 userId 隔离数据。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from edith_backend.main import app


@pytest.fixture(scope="session")
def client(tmp_path_factory) -> TestClient:
    data_dir = tmp_path_factory.mktemp("data")
    db_path = data_dir / "edith-test.db"
    previous_db = os.environ.get("EDITH_DB_PATH")
    os.environ["EDITH_DB_PATH"] = str(db_path)
    # 整个数据根一起重定向：`data_root()` 底下还有 `skills/` 与 `workspace-volumes/`，
    # 少设这一条时它们会写进仓库（skills 尤其隐蔽——看起来像"用户建的东西"）。
    previous_root = os.environ.get("ETHOS_DATA_DIR")
    os.environ["ETHOS_DATA_DIR"] = str(data_dir)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for name, value in (
            ("EDITH_DB_PATH", previous_db),
            ("ETHOS_DATA_DIR", previous_root),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
