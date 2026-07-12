"""
Plugin registry — 显式注册装饰器（取代 shared.registry.Registerable 元类自注册）。

为什么改成显式装饰器（P0-3）
----------------------------
原 Registerable 是一个混入 domain ports 的技术 mixin：注册表存储在端口基类
的 ``_target_registry`` ClassVar 上，靠 ``__init_subclass__`` 在 import 时
隐式注册。这让领域抽象背上了插件机制（基础设施关注点），也让"import 领域
端口的任何子类"产生全局副作用。

现在：注册表集中存放在本模块（infrastructure），插件类用显式装饰器登记::

    from omnibci.domain.ports.acquisition import SignalSource
    from omnibci.infrastructure.plugins.registry import register_plugin

    @register_plugin(SignalSource)
    class FileReader(SignalSource):
        plugin_type = "file_reader"
        ...

domain ports 回归纯 ABC；注册时机不变（PluginLoader import 插件模块时装饰器
执行），冲突语义与原实现保持一致：
  - 同一类对象重复注册 → 幂等跳过；
  - 同名类重复注册（测试环境 re-import）→ 允许覆盖；
  - 不同类抢同一 plugin_type → ValueError。
"""
import logging
from typing import Callable, Dict, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: base port class → {plugin_type: implementation class}
_REGISTRIES: Dict[type, Dict[str, type]] = {}


def register_plugin(base: type) -> Callable[[Type[T]], Type[T]]:
    """Class decorator: register the class in *base*'s plugin registry.

    The registry key is the class's ``plugin_type`` attribute (must be a
    non-empty string). The class must be a subclass of *base*.
    """
    def decorator(cls: Type[T]) -> Type[T]:
        plugin_type = getattr(cls, "plugin_type", None)
        if not isinstance(plugin_type, str) or not plugin_type:
            raise ValueError(
                f"@register_plugin: {cls.__name__} must define a non-empty "
                f"'plugin_type' class attribute."
            )
        if not issubclass(cls, base):
            raise TypeError(
                f"@register_plugin({base.__name__}): {cls.__name__} is not "
                f"a subclass of {base.__name__}."
            )

        registry = _REGISTRIES.setdefault(base, {})
        existing = registry.get(plugin_type)
        if existing is not None:
            if existing is cls:
                return cls  # 同一对象重复注册：幂等
            if existing.__name__ != cls.__name__:
                raise ValueError(
                    f"Duplicate plugin_type '{plugin_type}' for "
                    f"{base.__name__}: {existing.__name__} vs {cls.__name__}"
                )
            # 同名类（通常是测试重新加载）：允许覆盖

        registry[plugin_type] = cls
        logger.info("[Registry] %s -> %s (%s registry size: %d)",
                    plugin_type, cls.__name__, base.__name__, len(registry))
        return cls

    return decorator


def registry_for(base: type) -> Dict[str, type]:
    """Return the plugin registry for *base* (empty dict if none registered).

    调用方只应读取；需要变更注册请走 :func:`register_plugin`。
    """
    return _REGISTRIES.get(base, {})
