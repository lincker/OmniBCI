import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Dict, Iterator, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: base port class → {plugin_type: implementation class}
_REGISTRIES: Dict[type, Dict[str, type]] = {}
_REGISTRY_CONTEXT: ContextVar[Dict[type, Dict[str, type]] | None] = ContextVar(
    "omnibci_plugin_registries",
    default=None,
)


def _active_registries() -> Dict[type, Dict[str, type]]:
    isolated = _REGISTRY_CONTEXT.get()
    return isolated if isolated is not None else _REGISTRIES


@contextmanager
def isolated_registry() -> Iterator[None]:
    """Route import-time registrations to a disposable local registry.

    Descriptor entrypoints return their class directly and do not consume the
    legacy registry.  Isolating decorator side effects lets old-style plugins
    remain loadable without contaminating or colliding with process-global
    registrations.
    """
    token = _REGISTRY_CONTEXT.set({})
    try:
        yield
    finally:
        _REGISTRY_CONTEXT.reset(token)


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

        registry = _active_registries().setdefault(base, {})
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
    return _active_registries().get(base, {})


class ObjectFactory:
    """Legacy registry-backed constructor for subsystems not yet migrated.

    New runtimes should resolve a descriptor and use ``PluginLoader``.  This
    compatibility adapter remains here while acquisition, processing and
    stimulation still use import-time registration.
    """

    @staticmethod
    def create(
        base_cls: Type[T],
        target_id: Optional[str],
        **kwargs,
    ) -> Optional[T]:
        """Instantiate a registered plugin, returning ``None`` on failure."""
        if not target_id:
            logger.error("[Factory] No plugin_type given for %s.", base_cls.__name__)
            return None

        registry = registry_for(base_cls)
        if target_id not in registry:
            logger.error(
                "[Factory] Unknown %s type: '%s'. Available: %s",
                base_cls.__name__,
                target_id,
                list(registry.keys()),
            )
            return None

        try:
            instance = registry[target_id](**kwargs)
            logger.info(
                "[Factory] Created %s instance: '%s'",
                base_cls.__name__,
                target_id,
            )
            return instance
        except Exception as exc:
            logger.error(
                "[Factory] Failed to instantiate '%s': %s",
                target_id,
                exc,
                exc_info=True,
            )
            return None
