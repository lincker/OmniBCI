#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-27 11:50

import logging

from typing import Type, TypeVar, Optional, Dict

from omnibci.conf import settings

T = TypeVar("T")

logger = logging.getLogger(__name__)


class ObjectFactory:

    @staticmethod
    def create(base_cls: Type[T], config_key: str = None, **kwargs) -> Optional[T]:


        target_id = settings.get(config_key)

        if not target_id:
            logger.error(f"[Factory] No config key found in settings. Key: '{config_key}'")
            return None

        if not hasattr(base_cls, "_target_registry"):
            logger.critical(
                f"[Factory] Class {base_cls.__name__} has no '_target_registry'. Is it inheriting Registerable?")
            return None

        registry: Dict[str, Type[T]] = getattr(base_cls, "_target_registry")

        if target_id not in registry:
            available = list(registry.keys())
            logger.error(f"[Factory] Unknown {base_cls.__name__} type: '{target_id}'. Available: {available}")
            return None

        target_cls = registry[target_id]

        try:
            instance = target_cls(**kwargs)
            logger.info(f"[Factory] Created {base_cls.__name__} instance: '{target_id}'")
            return instance
        except Exception as e:
            logger.error(f"[Factory] Failed to instantiate '{target_id}': {e}", exc_info=True)
            return None