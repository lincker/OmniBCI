"""
Plugin discovery read model — application-layer DTO.

``PluginInfo`` 是插件发现（infrastructure/plugins/plugin_catalog）的返回类型。
它放在 application 层，因为它是跨层共享的查询结果契约：

  - infrastructure 生产它（catalog 扫描 manifest.toml 后填充）；
  - application 消费它（config_service 对 plugin_type 做软校验）；
  - presentation 将它映射为 wire schema（schemas.stimulation.ParadigmInfo）。

此前 catalog 直接返回 presentation 的 pydantic wire 契约，构成
infrastructure → presentation 的反向依赖（见重构报告 P0-2）。
本 DTO 与 wire schema 字段一一对应，但相互独立演化：改 HTTP 契约
不再波及插件发现，反之亦然。
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
