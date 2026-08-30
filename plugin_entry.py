"""Sundarr Core 从仓库根目录加载的官方插件聚合入口。"""

from src.sundarr_official_plugins.tmdb_catalog import activate


activate_tmdb_catalog = activate


__all__ = ["activate_tmdb_catalog"]
