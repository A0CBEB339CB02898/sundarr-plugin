"""Sundarr Core 从仓库根目录加载的官方插件聚合入口。"""

from src.sundarr_official_plugins.douban_catalog import activate as activate_douban
from src.sundarr_official_plugins.tmdb_catalog import activate as activate_tmdb


activate_tmdb_catalog = activate_tmdb
activate_douban_catalog = activate_douban


__all__ = ["activate_douban_catalog", "activate_tmdb_catalog"]
