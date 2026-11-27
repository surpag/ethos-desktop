"""环境变量加载与配置读取。

对应 Go 版 cmd/server/config.go。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

APP_DIR_NAME = "ETHOS"


def project_root() -> Path:
    """backend-py 目录（本文件位于 backend-py/src/edith_backend/）。

    frozen（PyInstaller / Nuitka）后 `__file__` 落在临时解包目录，
    `parents[2]` 会推导到不存在的路径；改用可执行文件所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def is_desktop() -> bool:
    """是否运行在桌面壳内（由 Electron 主进程注入）。"""
    return os.getenv("ETHOS_DESKTOP") == "1"


def data_root() -> Path:
    """**可变数据**的根目录：数据库、工作区、用户 skill。

    三种来源，优先级从高到低：

    1. `ETHOS_DATA_DIR` —— 显式指定（测试或多实例调试）；
    2. 桌面模式 —— `%APPDATA%\\ETHOS`。安装目录可能是只读的（Program Files），
       用户数据绝不能写在那里；升级覆盖安装时也不能把用户数据一起冲掉；
    3. 其余情况 —— `project_root()`，与历史行为完全一致。

    注意这里只管**数据**，不管代码。代码资源（模板、prompt）仍走 `project_root()`。
    """
    configured = os.getenv("ETHOS_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if is_desktop():
        base = os.getenv("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / APP_DIR_NAME
    return project_root()


def load_environment() -> None:
    """加载本地开发配置；进程环境变量优先。

    兼容两种布局：backend-py/.env（旧），或仓库根目录 .env（统一配置）。

    文件位置由本文件路径推导，**不依赖进程 cwd**：原先按 `./.env`、`../.env`
    找，从仓库根目录启动时只能看到根 `.env`，读不到 `backend-py/.env` 里的
    COS / DB 路径等配置，行为随启动目录漂移。顺序保持「backend-py 优先」，
    与 dotenv 的 override=False 配合：先加载者生效。
    """
    backend_root = project_root()
    for path in (backend_root / ".env", backend_root.parent / ".env"):
        if path.exists():
            _load_dotenv_tolerant(path)


def _load_dotenv_tolerant(path: Path) -> None:
    """按多种编码尝试加载 .env。

    根目录 .env 可能由 Windows 记事本保存为 GBK 编码，
    而 python-dotenv 默认按 UTF-8 解码会崩溃，这里做编码回退。
    """
    for encoding in ("utf-8", "gbk"):
        try:
            load_dotenv(path, encoding=encoding)
            return
        except UnicodeDecodeError:
            continue
    # 两种编码都无法解码时，抛出以暴露问题（对齐 Go 版 log.Fatalf）
    raise RuntimeError(f"无法解码环境变量文件: {path}")


def database_path() -> str:
    """数据库文件路径，对应 Go 版 EDITH_DB_PATH。

    缺省值是 `data_root()/edith-v2.db` 的**绝对路径**，不是相对 cwd 的
    `"edith-v2.db"`。相对路径的后果：从仓库根目录启动时会当场新建一个空库，
    页面表现为「所有会话和工作区都不见了」（2026-08-31、2026-11-09 各踩一次）。

    非桌面模式（含云端部署）下 `data_root()` == `project_root()`，
    与历史路径 `backend-py/edith-v2.db` 完全一致，零行为变化。
    """
    configured = os.getenv("EDITH_DB_PATH")
    if configured:
        return configured
    return str(data_root() / "edith-v2.db")


def runtime_address() -> str:
    """HTTP 监听地址，对应 Go 版 EDITH_ADDR。"""
    return os.getenv("EDITH_ADDR", "127.0.0.1:8080")


def runtime_token() -> str:
    """本地 Runtime 的入站令牌（由桌面壳随机生成后注入）。

    **为什么需要它**：Runtime 监听的是回环 TCP 端口，同机任何进程——包括任意网页
    发起的跨站请求——都能打到它。端口是随机的，但那只是概率上的遮挡，不是边界。
    令牌把「能连上端口」和「被允许调用接口」分开：只有拿到令牌的 BFF 能进来。

    缺省为空串表示**不启用**校验，仅用于开发（`uv run edith-backend` 直连）与测试；
    桌面壳与生产部署必须注入。空值不静默降级成某个内置默认值——那等于没有令牌。
    """
    return os.getenv("EDITH_RUNTIME_TOKEN", "").strip()


def browser_rpc_url() -> str:
    """内置浏览器的 RPC 地址（由桌面壳启动时注入，形如 `http://127.0.0.1:<port>`）。

    **为什么是「后端当客户端」**：浏览器的真实资源是 Electron 主进程里的
    `WebContentsView`，只能在主进程里操作；后端拿到的只是一个受控的 HTTP 入口。
    没有注入时返回空串——非桌面模式（`uv run edith-backend`）下浏览器工具一律不可用，
    而不是退化成一个假的浏览器。
    """
    return os.getenv("ETHOS_BROWSER_RPC_URL", "").strip()


def browser_rpc_token() -> str:
    """浏览器 RPC 的令牌。

    与 `runtime_token()` **刻意不同**：这个令牌只允许操作浏览器，拿到它也调不了
    会话、文件、工具等内部接口。两个令牌复用等于把浏览器能力提权成全部内部能力。
    空串同样表示不启用，由调用方显式报「浏览器不可用」。
    """
    return os.getenv("ETHOS_BROWSER_RPC_TOKEN", "").strip()
