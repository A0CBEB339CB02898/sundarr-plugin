"""豆瓣公开目录的显式实时集成测试。"""

from __future__ import annotations

import asyncio

import pytest

from sundarr.app.plugins.conformance import (
    CatalogConformanceProbe,
    run_catalog_provider_conformance,
)
from sundarr.app.plugins.contracts import CatalogQuery, CatalogSort, MediaType
from sundarr.app.plugins.http import PluginHttpClient
from sundarr_official_plugins.douban_catalog import DoubanCatalogProvider


pytestmark = pytest.mark.live


def test_douban_real_search_returns_love_letter() -> None:
    async def run() -> None:
        provider = DoubanCatalogProvider(PluginHttpClient(plugin_id="douban-catalog-live-search"))
        await provider.initialize()
        search = await provider.search(
            CatalogQuery(keyword="情书", media_type=MediaType.MOVIE, limit=10)
        )
        target = next(item for item in search.items if item.external_id == "1292220")
        assert target.title == "情书"
        assert target.year == 1995
        assert target.poster_url and target.poster_url.startswith("https://")

    asyncio.run(run())


def test_douban_real_search_trending_categories_and_details() -> None:
    async def run() -> None:
        client = PluginHttpClient(plugin_id="douban-catalog-live")
        provider = DoubanCatalogProvider(client)
        try:
            await provider.initialize()
            report = await run_catalog_provider_conformance(
                provider,
                CatalogConformanceProbe(
                    query=CatalogQuery(
                        keyword="星际穿越",
                        media_type=MediaType.MOVIE,
                        limit=2,
                    ),
                    detail_external_id="1889243",
                    detail_media_type=MediaType.MOVIE,
                ),
            )
            assert all(count > 0 for count in report.checks.values())
            search = await provider.search(
                CatalogQuery(keyword="星际穿越", media_type=MediaType.MOVIE, limit=2)
            )
            target = next(item for item in search.items if item.external_id == "1889243")
            assert target.title == "星际穿越"
            assert target.poster_url and target.poster_url.startswith("https://")

            movie = await provider.get_detail("1889243", MediaType.MOVIE)
            assert movie.rating is not None
            assert movie.overview
            assert movie.genres
            series_page = await provider.trending(
                CatalogQuery(media_type=MediaType.SERIES, limit=3)
            )
            assert series_page.items
            series = await provider.get_detail(
                series_page.items[0].external_id,
                MediaType.SERIES,
            )
            assert series.media_type is MediaType.SERIES
            assert series.poster_url and series.poster_url.startswith("https://")

            categories = await provider.categories(
                CatalogQuery(
                    media_type=MediaType.MOVIE,
                    genres=("科幻",),
                    sort=CatalogSort.RATING,
                    limit=3,
                )
            )
            assert categories.items
            assert all(item.external_ids.get("douban.subject") for item in categories.items)
            assert provider.describe_capabilities().attribution is not None
        finally:
            # Core 的受控 HTTP 客户端当前不持有需要显式关闭的连接池。
            pass

    asyncio.run(run())
