"""用户配置模块的类型定义。

内部类型用 dataclass，HTTP 契约用 Pydantic（camelCase 对齐 Go 版 json tag）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


@dataclass
class AgentSettings:
    """用户可编辑的 Agent 配置，不含任何密钥。"""

    personality: str = ""
    default_model_id: str = ""
    timezone: str = ""
    # git 提交身份；留空则回退平台默认（EDITH_GIT_AUTHOR_NAME/EMAIL）
    git_author_name: str = ""
    git_author_email: str = ""
    #: 执行档位：read_only / approval / full。空 = 未配置（按默认档位处理）。
    #: 这里只做存储，解析与降级在 `permissions.policy.parse_level`（非法值降为只读）。
    permission_level: str = ""


@dataclass
class ProviderStatus:
    """可安全返回浏览器的供应商配置状态。"""

    provider_id: str
    has_api_key: bool


class ProviderCredentialInput(BaseModel):
    """浏览器提交的供应商密钥；api_key 为空表示不覆盖已有密钥。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    provider_id: str
    api_key: str | None = None


class SettingsRequest(BaseModel):
    """保存用户设置的 HTTP 输入。userId 由 BFF 注入。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    personality: str = ""
    default_model_id: str = ""
    timezone: str = ""
    git_author_name: str = ""
    git_author_email: str = ""
    #: 执行档位。留空 = 不改（不是「重置为默认」）——前端还没做这个开关，
    #: 它保存其它设置时不该顺手把用户调过的档位冲掉。
    permission_level: Literal["", "read_only", "approval", "full"] = ""
    providers: list[ProviderCredentialInput] = Field(default_factory=list)


class ProviderCredentialState(BaseModel):
    """表示一个供应商是否已保存密钥。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    provider_id: str
    has_api_key: bool


class SettingsResponse(BaseModel):
    """读取或保存用户设置的 HTTP 输出，永不包含密钥。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    personality: str
    default_model_id: str
    timezone: str
    git_author_name: str = ""
    git_author_email: str = ""
    #: 当前生效档位（读接口总是给出实际值，不会是空串）
    permission_level: str = "full"
    providers: list[ProviderCredentialState]


class PermissionLevelRequest(BaseModel):
    """PUT /internal/user-settings/permission-level 的请求体。

    只改这一列。刻意不复用 `SettingsRequest`：那个接口要 personality、
    defaultModelId、providers 全套字段，聊天页为了改一个下拉去回写整份设置，
    等于给了它一个「顺手冲掉别的字段」的机会。这里少一个字段就少一条路。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    permission_level: str = ""


class PermissionLevelResponse(BaseModel):
    """写入后的用户级档位（已归一：非法值在写侧就被拒了）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    permission_level: str


class GitCredentialRequest(BaseModel):
    """浏览器提交的 Git 凭据（PAT）。userId 由 BFF 注入。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    user_id: str = ""
    provider: str = "github"
    token: str


class GitCredentialsResponse(BaseModel):
    """Git 凭据状态输出：只暴露「哪些 provider 已连接」，永不包含 token。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    connected: list[str] = Field(default_factory=list)

