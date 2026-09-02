"""公开豆瓣想看列表到 Sundarr WATCHLIST_PROVIDER 合同的映射。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from html.parser import HTMLParser
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from sundarr.app.plugins.contracts import (
    CatalogItem,
    MediaType,
    PluginHealthResult,
    WatchlistItem,
    WatchlistPage,
    WatchlistPullRequest,
)


BASE_URL = "https://movie.douban.com"
_PAGE_SIZE = 30
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_SUBJECT_URL = re.compile(
    r"^(?:https://movie\.douban\.com)?/subject/(?P<id>\d+)/?$"
)
_YEAR = re.compile(r"(?<!\d)(?P<year>\d{4})(?!\d)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class TextHttpClient(Protocol):
    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str: ...


class DoubanWatchlistError(RuntimeError):
    """公开想看页面不能满足插件合同时的安全错误。"""


@dataclass(frozen=True)
class _StreamCursor:
    start: int = 0
    index: int = 0
    done: bool = False


@dataclass(frozen=True)
class _CursorState:
    movie: _StreamCursor = _StreamCursor()
    tv: _StreamCursor = _StreamCursor()


@dataclass(frozen=True)
class _ParsedPage:
    items: tuple[WatchlistItem, ...]
    next_start: int | None


class _WishListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page_title = ""
        self.items: list[dict[str, str | None]] = []
        self.links: list[str] = []
        self._depth = 0
        self._page_title_depth: int | None = None
        self._page_title_parts: list[str] = []
        self._item_depth: int | None = None
        self._current: dict[str, str | None] | None = None
        self._title_depth: int | None = None
        self._title_parts: list[str] = []
        self._date_depth: int | None = None
        self._date_parts: list[str] = []
        self._intro_depth: int | None = None
        self._intro_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag not in _VOID_TAGS:
            self._depth += 1

        if tag == "title":
            self._page_title_depth = self._depth
            self._page_title_parts = []

        if tag == "li" and "item" in classes and self._current is None:
            self._item_depth = self._depth
            self._current = {
                "subject_id": None,
                "record_id": None,
                "title": None,
                "added_at": None,
                "intro": None,
            }

        if self._current is not None:
            record_id = attributes.get("data-cid")
            if record_id and self._current["record_id"] is None:
                self._current["record_id"] = record_id.strip()

            href = attributes.get("href")
            match = _SUBJECT_URL.fullmatch(href or "") if tag == "a" else None
            if match and self._current["subject_id"] is None:
                self._current["subject_id"] = match.group("id")
                self._title_depth = self._depth
                self._title_parts = []

            if tag == "div" and "date" in classes:
                self._date_depth = self._depth
                self._date_parts = []
            if tag == "span" and "intro" in classes:
                self._intro_depth = self._depth
                self._intro_parts = []

        href = attributes.get("href")
        if tag == "a" and href and "/wish?" in href:
            self.links.append(href)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._page_title_depth is not None:
            self._page_title_parts.append(data)
        if self._title_depth is not None:
            self._title_parts.append(data)
        if self._date_depth is not None:
            self._date_parts.append(data)
        if self._intro_depth is not None:
            self._intro_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth == self._depth and tag == "a" and self._current:
            self._current["title"] = _compact(self._title_parts)
            self._title_depth = None
        if self._date_depth == self._depth and tag == "div" and self._current:
            self._current["added_at"] = _compact(self._date_parts)
            self._date_depth = None
        if self._intro_depth == self._depth and tag == "span" and self._current:
            self._current["intro"] = _compact(self._intro_parts)
            self._intro_depth = None
        if self._page_title_depth == self._depth and tag == "title":
            self.page_title = _compact(self._page_title_parts)
            self._page_title_depth = None
        if self._item_depth == self._depth and tag == "li" and self._current:
            self.items.append(self._current)
            self._current = None
            self._item_depth = None
            self._title_depth = None
            self._date_depth = None
            self._intro_depth = None
        if tag not in _VOID_TAGS and self._depth > 0:
            self._depth -= 1


class DoubanWatchlistProvider:
    """按电影和电视双流同步公开豆瓣想看列表。"""

    id = "douban-watchlist"

    def __init__(
        self,
        http_client: TextHttpClient,
        user_id: str,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        normalized_user_id = user_id.strip()
        if not normalized_user_id.isdigit():
            raise ValueError("豆瓣用户 ID 必须完全由数字组成")
        self._http = http_client
        self._user_id = normalized_user_id
        self._user_hash = hashlib.sha256(normalized_user_id.encode()).hexdigest()[:16]
        self._logger = logger or logging.getLogger("sundarr.plugin.douban-watchlist")
        self._initialized = False

    async def initialize(self) -> None:
        await self._fetch_page("movie", 0)
        self._initialized = True

    def health_check(self) -> PluginHealthResult:
        return PluginHealthResult(
            ok=self._initialized,
            message="豆瓣想看插件可用" if self._initialized else "豆瓣想看插件尚未初始化",
            details={"access": "公开列表", "mode": "list"},
        )

    async def pull(self, request: WatchlistPullRequest) -> WatchlistPage:
        if not self._initialized:
            raise DoubanWatchlistError("豆瓣想看 Provider 尚未初始化")
        state = _decode_cursor(request.cursor, self._user_hash)
        movie_cursor, movie_page = await self._load_current("movie", state.movie)
        tv_cursor, tv_page = await self._load_current("tv", state.tv)

        candidates = [
            ("movie", item)
            for item in movie_page.items[movie_cursor.index :]
        ] + [
            ("tv", item)
            for item in tv_page.items[tv_cursor.index :]
        ]
        candidates.sort(key=_merged_sort_key, reverse=True)
        selected = candidates[: min(request.limit, _PAGE_SIZE)]
        consumed_movie = sum(kind == "movie" for kind, _ in selected)
        consumed_tv = sum(kind == "tv" for kind, _ in selected)
        next_movie = _advance_stream(movie_cursor, movie_page, consumed_movie)
        next_tv = _advance_stream(tv_cursor, tv_page, consumed_tv)
        next_state = _CursorState(movie=next_movie, tv=next_tv)
        next_cursor = (
            None
            if next_movie.done and next_tv.done
            else _encode_cursor(next_state, self._user_hash)
        )
        return WatchlistPage(
            items=tuple(item for _, item in selected),
            next_cursor=next_cursor,
        )

    async def _load_current(
        self,
        kind: str,
        cursor: _StreamCursor,
    ) -> tuple[_StreamCursor, _ParsedPage]:
        current = cursor
        for _ in range(3):
            if current.done:
                return current, _ParsedPage(items=(), next_start=None)
            page = await self._fetch_page(kind, current.start)
            if current.index < len(page.items):
                return current, page
            if page.next_start is None:
                done = replace(current, index=0, done=True)
                return done, _ParsedPage(items=(), next_start=None)
            current = _StreamCursor(start=page.next_start)
        raise DoubanWatchlistError("豆瓣想看连续返回空分页")

    async def _fetch_page(self, kind: str, start: int) -> _ParsedPage:
        if kind not in {"movie", "tv"}:
            raise ValueError("豆瓣想看类型必须是 movie 或 tv")
        query = urlencode(
            {
                "sort": "time",
                "start": start,
                "filter": "all",
                "mode": "list",
                "type": kind,
                "tags_sort": "count",
            }
        )
        url = f"{BASE_URL}/people/{self._user_id}/wish?{query}"
        try:
            payload = await self._http.get_text(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": f"{BASE_URL}/",
                    "User-Agent": _USER_AGENT,
                },
            )
        except Exception as exc:
            raise DoubanWatchlistError(
                f"豆瓣想看页面请求失败（{type(exc).__name__}）"
            ) from exc
        return _parse_page(payload, kind, start)


async def activate(context: Any) -> DoubanWatchlistProvider:
    """Manifest v2 入口。"""

    factory = context.require("core.http.v1")
    client = factory.create(context.plugin_id)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if callable(close):
        context.register_cleanup(close)
    provider = DoubanWatchlistProvider(
        client,
        str(context.plugin_config["user_id"]),
        logger=context.logger,
    )
    await provider.initialize()
    return provider


def _parse_page(payload: str, kind: str, start: int) -> _ParsedPage:
    parser = _WishListParser()
    try:
        parser.feed(payload)
        parser.close()
    except Exception as exc:
        raise DoubanWatchlistError("豆瓣想看页面 HTML 无法解析") from exc
    expected_title = "想看的电影" if kind == "movie" else "想看的电视剧"
    if expected_title not in parser.page_title:
        raise DoubanWatchlistError("豆瓣想看页面不可公开访问或触发了保护页面")

    media_type = MediaType.MOVIE if kind == "movie" else MediaType.SERIES
    items: list[WatchlistItem] = []
    seen_records: set[str] = set()
    for raw in parser.items:
        subject_id = (raw.get("subject_id") or "").strip()
        title_value = (raw.get("title") or "").strip()
        added_value = (raw.get("added_at") or "").strip()
        if not subject_id or not title_value or not _DATE.fullmatch(added_value):
            continue
        record_id = (raw.get("record_id") or f"subject:{subject_id}").strip()
        if record_id in seen_records:
            continue
        seen_records.add(record_id)
        title, original_title = _split_title(title_value)
        year = _parse_year(raw.get("intro"))
        added_at = datetime.strptime(added_value, "%Y-%m-%d").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
        items.append(
            WatchlistItem(
                external_record_id=record_id,
                added_at=added_at,
                subject=CatalogItem(
                    external_id=subject_id,
                    external_id_provider="douban.subject",
                    external_ids={"douban.subject": subject_id},
                    title=title,
                    original_title=original_title,
                    media_type=media_type,
                    year=year,
                ),
            )
        )

    items.sort(key=lambda item: item.added_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    next_starts = []
    for href in parser.links:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if query.get("type") != [kind]:
            continue
        try:
            candidate = int(query.get("start", ["-1"])[0])
        except ValueError:
            continue
        if candidate > start:
            next_starts.append(candidate)
    return _ParsedPage(
        items=tuple(items),
        next_start=min(next_starts) if next_starts else None,
    )


def _advance_stream(
    cursor: _StreamCursor,
    page: _ParsedPage,
    consumed: int,
) -> _StreamCursor:
    if cursor.done:
        return cursor
    next_index = cursor.index + consumed
    if next_index < len(page.items):
        return replace(cursor, index=next_index)
    if page.next_start is None:
        return _StreamCursor(done=True)
    return _StreamCursor(start=page.next_start)


def _merged_sort_key(candidate: tuple[str, WatchlistItem]) -> tuple[datetime, int, str]:
    kind, item = candidate
    added_at = item.added_at or datetime.min.replace(tzinfo=UTC)
    return added_at, 1 if kind == "movie" else 0, item.external_record_id or ""


def _encode_cursor(state: _CursorState, user_hash: str) -> str:
    payload = {
        "v": 1,
        "u": user_hash,
        "movie": {
            "start": state.movie.start,
            "index": state.movie.index,
            "done": state.movie.done,
        },
        "tv": {
            "start": state.tv.start,
            "index": state.tv.index,
            "done": state.tv.done,
        },
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(value: str | None, user_hash: str) -> _CursorState:
    if value is None:
        return _CursorState()
    if len(value) > 2048:
        raise ValueError("豆瓣想看 cursor 过长")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode())
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("豆瓣想看 cursor 无效") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("u") != user_hash:
        raise ValueError("豆瓣想看 cursor 版本或用户不匹配")
    return _CursorState(
        movie=_decode_stream(payload.get("movie")),
        tv=_decode_stream(payload.get("tv")),
    )


def _decode_stream(value: object) -> _StreamCursor:
    if not isinstance(value, dict):
        raise ValueError("豆瓣想看 cursor 缺少类型游标")
    start = value.get("start")
    index = value.get("index")
    done = value.get("done")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or start < 0
        or start > 10_000_000
        or isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index > _PAGE_SIZE
        or not isinstance(done, bool)
    ):
        raise ValueError("豆瓣想看 cursor 类型游标无效")
    return _StreamCursor(start=start, index=index, done=done)


def _split_title(value: str) -> tuple[str, str | None]:
    title, separator, original = value.partition(" / ")
    return title.strip(), original.strip() if separator and original.strip() else None


def _parse_year(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _YEAR.search(value)
    if not match:
        return None
    year = int(match.group("year"))
    return year if 1888 <= year <= datetime.now().year + 10 else None


def _compact(parts: list[str]) -> str:
    return " ".join("".join(parts).split())
