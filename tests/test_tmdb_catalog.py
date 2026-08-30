"""TMDb 目录插件离线合同和映射测试。"""

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
from sundarr_official_plugins.tmdb_catalog import TmdbCatalogProvider, TmdbProviderError


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []

    async def get_json(self, url: str, *, headers: dict[str, str] | None = None):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        request_headers = dict(headers or {})
        self.calls.append((parsed.path, query, request_headers))
        assert request_headers["Authorization"] == "Bearer offline-token"
        path = parsed.path.removeprefix("/3")
        if path == "/configuration":
            return deepcopy(_fixture("configuration.json"))
        if path == "/configuration/countries":
            return deepcopy(_fixture("countries.json"))
        if path == "/genre/movie/list":
            return deepcopy(_fixture("movie_genres.json"))
        if path == "/genre/tv/list":
            return deepcopy(_fixture("tv_genres.json"))
        if path == "/movie/603":
            return deepcopy(_fixture("movie_detail.json"))
        if path == "/tv/1399":
            return deepcopy(_fixture("tv_detail.json"))
        if path in {
            "/search/multi",
            "/trending/all/day",
        }:
            return deepcopy(_fixture("search_page_2.json" if query.get("page") == ["2"] else "search_page_1.json"))
        if path in {
            "/search/movie",
            "/trending/movie/day",
            "/discover/movie",
        }:
            payload = deepcopy(_fixture("search_page_2.json" if query.get("page") == ["2"] else "search_page_1.json"))
            payload["results"] = [item for item in payload["results"] if item.get("media_type") == "movie"]
            return payload
        if path in {
            "/search/tv",
            "/trending/tv/day",
            "/discover/tv",
        }:
            payload = deepcopy(_fixture("search_page_1.json"))
            payload["total_pages"] = 1
            payload["results"] = [item for item in payload["results"] if item.get("media_type") == "tv"]
            return payload
        raise AssertionError(f"未声明离线 TMDb 路由：{path}")


class FixtureHttpFactory:
    def __init__(self, client: FixtureHttpClient) -> None:
        self.client = client

    def create(self, plugin_id: str) -> FixtureHttpClient:
        assert plugin_id == "tmdb-catalog"
        return self.client


async def _provider(client: FixtureHttpClient | None = None) -> TmdbCatalogProvider:
    provider = TmdbCatalogProvider(
        client or FixtureHttpClient(),
        api_read_access_token="offline-token",
    )
    await provider.initialize()
    return provider


@pytest.fixture(autouse=True)
def _clear_catalog_registry():
    catalog_provider_registry.clear()
    yield
    catalog_provider_registry.clear()


def test_manifest_v2_can_activate_tmdb_with_public_core_contract() -> None:
    async def run() -> None:
        client = FixtureHttpClient()
        loader = PluginLoader(repos_dir=ROOT / ".test-cache")
        manifest = loader.parse_manifests(ROOT)[0]
        assert manifest.id == "tmdb-catalog"
        assert manifest.entry == "plugin_entry:activate_tmdb_catalog"
        activation = await PluginActivator(
            loader=loader,
            extra_capabilities={"core.http.v1": FixtureHttpFactory(client)},
        ).activate_candidate(
            manifest,
            ROOT,
            plugin_config={"api_read_access_token": "offline-token"},
            repository_id="official",
            commit_hash="offline",
        )
        assert catalog_provider_registry.require("tmdb-catalog") is activation.instance
        assert set(activation.provided_capabilities) == set(manifest.provides)
        assert activation.instance.health_check().ok is True
        await activation.dispose()
        assert len(catalog_provider_registry) == 0

    asyncio.run(run())


def test_activated_tmdb_runs_through_core_discover_api(monkeypatch: pytest.MonkeyPatch) -> None:
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
    loader = PluginLoader(repos_dir=ROOT / ".test-cache")
    manifest = loader.parse_manifests(ROOT)[0]
    activation = asyncio.run(
        PluginActivator(
            loader=loader,
            extra_capabilities={"core.http.v1": FixtureHttpFactory(client_http)},
        ).activate_candidate(
            manifest,
            ROOT,
            plugin_config={"api_read_access_token": "offline-token"},
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
        assert providers.json()[0]["id"] == "tmdb-catalog"
        assert providers.json()[0]["operation_filters"]["search"] == [
            "genre",
            "media_type",
            "year",
        ]
        assert providers.json()[0]["operation_sorts"]["search"] == []
        assert providers.json()[0]["attribution"]["provider_name"] == "TMDB"
        assert providers.json()[0]["attribution"]["logo_url"].endswith(".svg")

        search = api.get(
            "/discover/search",
            params={"q": "matrix", "media_type": "movie", "limit": 2, "refresh": "true"},
        )
        assert search.status_code == 200
        first = search.json()["items"][0]
        assert first["external_ids"]["tmdb.movie"] == "603"

        detail = api.get(
            f"/discover/{first['media_subject_id']}",
            params={"refresh": "true"},
        )
        assert detail.status_code == 200
        assert detail.json()["external_ids"]["imdb"] == "tt0133093"
        assert any(path.endswith("/movie/603") for path, _query, _headers in client_http.calls)

        trending = api.get(
            "/discover/trending",
            params={"media_type": "series", "limit": 1, "refresh": "true"},
        )
        categories = api.get(
            "/discover/categories",
            params={"media_type": "movie", "genre": "28", "limit": 1, "refresh": "true"},
        )
        assert trending.status_code == 200
        assert categories.status_code == 200
    finally:
        asyncio.run(activation.dispose())
        session.close()
        Base.metadata.drop_all(bind=engine)


def test_capabilities_are_built_from_runtime_genres_and_countries() -> None:
    capabilities = asyncio.run(_provider()).describe_capabilities()
    assert capabilities.identity_namespaces == frozenset({"tmdb.movie", "tmdb.tv"})
    assert capabilities.media_types == frozenset(MediaType)
    assert capabilities.sorts == frozenset(CatalogSort)
    assert capabilities.filters_for(CatalogOperation.SEARCH) == frozenset(
        {CatalogFilter.MEDIA_TYPE, CatalogFilter.GENRE, CatalogFilter.YEAR}
    )
    assert capabilities.filters_for(CatalogOperation.TRENDING) == frozenset(
        {CatalogFilter.MEDIA_TYPE}
    )
    assert capabilities.sorts_for(CatalogOperation.SEARCH) == frozenset()
    assert capabilities.sorts_for(CatalogOperation.CATEGORIES) == frozenset(CatalogSort)
    assert capabilities.attribution is not None
    assert capabilities.attribution.provider_name == "TMDB"
    assert capabilities.attribution.homepage_url == "https://www.themoviedb.org"
    assert "not endorsed or certified" in capabilities.attribution.notice
    assert capabilities.attribution.logo_url is not None
    assert capabilities.attribution.logo_url.endswith(".svg")
    genre_values = {item.value for item in capabilities.filter_options[CatalogFilter.GENRE]}
    region_values = {item.value for item in capabilities.filter_options[CatalogFilter.REGION]}
    assert {"18", "28", "10765"}.issubset(genre_values)
    assert region_values == {"CN", "US"}


def test_search_maps_movie_and_tv_and_skips_people() -> None:
    async def run() -> None:
        provider = await _provider()
        page = await provider.search(CatalogQuery(keyword="matrix", limit=10))
        assert [item.media_type for item in page.items] == [MediaType.MOVIE, MediaType.SERIES, MediaType.MOVIE]
        movie = page.items[0]
        series = page.items[1]
        assert movie.external_id_provider == "tmdb.movie"
        assert movie.external_ids == {"tmdb.movie": "603"}
        assert movie.poster_url == "https://image.tmdb.org/t/p/w500/matrix-poster.jpg"
        assert movie.year == 1999
        assert series.external_id_provider == "tmdb.tv"
        assert series.external_ids == {"tmdb.tv": "1399"}
        assert series.genres == ("剧情", "科幻奇幻")

    asyncio.run(run())


def test_search_applies_genre_and_year_without_silent_ignore() -> None:
    async def run() -> None:
        provider = await _provider()
        page = await provider.search(
            CatalogQuery(
                keyword="matrix",
                media_type=MediaType.MOVIE,
                genres=("28",),
                year_from=1999,
                year_to=1999,
                limit=10,
            )
        )
        assert [item.external_id for item in page.items] == ["603"]

    asyncio.run(run())


def test_search_and_trending_reject_capabilities_only_available_to_categories() -> None:
    async def run() -> None:
        provider = await _provider()
        with pytest.raises(ValueError, match="search 不支持筛选：region"):
            await provider.search(CatalogQuery(keyword="matrix", regions=("US",)))
        with pytest.raises(ValueError, match="search 不支持排序：rating"):
            await provider.search(
                CatalogQuery(keyword="matrix", sort=CatalogSort.RATING)
            )
        with pytest.raises(ValueError, match="trending 不支持筛选：genre"):
            await provider.trending(CatalogQuery(genres=("28",)))

    asyncio.run(run())


def test_continuation_token_resumes_inside_page_and_binds_query() -> None:
    async def run() -> None:
        provider = await _provider()
        first = await provider.search(CatalogQuery(keyword="matrix", limit=1))
        assert [item.external_id for item in first.items] == ["603"]
        assert first.continuation_token
        second = await provider.search(
            CatalogQuery(keyword="matrix", limit=1, continuation_token=first.continuation_token)
        )
        assert [item.external_id for item in second.items] == ["1399"]
        with pytest.raises(ValueError, match="不匹配"):
            await provider.search(
                CatalogQuery(keyword="different", limit=1, continuation_token=first.continuation_token)
            )

    asyncio.run(run())


def test_trending_continuation_stays_inside_single_remote_response() -> None:
    async def run() -> None:
        client = FixtureHttpClient()
        provider = await _provider(client)
        first = await provider.trending(CatalogQuery(limit=1))
        assert first.continuation_token
        second = await provider.trending(
            CatalogQuery(limit=10, continuation_token=first.continuation_token)
        )
        assert second.items
        assert second.continuation_token is None
        trending_calls = [
            query
            for path, query, _headers in client.calls
            if path.endswith("/trending/all/day")
        ]
        assert trending_calls
        assert all("page" not in query for query in trending_calls)

    asyncio.run(run())


def test_categories_maps_filters_sort_and_mixes_movie_with_series() -> None:
    async def run() -> None:
        client = FixtureHttpClient()
        provider = await _provider(client)
        movie_page = await provider.categories(
            CatalogQuery(
                media_type=MediaType.MOVIE,
                genres=("28",),
                regions=("US",),
                year_from=1999,
                year_to=2020,
                sort=CatalogSort.RATING,
                limit=2,
            )
        )
        assert movie_page.items
        path, query, _ = next(call for call in client.calls if call[0].endswith("/discover/movie"))
        assert path.endswith("/discover/movie")
        assert query["with_genres"] == ["28"]
        assert query["with_origin_country"] == ["US"]
        assert query["primary_release_date.gte"] == ["1999-01-01"]
        assert query["primary_release_date.lte"] == ["2020-12-31"]
        assert query["sort_by"] == ["vote_average.desc"]
        assert query["vote_count.gte"] == ["1"]

        mixed = await provider.categories(CatalogQuery(limit=4))
        assert {item.media_type for item in mixed.items} == {MediaType.MOVIE, MediaType.SERIES}

    asyncio.run(run())


def test_mixed_categories_limit_one_alternates_without_starving_series() -> None:
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


def test_detail_requires_media_type_and_maps_imdb_and_images() -> None:
    async def run() -> None:
        provider = await _provider()
        with pytest.raises(ValueError, match="必须明确"):
            await provider.get_detail("603")
        movie = await provider.get_detail("603", MediaType.MOVIE)
        series = await provider.get_detail("1399", MediaType.SERIES)
        assert movie.external_ids == {"tmdb.movie": "603", "imdb": "tt0133093"}
        assert movie.regions == ("US",)
        assert movie.genres == ("动作", "科幻")
        assert movie.image_urls == (
            "https://image.tmdb.org/t/p/w500/matrix-poster.jpg",
            "https://image.tmdb.org/t/p/w500/matrix-backdrop.jpg",
        )
        assert series.external_ids == {"tmdb.tv": "1399", "imdb": "tt0944947"}

    asyncio.run(run())


def test_offline_provider_passes_core_conformance_runner() -> None:
    async def run() -> None:
        provider = await _provider()
        report = await run_catalog_provider_conformance(
            provider,
            CatalogConformanceProbe(
                query=CatalogQuery(keyword="matrix", media_type=MediaType.MOVIE, limit=2),
                detail_external_id="603",
                detail_media_type=MediaType.MOVIE,
            ),
        )
        assert report.plugin_id == "tmdb-catalog"
        assert set(report.checks) == {"search", "trending", "categories", "detail"}
        assert all(count > 0 for count in report.checks.values())

    asyncio.run(run())


def test_provider_error_does_not_echo_token() -> None:
    class Unauthorized:
        async def get_json(self, url: str, *, headers=None):
            error = RuntimeError("offline-token")
            error.code = 401  # type: ignore[attr-defined]
            raise error

    provider = TmdbCatalogProvider(Unauthorized(), api_read_access_token="offline-token")
    with pytest.raises(TmdbProviderError) as captured:
        asyncio.run(provider.initialize())
    assert "offline-token" not in str(captured.value)
    assert "认证失败" in str(captured.value)


def test_malformed_continuation_token_is_rejected_as_parameter_error() -> None:
    provider = asyncio.run(_provider())
    with pytest.raises(ValueError, match="continuation_token 无效"):
        asyncio.run(
            provider.search(
                CatalogQuery(keyword="matrix", continuation_token="%%%")
            )
        )
