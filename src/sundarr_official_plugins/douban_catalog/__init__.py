"""豆瓣 CATALOG_PROVIDER。"""

from .provider import DoubanCatalogProvider, DoubanProviderError, activate


__all__ = ["DoubanCatalogProvider", "DoubanProviderError", "activate"]
