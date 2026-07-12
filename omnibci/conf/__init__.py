#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-28 10:39
"""
Settings with layered profiles.

    effective = active profile overlay ⊕ default (settings.toml)

Profiles are named key-subsets stored as TOML files under ``profiles/`` next
to settings.toml.  The active selection persists in ``[PROFILES] active`` of
settings.toml itself.  Resolution happens *inside* this module, so spawned
worker processes — which re-import ``omnibci.conf`` and reload from disk —
see exactly the same effective configuration as the API process.
"""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import tomlkit

logger = logging.getLogger(__name__)

# Reserved section in settings.toml holding the active-profile pointer.
# Never part of any profile file and never exposed through the config API.
_ACTIVE_KEY = "PROFILES.active"
# Reserved metadata section inside profile files.
_META_SECTION = "META"

# Filenames are the user-facing profile names (Chinese OK).  Reject anything
# that could escape the profiles dir or surprise the filesystem.
_NAME_RE = re.compile(r"^[\w\- ]{1,64}$")  # \w is unicode: CJK, latin, digits, _


class ProfileNameError(ValueError):
    """Profile name fails filesystem-safety validation."""


class ProfileNotFoundError(KeyError):
    """No profile file with that name."""


class OmniBCISettings:
    def __init__(self, toml_file: str | Path = 'settings.toml'):
        self._config_path = Path(toml_file)
        self._profiles_dir = self._config_path.parent / "profiles"
        self._data = tomlkit.document()
        self._overlay = tomlkit.document()   # active profile content (or empty)
        self.load_config()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_config(self):
        """动态加载配置(默认 + 激活的 profile 叠加层)"""
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件 {self._config_path} 不存在")
        with open(self._config_path, "r", encoding="utf-8") as f:
            self._data = tomlkit.load(f)
        self._load_overlay()

    def _load_overlay(self) -> None:
        """(Re)load the active profile; degrade to default-only on any problem."""
        self._overlay = tomlkit.document()
        name = self._raw_get(self._data, _ACTIVE_KEY)
        if not name:
            return
        path = self._profile_path(name, validate=False)
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._overlay = tomlkit.load(f)
        except FileNotFoundError:
            logger.warning("Active profile %r missing (%s) — using defaults only", name, path)
        except Exception as exc:  # malformed toml etc.
            logger.warning("Active profile %r unreadable (%s) — using defaults only", name, exc)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def _raw_get(doc, key: str):
        value = doc
        try:
            for k in key.split('.'):
                value = value[k]
        except (KeyError, TypeError):
            return None
        return value.unwrap() if hasattr(value, 'unwrap') else value

    #: get() 的"未提供默认值"哨兵——用于区分 get(key)（缺键抛错）与
    #: get(key, None)（缺键返回 None）。
    _MISSING = object()

    def get(self, key: str, default=_MISSING):
        """获取**生效**配置值(profile 覆盖优先),支持点分隔符 'section.key'。

        P2-9 修复：default 参数原先形同虚设（缺键无条件抛 NameError）。
        现在语义为——
          get(key)          缺键抛 NameError（原行为，供必填配置 fail-fast）
          get(key, 默认值)  缺键返回默认值，不记错误日志（可选配置）
        """
        overlaid = self._raw_get(self._overlay, key)
        if overlaid is not None:
            return overlaid
        value = self._raw_get(self._data, key)
        if value is not None:
            return value
        if default is not self._MISSING:
            return default
        logging.error(f'settings can not find key: {key}')
        raise NameError(f'settings key not found: {key}')

    def get_default(self, key: str):
        """获取默认配置值(忽略 profile 覆盖);键缺失返回 None"""
        return self._raw_get(self._data, key)

    def get_override(self, key: str):
        """获取激活 profile 的覆盖值;无覆盖返回 None"""
        return self._raw_get(self._overlay, key)

    # ------------------------------------------------------------------
    # Writes (default config)
    # ------------------------------------------------------------------

    def set(self, key: str, value):
        """设置**默认**配置值，支持点分隔符"""
        keys = key.split('.')
        data_ref = self._data
        for k in keys[:-1]:
            if k not in data_ref:
                data_ref[k] = tomlkit.table()
            data_ref = data_ref[k]
        data_ref[keys[-1]] = value

    def save(self):
        """保存默认配置到文件，保留所有格式和注释"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            tomlkit.dump(self._data, f)

    # ------------------------------------------------------------------
    # Profiles — storage primitives (policy/whitelist lives in ConfigService)
    # ------------------------------------------------------------------

    def _profile_path(self, name: str, validate: bool = True) -> Path:
        if validate and not _NAME_RE.match(name or ""):
            raise ProfileNameError(
                f"非法配置名 {name!r}:仅限中英文、数字、下划线、连字符、空格,≤64 字符")
        return self._profiles_dir / f"{name}.toml"

    def list_profiles(self) -> List[dict]:
        """[{name, description, key_count, updated_at}],按名称排序"""
        if not self._profiles_dir.is_dir():
            return []
        out = []
        for path in sorted(self._profiles_dir.glob("*.toml")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = tomlkit.load(f)
            except Exception:
                continue
            keys = self._flatten(doc)
            out.append({
                "name": path.stem,
                "description": self._raw_get(doc, f"{_META_SECTION}.description"),
                "key_count": len(keys),
                "updated_at": self._raw_get(doc, f"{_META_SECTION}.updated_at"),
            })
        return out

    def read_profile(self, name: str) -> dict:
        """profile 的覆盖键平铺 {'SECTION.key': value};不存在抛 ProfileNotFoundError"""
        path = self._profile_path(name)
        if not path.exists():
            raise ProfileNotFoundError(name)
        with open(path, "r", encoding="utf-8") as f:
            doc = tomlkit.load(f)
        return self._flatten(doc)

    def write_profile(self, name: str, updates: dict, description: Optional[str] = None,
                      replace: bool = False) -> None:
        """创建/更新 profile(merge 语义;replace=True 整体替换)并刷新叠加层"""
        path = self._profile_path(name)
        doc = tomlkit.document()
        if path.exists() and not replace:
            with open(path, "r", encoding="utf-8") as f:
                doc = tomlkit.load(f)
        for key, value in updates.items():
            keys = key.split('.')
            ref = doc
            for k in keys[:-1]:
                if k not in ref:
                    ref[k] = tomlkit.table()
                ref = ref[k]
            ref[keys[-1]] = value
        if _META_SECTION not in doc:
            doc[_META_SECTION] = tomlkit.table()
        if description is not None:
            doc[_META_SECTION]["description"] = description
        doc[_META_SECTION]["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            tomlkit.dump(doc, f)
        if self.active_profile == name:
            self._load_overlay()

    def delete_profile(self, name: str) -> None:
        """删除 profile 文件;不存在抛 ProfileNotFoundError(激活检查由服务层做)"""
        path = self._profile_path(name)
        if not path.exists():
            raise ProfileNotFoundError(name)
        path.unlink()

    # ------------------------------------------------------------------
    # Profiles — activation
    # ------------------------------------------------------------------

    @property
    def active_profile(self) -> Optional[str]:
        name = self._raw_get(self._data, _ACTIVE_KEY)
        return name or None  # "" (deactivated) normalises to None

    def set_active_profile(self, name: Optional[str]) -> None:
        """激活(name=None 表示回到仅默认);持久化指针并刷新叠加层"""
        if name is not None:
            if not self._profile_path(name).exists():
                raise ProfileNotFoundError(name)
        self.set(_ACTIVE_KEY, name if name is not None else "")
        self.save()
        self._load_overlay()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @classmethod
    def _flatten(cls, doc, prefix: str = "") -> dict:
        """嵌套 toml → {'SECTION.key': value} 平铺;跳过 META 段"""
        flat = {}
        for k, v in doc.items():
            if not prefix and k == _META_SECTION:
                continue
            dotted = f"{prefix}{k}"
            if hasattr(v, "items") and not isinstance(v, (str, list)):
                flat.update(cls._flatten(v, prefix=f"{dotted}."))
            else:
                flat[dotted] = v.unwrap() if hasattr(v, "unwrap") else v
        return flat


settings = OmniBCISettings(
    toml_file=Path(__file__).parent.joinpath('settings.toml')
)
