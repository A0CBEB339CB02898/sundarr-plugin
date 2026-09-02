"""豆瓣想看插件的离线解析、游标和 Core 集成测试。"""

from __future__ import annotations

import asyncio
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
from sundarr.app.models import MediaWatchlistEntry, WatchlistSyncState
from sundarr.app.plugins.activator import PluginActivator
from sundarr.app.plugins.conformance import run_watchlist_provider_conformance
from sundarr.app.plugins.contracts import MediaType, WatchlistPullRequest
from sundarr.app.plugins.loader import PluginLoader
from sundarr.app.plugins.runtime_registry import watchlist_provider_registry
from sundarr_official_plugins.douban_watchlist import (
    DoubanWatchlistError,
    DoubanWatchlistProvider,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "douban_watchlist"


class FixtureTextClient:
    def __init__(self, *, expected_user_id: str = "123456", protected: bool = False) -> None:
        self.expected_user_id = expected_user_id
        self.protected = protected
        self.calls: list[tuple[str, int, dict[str, str]]] = []

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        kind = query["type"][0]
        start = int(query["start"][0])
        request_headers = dict(headers or {})
        self.calls.append((kind, start, request_headers))
        assert parsed.scheme == "https"
        assert parsed.hostname == "movie.douban.com"
        assert parsed.path == f"/people/{self.expected_user_id}/wish"
        assert query["mode"] == ["list"]
        assert query["sort"] == ["time"]
        assert query["filter"] == ["all"]
        assert request_headers["Referer"] == "https://movie.douban.com/"
        if self.protected:
            return "<html><head><title>登录豆瓣</title></head><body></body></html>"
        path = FIXTURE / f"{kind}-{start}.html"
        if not path.exists():
            raise AssertionError(f"未声明离线想看页：{kind}-{start}")
        return path.read_text(encoding="utf-8")


class FixtureHttpFactory:
    def __init__(self, client: FixtureTextClient) -> None:
        self.client = client

    def create(self, plugin_id: str) -> FixtureTextClient:
        assert plugin_id == "douban-watchlist"
        return self.client


@pytest.fixture(autouse=True)
def clear_watchlist_registry():
    watchlist_provider_registry.clear()
    yield
    watchlist_provider_registry.clear()


async def make_provider(
    client: FixtureTextClient | None = None,
    user_id: str = "123456",
) -> DoubanWatchlistProvider:
    provider = DoubanWatchlistProvider(
        client or FixtureTextClient(expected_user_id=user_id),
        user_id,
    )
    await provider.initialize()
    return provider


def manifest():
    loader = PluginLoader(repos_dir=ROOT / ".test-cache")
    parsed = loader.parse_manifests(ROOT)
    return loader, next(item for item in parsed if item.id == "douban-watchlist")


def test_manifest_activates_watchlist_independently() -> None:
    async def run() -> None:
        client = FixtureTextClient()
        loader, watchlist_manifest = manifest()
        assert watchlist_manifest.config_schema == {
            "user_id": {
                "type": "string",
                "label": "豆瓣用户 ID",
                "required": True,
                "placeholder": "例如 164867789",
            }
        }
        activation = await PluginActivator(
            loader=loader,
            extra_capabilities={"core.http.v1": FixtureHttpFactory(client)},
        ).activate_candidate(
            watchlist_manifest,
            ROOT,
            plugin_config={"user_id": "123456"},
            repository_id="official",
            commit_hash="offline",
        )
        assert watchlist_provider_registry.require("douban-watchlist") is activation.instance
        assert activation.instance.health_check().ok is True
        assert set(activation.provided_capabilities) == {"watchlist.pull.v1"}
        await activation.dispose()
        assert len(watchlist_provider_registry) == 0

    asyncio.run(run())


def test_pull_merges_movie_tv_and_resumes_without_skipping_short_page() -> None:
    async def run() -> None:
        provider = await make_provider()
        cursor = None
        collected = []
        for _ in range(4):
            page = await provider.pull(WatchlistPullRequest(cursor=cursor, limit=2))
            collected.extend(page.items)
            cursor = page.next_cursor

        assert [item.subject.external_id for item in collected] == [
            "100",
            "200",
            "101",
            "201",
            "102",
            "103",
            "104",
        ]
        assert len({item.external_record_id for item in collected}) == 7
        assert collected[0].subject.media_type is MediaType.MOVIE
        assert collected[1].subject.media_type is MediaType.SERIES
        assert collected[0].subject.title == "电影甲"
        assert collected[0].subject.original_title == "Movie A"
        assert collected[0].subject.year == 2026
        assert collected[4].external_record_id == "subject:102"
        assert collected[-1].subject.year is None
        assert cursor is None

    asyncio.run(run())


def test_cursor_is_bound_to_user_and_protection_page_fails() -> None:
    async def run() -> None:
        provider = await make_provider()
        first = await provider.pull(WatchlistPullRequest(limit=1))
        other = await make_provider(user_id="654321")
        with pytest.raises(ValueError, match="版本或用户不匹配"):
            await other.pull(WatchlistPullRequest(cursor=first.next_cursor, limit=1))

        protected = DoubanWatchlistProvider(FixtureTextClient(protected=True), "123456")
        with pytest.raises(DoubanWatchlistError, match="不可公开访问"):
            await protected.initialize()

    asyncio.run(run())


def test_contract_and_core_sync_are_incremental_and_idempotent() -> None:
    async def run() -> None:
        provider = await make_provider()
        report = await run_watchlist_provider_conformance(
            provider,
            WatchlistPullRequest(limit=2),
        )
        assert report.checks == {"pull": 2}
        watchlist_provider_registry.register(provider.id, provider)

        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        session = session_factory()
        app = create_app()

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        try:
            pulled = []
            for _ in range(4):
                response = client.post(
                    "/discover/watchlist/douban-watchlist/sync",
                    params={"limit": 2},
                )
                assert response.status_code == 200
                pulled.append(response.json()["pulled_count"])
            assert pulled == [2, 2, 1, 2]
            assert session.query(MediaWatchlistEntry).count() == 7
            assert session.get(WatchlistSyncState, "douban-watchlist").cursor is None
            assert client.get("/discover/watchlist").json()["count"] == 7

            replay = client.post(
                "/discover/watchlist/douban-watchlist/sync",
                params={"limit": 2},
            )
            assert replay.status_code == 200
            assert session.query(MediaWatchlistEntry).count() == 7
        finally:
            session.close()
            engine.dispose()

    asyncio.run(run())
