"""Provider 实现。加新后端从 :class:`~sleight.providers.base.HTTPProvider` 继承。"""

from .base import BaseProvider, HTTPProvider, Provider
from .cloakbrowser import CLEAR, UNSET, CloakBrowserManager, ProfileSpec
from .plain import Plain

__all__ = [
    "CLEAR",
    "UNSET",
    "BaseProvider",
    "CloakBrowserManager",
    "HTTPProvider",
    "Plain",
    "ProfileSpec",
    "Provider",
]
