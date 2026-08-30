"""TMDb 显式实时集成测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from sundarr.app.plugins.activator import PluginActivator
from sundarr.app.plugins.conformance import (
    CatalogConformanceProbe,
    run_catalog_provider_conformance,
)
from sundarr.app.plugins.contracts import CatalogQuery, MediaType
from sundarr.app.plugins.loader import PluginLoader
from sundarr.app.plugins.runtime_registry import catalog_provider_registry


pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[1]


def test_tmdb_real_data_conformance() -> None:
    token = os.getenv("TMDB_API_READ_ACCESS_TOKEN", "").strip()
    if not token:
        pytest.skip("需要设置 TMDB_API_READ_ACCESS_TOKEN 才能执行真实数据验收")

    async def run() -> None:
        catalog_provider_registry.clear()
        loader = PluginLoader(repos_dir=ROOT / ".live-cache")
        manifest = loader.parse_manifests(ROOT)[0]
        activation = await PluginActivator(loader=loader).activate_candidate(
            manifest,
            ROOT,
            plugin_config={
                "api_read_access_token": token,
                "language": "zh-CN",
                "include_adult": False,
                "image_size": "w500",
            },
            repository_id="sundarr-plugin-live",
            commit_hash="working-tree",
        )
        try:
            report = await run_catalog_provider_conformance(
                activation.instance,
                CatalogConformanceProbe(
                    query=CatalogQuery(
                        keyword="盗梦空间",
                        media_type=MediaType.MOVIE,
                        limit=5,
                    ),
                    detail_external_id="27205",
                    detail_media_type=MediaType.MOVIE,
                ),
            )
            assert set(report.checks) == {"search", "trending", "categories", "detail"}
            assert all(count > 0 for count in report.checks.values())
            page = await activation.instance.search(
                CatalogQuery(keyword="黑客帝国", limit=5)
            )
            assert page.items
            assert any(item.poster_url for item in page.items)
            assert all(
                item.external_id_provider in {"tmdb.movie", "tmdb.tv"}
                for item in page.items
            )
        finally:
            await activation.dispose()
            catalog_provider_registry.clear()

    asyncio.run(run())
