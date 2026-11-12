"""沙箱的原始类型：文件项的类型枚举。

**为什么需要单独一份**：工具层、HTTP 文件面板、上传校验三处都要判断「这一项是
目录还是文件」。这个判断与「文件放在哪儿」无关——不管下面是本地磁盘还是别的
实现，`type` 的取值都是同一套。

**为什么不继承 `str`**：调用点全部写成 `info.type == FileType.DIR`，
如果枚举同时等于 `"dir"`，就会有人图省事写成字符串比较，而字符串比较漏掉
"实现返回了别的拼写"这种情况时不会报错，只会静默走错分支。枚举只跟自己相等，
写错了立刻是 `False` 而不是"看起来对"。
"""

from __future__ import annotations

from enum import Enum


class FileType(Enum):
    """文件项类型。取值与工具结果的 JSON 序列化保持一致（`file` / `dir`）。"""

    FILE = "file"
    DIR = "dir"
