# images 模块（聊天图片：本机磁盘存储 + Agent 图片输入）

## 第一部分：模块名称与简介

**模块名**：`images`

**一句话定位**：管理聊天图片的元数据（SQLite）与**本机磁盘**上的字节，对外提供
「预约 → 写入 → 确认 → 读取」四步生命周期，并把已确认的图片交给模型。

**为什么不是对象存储**：上游用腾讯云 COS（预签名直传 + 短期签名 URL）。桌面端
没有 bucket、没有密钥，而且远端模型拉不到 `127.0.0.1` 上的图片地址——那是模型
机器自己的回环地址。所以字节落在 `data_root()/images/`，喂给模型时内联为
data URL。

**文件清单**（`src/edith_backend/internal/images/` 下）：

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 模块公开导出 |
| `agent_input.py` | Agent 输入适配：图片 ID → data URL（带内联体积上限）；持久引用标记 `edith-image://` 编解码 |
| `http.py` | HTTP 边界：创建预约、PUT 字节、确认、读取四条路由与错误映射 |
| `module.py` | 模块工厂：`new_module(db, images_root)` |
| `service.py` | 服务层：预约校验、写入（含类型/大小把关）、确认比对、读取、失败清理 |
| `storage.py` | 本机存储：object_key → 路径、魔数嗅探、`.part` + `os.replace` 原子写入、删除 |
| `store.py` | SQLite 访问：`chat_images` 表 CRUD，`ImageRecord` / `ImageNotFound` |
| `types.py` | 领域 dataclass 与 Pydantic 请求/响应模型（camelCase 别名） |

## 第二部分：架构设计

### ① 分层

| 层次 | 文件 | 职责 |
| --- | --- | --- |
| HTTP 边界 | `http.py` | 解析请求、把异常映射为 400 / 404 / 413 |
| 服务 | `service.py` | 生命周期与业务规则；不碰 HTTP，也不直接碰文件字节格式 |
| 存储 | `storage.py` | 只负责「字节 ↔ 文件」，含原子写与魔数校验 |
| 元数据 | `store.py` | 只负责 `chat_images` 表 |

### ② 四步生命周期与各自守的失败面

| 步骤 | 入口 | 校验什么 | 失败码 |
| --- | --- | --- | --- |
| 预约 | `POST /internal/images` | user/session 非空、mime 在白名单、声明大小在 (0, 10 MiB] | 400 |
| 写入 | `PUT /internal/images/{id}/blob?userId=` | 预约存在且属于该用户；Content-Type 与预约一致；字节数不超上限；魔数与预约一致 | 404 / 400 / 413 |
| 确认 | `POST /internal/images/{id}/complete` | 文件在盘上、大小等于声明、魔数等于预约 | 400 / 404 |
| 读取 | `GET /internal/images/{id}?userId=` | 记录 ready 且属于该用户 | 404 |

**为什么不合并成一步**：三段各守不同的失败面，合并后「字节写了一半」这个中间
态就无处安放，只能靠「记录已 confirmed」假装成功。

**为什么按魔数判类型**：`Content-Type` 由客户端说了算。把 HTML 伪装成
`image/png` 存下来、再原样从同源地址吐回去，就是一个存储型 XSS。

**为什么先写 `.part` 再 `os.replace`**：同文件系统内 `os.replace` 是原子的，
读取方永远看不到半截文件；超限时立刻中止，而不是读完整个流再看长度——否则一个
声明 1 KB 的请求可以先把磁盘写满。

### ③ 关键约定

- **object_key 只取末段**：历史记录里的 `chat-images/<id>` 与现在的 `images/<id>`
  都解析到 `images/<id>`，所以换布局不需要数据迁移；同时 `..` 逃逸也没有空间。
- **确认可重复**：已 ready 的图片再次 complete 直接返回，不报错（前端重试友好）。
- **失败要连记录一起丢**：字节不合法或大小不符时，文件和记录一起删。留着记录会
  让这张 id 永远停在 pending、永远 complete 不了。
- **id 一旦 ready 不再变化**：GET 可以长期缓存（`private, max-age=31536000,
  immutable`），会话里滚动不必重下。
- **内联上限 7 MiB**：base64 放大 1.33 倍，10 MB 原图会变成 13 MB 请求体，超过
  多数视觉模型的单图限制。超限的图片明确跳过并记 warning，不发一个必然 400 的
  请求——那种失败在用户侧表现为「模型没反应」，无法归因。

## 第三部分：与其他模块的联系

| 依赖方 | 用途 |
| --- | --- |
| `agentrun`（`configuration.py`） | 通过 `images.agent_input.message_image_urls()` 取本次调用的图片 |
| `agentrun.execution.py` | 把返回的 data URL 包进 `{"type":"image_url","image_url":{"url":...}}` |
| 组合根 `main.py` | `new_images_module(db, config.data_root()/"images")` + `include_router` |
| 前端 BFF | `POST /api/images`、`PUT /api/images/{id}/blob`、`POST /api/images/{id}/complete`、`GET /api/images/{id}` 四个同源代理 |

画像与库同处 `data_root()`：备份或迁移时「库」与「图」是一个整体，不会一半走丢。

## 第四部分：已知缺口

- **没有孤儿回收**：删除会话不会删图片文件。桌面端单用户、10 MB 上限，短期可接受；
  若要做，应放在「会话删除」的收尾钩子里按 `session_id` 批量清。
- **没有图片压缩/转码**：原图直接内联。对截图类场景够用，对手机原图会更容易触发
  7 MiB 上限。
