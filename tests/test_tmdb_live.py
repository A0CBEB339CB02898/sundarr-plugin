"""TMDb 显式实时集成测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sundarr.app import models  # noqa: F401
from sundarr.app.core.database import Base, get_db
from sundarr.app.main import create_app
from sundarr.app.plugins.activator import CandidateActivationError, PluginActivator
from sundarr.app.plugins.conformance import (
    CatalogConformanceProbe,
    run_catalog_provider_conformance,
)
from sundarr.app.plugins.contracts import CatalogQuery, MediaType
from sundarr.app.plugins.loader import PluginLoader
from sundarr.app.plugins.runtime import PluginActivation
from sundarr.app.plugins.runtime_registry import catalog_provider_registry
from sundarr.app.services.catalog_cache import catalog_cache


pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[1]


def test_tmdb_real_endpoint_rejects_invalid_token_safely() -> None:
    invalid_token = "definitely-invalid-live-smoke-token"
    catalog_provider_registry.clear()
    loader = PluginLoader(repos_dir=ROOT / ".live-cache")
    manifest = loader.parse_manifests(ROOT)[0]
    with pytest.raises(CandidateActivationError) as captured:
        asyncio.run(
            PluginActivator(loader=loader).activate_candidate(
                manifest,
                ROOT,
                plugin_config={"api_read_access_token": invalid_token},
                repository_id="sundarr-plugin-live-auth-failure",
                commit_hash="working-tree",
            )
        )
    catalog_provider_registry.clear()

    message = str(captured.value)
    assert "认证失败" in message
    assert invalid_token not in message


def test_tmdb_real_data_conformance_and_core_api(monkeypatch: pytest.MonkeyPatch) -> None:
    token = os.getenv("TMDB_API_READ_ACCESS_TOKEN", "").strip()
    if not token:
        pytest.skip("需要设置 TMDB_API_READ_ACCESS_TOKEN 才能执行真实数据验收")

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

    async def activate_and_check() -> PluginActivation:
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
            return activation
        except BaseException:
            await activation.dispose()
            catalog_provider_registry.clear()
            raise

    activation = asyncio.run(activate_and_check())
    app = create_app()

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    api = TestClient(app)
    try:
        providers = api.get("/discover/providers")
        assert providers.status_code == 200
        assert providers.json()[0]["id"] == "tmdb-catalog"
        assert {"tmdb.movie", "tmdb.tv"}.issubset(
            providers.json()[0]["identity_namespaces"]
        )
        assert providers.json()[0]["operation_filters"]["search"] == [
            "genre",
            "media_type",
            "year",
        ]
        assert providers.json()[0]["operation_sorts"]["search"] == []

        search = api.get(
            "/discover/search",
            params={
                "q": "盗梦空间",
                "media_type": "movie",
                "limit": 5,
                "refresh": "true",
            },
        )
        assert search.status_code == 200
        items = search.json()["items"]
        assert items
        assert any(item["poster_url"] for item in items)
        assert all(item["media_type"] == "movie" for item in items)

        trending = api.get(
            "/discover/trending",
            params={"media_type": "series", "limit": 5, "refresh": "true"},
        )
        categories = api.get(
            "/discover/categories",
            params={
                "media_type": "movie",
                "genre": "878",
                "sort": "popularity",
                "limit": 5,
                "refresh": "true",
            },
        )
        assert trending.status_code == 200
        assert trending.json()["items"]
        assert categories.status_code == 200
        assert categories.json()["items"]

        target = items[0]
        detail = api.get(
            f"/discover/{target['media_subject_id']}",
            params={"refresh": "true"},
        )
        assert detail.status_code == 200
        assert detail.json()["overview"]
        assert detail.json()["rating_provider"] == "tmdb-catalog"
        assert detail.json()["external_ids"]["tmdb.movie"] == target["external_id"]
    finally:
        asyncio.run(activation.dispose())
        catalog_provider_registry.clear()
        session.close()
        Base.metadata.drop_all(bind=engine)
