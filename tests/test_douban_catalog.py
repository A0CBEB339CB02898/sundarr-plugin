"""豆瓣目录插件的离线合同、映射和 Core API 测试。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sundarr.app import models  # noqa: F401
from sundarr.app.core.database import Base, get_db
from sundarr.app.main import create_app
from sundarr.app.plugins.activator import PluginActivator
from sundarr.app.plugins.conformance import (
    CatalogConformanceProbe,
    run_catalog_provider_conformance,
)
from sundarr.app.plugins.contracts import (
    CatalogFilter,
    CatalogOperation,
    CatalogQuery,
    CatalogSort,
    MediaType,
)
from sundarr.app.plugins.loader import PluginLoader
from sundarr.app.plugins.runtime_registry import catalog_provider_registry
from sundarr.app.services.catalog_cache import catalog_cache
from sundarr_official_plugins.douban_catalog import (
    DoubanCatalogProvider,
    DoubanProviderError,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "douban" / "responses.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FixtureHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []

    async def get_json(self, url: str, *, headers: dict[str, str] | None = None):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        request_headers = dict(headers or {})
        self.calls.append((parsed.path, query, request_headers))
        assert request_headers["User-Agent"].startswith("Mozilla/5.0")
        assert request_headers["Referer"].startswith("https://")
        payload = _fixture()
        if parsed.path == "/j/subject_suggest":
            return deepcopy(payload["suggestions"])
        if parsed.path == "/j/search_subjects":
            rows = payload[
                "movie_subjects" if query.get("type") == ["movie"] else "series_subjects"
            ]
            start = int(query.get("page_start", ["0"])[0])
            limit = int(query.get("page_limit", ["20"])[0])
            return {"subjects": deepcopy(rows[start : start + limit])}
        if parsed.path == "/j/new_search_subjects":
            tags = query.get("tags", [""])[0].split(",")
            rows = payload["movie_subjects" if tags[0] == "电影" else "series_subjects"]
            start = int(query.get("start", ["0"])[0])
            limit = int(query.get("limit", ["20"])[0])
            return {"data": deepcopy(rows[start : start + limit])}
        if parsed.path == "/rexxar/api/v2/movie/1889243":
            return deepcopy(payload["movie_detail"])
        if parsed.path == "/rexxar/api/v2/tv/35588177":
            return deepcopy(payload["series_detail"])
        raise AssertionError(f"未声明离线豆瓣路由：{parsed.path}")


class FixtureHttpFactory:
    def __init__(self, client: FixtureHttpClient) -> None:
        self.client = client

    def create(self, plugin_id: str) -> FixtureHttpClient:
        assert plugin_id == "douban-catalog"
        return self.client


async def _provider(client: FixtureHttpClient | None = None) -> DoubanCatalogProvider:
    provider = DoubanCatalogProvider(client or FixtureHttpClient())
    await provider.initialize()
    return provider


@pytest.fixture(autouse=True)
def _clear_catalog_registry():
    catalog_provider_registry.clear()
    yield
    catalog_provider_registry.clear()


def _douban_manifest():
    loader = PluginLoader(repos_dir=ROOT / ".test-cache")
    return loader, next(
        manifest for manifest in loader.parse_manifests(ROOT) if manifest.id == "douban-catalog"
    )


def test_manifest_activates_douban_independently() -> None:
    async def run() -> None:
        client = FixtureHttpClient()
        loader, manifest = _douban_manifest()
        assert manifest.entry == "plugin_entry:activate_douban_catalog"
        assert manifest.config_schema == {}
        activation = await PluginActivator(
            loader=loader,
            extra_capabilities={"core.http.v1": FixtureHttpFactory(client)},
        ).activate_candidate(
            manifest,
            ROOT,
            plugin_config={},
            repository_id="official",
            commit_hash="offline",
        )
        assert catalog_provider_registry.require("douban-catalog") is activation.instance
        assert set(activation.provided_capabilities) == set(manifest.provides)
        assert activation.instance.health_check().ok is True
        await activation.dispose()
        assert len(catalog_provider_registry) == 0

    asyncio.run(run())


def test_capabilities_are_operation_specific_and_attributed() -> None:
    capabilities = asyncio.run(_provider()).describe_capabilities()
    assert capabilities.identity_namespaces == frozenset({"douban.subject"})
    assert capabilities.filters_for(CatalogOperation.SEARCH) == frozenset(
        {CatalogFilter.MEDIA_TYPE, CatalogFilter.YEAR}
    )
    assert capabilities.filters_for(CatalogOperation.TRENDING) == frozenset(
        {CatalogFilter.MEDIA_TYPE}
    )
    assert capabilities.sorts_for(CatalogOperation.CATEGORIES) == frozenset(CatalogSort)
    assert capabilities.attribution is not None
    assert capabilities.attribution.provider_name == "豆瓣"
    assert capabilities.attribution.logo_url is None
    genres = {item.value for item in capabilities.filter_options[CatalogFilter.GENRE]}
    assert {"剧情", "科幻", "犯罪"}.issubset(genres)


def test_search_maps_types_filters_year_and_resumes() -> None:
    async def run() -> None:
        provider = await _provider()
        first = await provider.search(CatalogQuery(keyword="季节", limit=1))
        assert first.items[0].external_ids == {"douban.subject": "1889243"}
        assert first.items[0].original_title == "Interstellar"
        assert first.continuation_token
        second = await provider.search(
            CatalogQuery(
                keyword="季节",
                limit=1,
                continuation_token=first.continuation_token,
            )
        )
        assert second.items[0].media_type is MediaType.SERIES
        assert second.items[0].year == 2023
        filtered = await provider.search(
            CatalogQuery(
                keyword="季节",
                media_type=MediaType.SERIES,
                year_from=2023,
                year_to=2023,
            )
        )
        assert [item.external_id for item in filtered.items] == ["35588177"]
        with pytest.raises(ValueError, match="不匹配"):
            await provider.search(
                CatalogQuery(
                    keyword="另一个查询",
                    continuation_token=first.continuation_token,
                )
            )

    asyncio.run(run())


def test_unsupported_filters_are_not_silently_ignored() -> None:
    async def run() -> None:
        provider = await _provider()
        with pytest.raises(ValueError, match="search 不支持筛选：genre"):
            await provider.search(CatalogQuery(keyword="科幻", genres=("科幻",)))
        with pytest.raises(ValueError, match="trending 不支持筛选：year"):
            await provider.trending(CatalogQuery(year_from=2020))
        with pytest.raises(ValueError, match="不支持题材"):
            await provider.categories(CatalogQuery(genres=("不存在",)))

    asyncio.run(run())


def test_trending_and_categories_map_pagination_tags_and_sort() -> None:
    async def run() -> None:
        client = FixtureHttpClient()
        provider = await _provider(client)
        trending = await provider.trending(
            CatalogQuery(media_type=MediaType.MOVIE, limit=2)
        )
        assert len(trending.items) == 2
        assert trending.continuation_token
        next_page = await provider.trending(
            CatalogQuery(
                media_type=MediaType.MOVIE,
                limit=2,
                continuation_token=trending.continuation_token,
            )
        )
        assert [item.external_id for item in next_page.items] == ["1292720"]

        categories = await provider.categories(
            CatalogQuery(
                media_type=MediaType.SERIES,
                genres=("犯罪",),
                sort=CatalogSort.RATING,
                limit=2,
            )
        )
        assert categories.items[0].media_type is MediaType.SERIES
        category_call = next(call for call in client.calls if call[0] == "/j/new_search_subjects")
        assert category_call[1]["tags"] == ["电视剧,犯罪"]
        assert category_call[1]["sort"] == ["R"]

    asyncio.run(run())


def test_mixed_pages_alternate_when_limit_is_one() -> None:
    async def run() -> None:
        provider = await _provider()
        first = await provider.categories(CatalogQuery(limit=1))
        assert first.items[0].media_type is MediaType.MOVIE
        assert first.continuation_token
        second = await provider.categories(
            CatalogQuery(limit=1, continuation_token=first.continuation_token)
        )
        assert second.items[0].media_type is MediaType.SERIES

    asyncio.run(run())


def test_detail_requires_type_and_maps_public_fields() -> None:
    async def run() -> None:
        provider = await _provider()
        with pytest.raises(ValueError, match="必须明确"):
            await provider.get_detail("1889243")
        movie = await provider.get_detail("1889243", MediaType.MOVIE)
        series = await provider.get_detail("35588177", MediaType.SERIES)
        assert movie.release_date is not None
        assert movie.release_date.isoformat() == "2014-11-12"
        assert movie.genres == ("剧情", "科幻", "冒险")
        assert movie.regions == ("美国", "英国")
        assert movie.rating == 9.4
        assert movie.vote_count == 2200000
        assert len(movie.image_urls) == 2
        assert series.media_type is MediaType.SERIES
        assert series.external_ids == {"douban.subject": "35588177"}

    asyncio.run(run())


def test_provider_passes_core_conformance_runner() -> None:
    async def run() -> None:
        provider = await _provider()
        report = await run_catalog_provider_conformance(
            provider,
            CatalogConformanceProbe(
                query=CatalogQuery(
                    keyword="星际穿越",
                    media_type=MediaType.MOVIE,
                    limit=1,
                ),
                detail_external_id="1889243",
                detail_media_type=MediaType.MOVIE,
            ),
        )
        assert report.plugin_id == "douban-catalog"
        assert set(report.checks) == {"search", "trending", "categories", "detail"}
        assert all(count > 0 for count in report.checks.values())

    asyncio.run(run())


def test_activated_douban_runs_through_explicit_core_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_cache_get(_key: str):
        return None

    async def no_cache_set(_key: str, _value: object) -> None:
        return None

    monkeypatch.setattr(catalog_cache, "get", no_cache_get)
    monkeypatch.setattr(catalog_cache, "set", no_cache_set)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    client_http = FixtureHttpClient()
    loader, manifest = _douban_manifest()
    activation = asyncio.run(
        PluginActivator(
            loader=loader,
            extra_capabilities={"core.http.v1": FixtureHttpFactory(client_http)},
        ).activate_candidate(
            manifest,
            ROOT,
            plugin_config={},
            repository_id="official",
            commit_hash="offline",
        )
    )
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    api = TestClient(app)
    try:
        providers = api.get("/discover/providers")
        assert providers.status_code == 200
        assert providers.json()[0]["id"] == "douban-catalog"
        search = api.get(
            "/discover/search",
            params={
                "q": "星际穿越",
                "provider_id": "douban-catalog",
                "media_type": "movie",
                "limit": 1,
                "refresh": "true",
            },
        )
        assert search.status_code == 200
        first = search.json()["items"][0]
        assert first["external_ids"]["douban.subject"] == "1889243"
        detail = api.get(
            f"/discover/{first['media_subject_id']}",
            params={"provider_id": "douban-catalog", "refresh": "true"},
        )
        assert detail.status_code == 200
        assert detail.json()["rating_provider"] == "douban-catalog"
    finally:
        asyncio.run(activation.dispose())
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_request_failure_does_not_echo_url_or_private_exception() -> None:
    class Rejected:
        async def get_json(self, url: str, *, headers=None):
            error = RuntimeError(f"private detail in {url}")
            error.code = 403  # type: ignore[attr-defined]
            raise error

    provider = DoubanCatalogProvider(Rejected())
    with pytest.raises(DoubanProviderError) as captured:
        asyncio.run(provider.initialize())
    assert "private detail" not in str(captured.value)
    assert "https://" not in str(captured.value)
    assert "拒绝" in str(captured.value)
