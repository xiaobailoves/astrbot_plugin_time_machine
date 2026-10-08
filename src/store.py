"""插件状态持久化 —— 原子写入

只存「协商出来的 token 模式」和「最近一次发送结果」，**绝不保存验证编码**。
"""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("astrbot")


class StateStore:
    """一个很小的 JSON 状态文件，读失败不会影响插件启动"""

    def __init__(self, path: Path | str, defaults: dict | None = None) -> None:
        self.path = Path(path)
        self._defaults = dict(defaults or {})
        self.data: dict = dict(self._defaults)
        self.load()

    def load(self) -> dict:
        if not self.path.exists():
            return self.data
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data = {**self._defaults, **loaded}
            else:
                logger.warning(f"⚠️ 状态文件结构异常，已重建：{self.path}")
        except Exception as e:
            logger.warning(f"⚠️ 读取状态文件失败，已重建：{type(e).__name__}: {e}")
            self.data = dict(self._defaults)
        return self.data

    def save(self) -> None:
        """临时文件 + os.replace，避免写一半掉电把文件写坏"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(self.path.parent),
                suffix=".tmp",
                delete=False,
                newline="\n",
            ) as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                tmp_path = f.name
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.warning(f"⚠️ 写入状态文件失败：{type(e).__name__}: {e}")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
