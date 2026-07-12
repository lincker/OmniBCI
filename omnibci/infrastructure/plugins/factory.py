"""
ObjectFactory — 按 plugin_type 实例化插件（原 shared/factory.py，P0-3 迁入）。

与原实现的两点差异：

1. 位置：shared 必须保持零内部依赖，而工厂属于插件基础设施，故迁至此处。
2. 签名：``create(base_cls, target_id, **kwargs)`` 直接接收 plugin_type，
   不再自己读全局 settings（原 ``config_key`` 参数）。配置读取上移到调用方
   （各 worker / stimulator），工厂只做"注册表查找 + 实例化"一件事。

错误语义保持原样：任何失败记日志并返回 None（调用方按 None 处理）。
"""
import logging
from typing import Optional, Type, TypeVar

from omnibci.infrastructure.plugins.registry import registry_for

T = TypeVar("T")

logger = logging.getLogger(__name__)


class ObjectFactory:

    @staticmethod
    def create(base_cls: Type[T], target_id: Optional[str], **kwargs) -> Optional[T]:
        """Instantiate the *base_cls* plugin registered under *target_id*.

        Returns None (with an error log) when *target_id* is falsy, unknown,
        or the plugin's constructor raises — 与迁移前行为一致。
        """
        if not target_id:
            logger.error("[Factory] No plugin_type given for %s.", base_cls.__name__)
            return None

        registry = registry_for(base_cls)
        if target_id not in registry:
            logger.error(
                "[Factory] Unknown %s type: '%s'. Available: %s",
                base_cls.__name__, target_id, list(registry.keys()),
            )
            return None

        target_cls = registry[target_id]
        try:
            instance = target_cls(**kwargs)
            logger.info("[Factory] Created %s instance: '%s'", base_cls.__name__, target_id)
            return instance
        except Exception as e:
            logger.error("[Factory] Failed to instantiate '%s': %s", target_id, e, exc_info=True)
            return None
