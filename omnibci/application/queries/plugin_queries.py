"""
Plugin discovery read model — application-layer DTO.

``PluginInfo`` is the plugin catalog's discovery result. Infrastructure creates
it from manifests, and presentation maps it to its independent wire schema.
Keeping this query contract in application avoids a dependency from the catalog
on presentation. Host settings no longer use discovery to validate plugin edits;
plugin selection and parameter validation belong to the Plugin Profile services.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class PluginInfo:
    """One discovered plugin (any category), as read from its manifest.toml.

    Attributes
    ----------
    id           机器标识，插件目录内唯一（= manifest package.id，缺省为目录名）。
    plugin_type  注册表键，引擎实际加载插件时使用。
    display_name UI 显示名。
    description  可选长描述。
    version      manifest 中的 semver 字符串（如有）。
    source       "builtin"（随应用发布）或 "user"（用户目录安装）。
    params       manifest [params] — 只读参数，UI 仅展示。
    controls     manifest [controls.<name>] — 可编辑控件描述符
                 {label, type, default, min?, max?, field}。
    """
    id: str
    plugin_type: str
    display_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    source: str = "user"
    params: Dict[str, Any] = field(default_factory=dict)
    controls: Dict[str, Dict[str, Any]] = field(default_factory=dict)
