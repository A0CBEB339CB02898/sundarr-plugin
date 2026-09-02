"""豆瓣公开想看列表的显式实时集成测试。"""

from __future__ import annotations

import asyncio
import os

import pytest

from sundarr.app.plugins.conformance import run_watchlist_provider_conformance
from sundarr.app.plugins.contracts import WatchlistPullRequest
from sundarr.app.plugins.http import PluginHttpClient
from sundarr_official_plugins.douban_watchlist import DoubanWatchlistProvider


pytestmark = pytest.mark.live


def test_douban_public_watchlist_pulls_two_real_pages() -> None:
    user_id = os.environ.get("DOUBAN_WATCHLIST_USER_ID", "").strip()
    if not user_id:
        pytest.skip("需要显式设置 DOUBAN_WATCHLIST_USER_ID")

    async def run() -> None:
        provider = DoubanWatchlistProvider(
            PluginHttpClient(plugin_id="douban-watchlist-live"),
            user_id,
        )
        await provider.initialize()
        first = await provider.pull(WatchlistPullRequest(limit=10))
        report = await run_watchlist_provider_conformance(
            provider,
            WatchlistPullRequest(limit=10),
        )
        assert report.checks["pull"] > 0
        assert first.items
        assert all(item.subject.external_ids.get("douban.subject") for item in first.items)
        assert all(item.external_record_id for item in first.items)
        assert all(item.added_at is not None for item in first.items)
        assert first.next_cursor is not None

        second = await provider.pull(
            WatchlistPullRequest(cursor=first.next_cursor, limit=10)
        )
        assert second.items
        assert not {
            item.external_record_id for item in first.items
        }.intersection(item.external_record_id for item in second.items)

    asyncio.run(run())
