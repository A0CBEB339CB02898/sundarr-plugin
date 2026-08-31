"""豆瓣公开影视目录到 Sundarr 公共合同的映射。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import Any, Protocol
from urllib.parse import urlencode

from sundarr.app.plugins.contracts import (
    CatalogAttribution,
    CatalogCapabilities,
    CatalogFilter,
    CatalogFilterOption,
    CatalogItem,
    CatalogOperation,
    CatalogPage,
    CatalogQuery,
    CatalogSort,
    MediaType,
    PluginHealthResult,
)


MOVIE_BASE_URL = "https://movie.douban.com"
MOBILE_BASE_URL = "https://m.douban.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_GENRES = (
    "剧情",
    "喜剧",
    "动作",
    "爱情",
    "科幻",
    "动画",
    "悬疑",
    "惊悚",
    "恐怖",
    "纪录片",
    "短片",
    "音乐",
    "歌舞",
    "家庭",
    "儿童",
    "传记",
    "历史",
    "战争",
    "犯罪",
    "西部",
    "奇幻",
    "冒险",
    "灾难",
    "武侠",
    "古装",
    "运动",
)
_YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4})(?:-(\d{2})-(\d{2}))?")


class JsonHttpClient(Protocol):
    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any: ...


class DoubanProviderError(RuntimeError):
    """豆瓣调用或响应无法满足公共合同。"""


class DoubanCatalogProvider:
    """使用公开 JSON 响应实现豆瓣补充目录。"""

    id = "douban-catalog"

    def __init__(
        self,
        http_client: JsonHttpClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._http = http_client
        self._logger = logger or logging.getLogger("sundarr.plugin.douban-catalog")
        self._initialized = False

    async def initialize(self) -> None:
        """用最小公开请求确认当前候选可以访问目录响应。"""

        payload = _require_mapping(
            await self._request_movie(
                "/j/search_subjects",
                {"type": "movie", "tag": "热门", "page_limit": 1, "page_start": 0},
            ),
            "初始化",
        )
        _require_sequence(payload.get("subjects"), "初始化.subjects")
        self._initialized = True

    def describe_capabilities(self) -> CatalogCapabilities:
        self._require_initialized()
        genre_options = tuple(
            CatalogFilterOption(value=genre, label=genre) for genre in _GENRES
        )
        return CatalogCapabilities(
            operations=frozenset(CatalogOperation),
            media_types=frozenset(MediaType),
            filters=frozenset(
                {CatalogFilter.MEDIA_TYPE, CatalogFilter.GENRE, CatalogFilter.YEAR}
            ),
            sorts=frozenset(CatalogSort),
            operation_filters={
                CatalogOperation.SEARCH: frozenset(
                    {CatalogFilter.MEDIA_TYPE, CatalogFilter.YEAR}
                ),
                CatalogOperation.TRENDING: frozenset({CatalogFilter.MEDIA_TYPE}),
                CatalogOperation.CATEGORIES: frozenset(
                    {CatalogFilter.MEDIA_TYPE, CatalogFilter.GENRE}
                ),
            },
            operation_sorts={
                CatalogOperation.SEARCH: frozenset(),
                CatalogOperation.TRENDING: frozenset(),
                CatalogOperation.CATEGORIES: frozenset(CatalogSort),
            },
            attribution=CatalogAttribution(
                provider_name="豆瓣",
                homepage_url="https://movie.douban.com",
                notice="数据来源于豆瓣公开页面；豆瓣与本项目无隶属、合作或背书关系。",
                image_referer_url="https://movie.douban.com/",
            ),
            identity_namespaces=frozenset({"douban.subject"}),
            filter_options={CatalogFilter.GENRE: genre_options},
        )

    def health_check(self) -> PluginHealthResult:
        if not self._initialized:
            return PluginHealthResult(ok=False, message="豆瓣目录尚未初始化")
        return PluginHealthResult(
            ok=True,
            message="豆瓣目录插件可用",
            details={"access": "公开目录", "genres": str(len(_GENRES))},
        )

    async def search(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.SEARCH, query)
        keyword = (query.keyword or "").strip()
        if not keyword:
            raise ValueError("豆瓣搜索必须提供关键词")
        signature = _query_signature("search", query)
        offset = _decode_offset_token(query.continuation_token, signature, "search")
        payload = _require_sequence(
            await self._request_movie("/j/subject_suggest", {"q": keyword}),
            "search",
        )
        items: list[CatalogItem] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            media_type = _media_type(raw.get("type"))
            if media_type is None or not _matches_search(raw, media_type, query):
                continue
            try:
                items.append(self._map_summary(raw, media_type, suggestion=True))
            except (TypeError, ValueError, DoubanProviderError) as exc:
                self._logger.warning("豆瓣 search 跳过无法映射的目录项：%s", type(exc).__name__)
        page_items = tuple(items[offset : offset + query.limit])
        next_offset = offset + len(page_items)
        continuation = None
        if next_offset < len(items):
            continuation = _encode_offset_token(signature, "search", next_offset)
        return CatalogPage(items=page_items, continuation_token=continuation)

    async def trending(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.TRENDING, query)
        if query.media_type is None:
            return await self._mixed_page("trending", query)
        return await self._trending_for_type(query)

    async def categories(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.CATEGORIES, query)
        if query.genres and query.genres[0] not in _GENRES:
            raise ValueError(f"豆瓣 categories 不支持题材：{query.genres[0]}")
        if query.media_type is None:
            return await self._mixed_page("categories", query)
        return await self._categories_for_type(query)

    async def get_detail(
        self,
        external_id: str,
        media_type: MediaType | None = None,
    ) -> CatalogItem:
        self._require_initialized()
        normalized_id = external_id.strip()
        if not normalized_id:
            raise ValueError("豆瓣 external_id 不能为空")
        if media_type is None:
            raise ValueError("豆瓣详情查询必须明确 movie 或 series")
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        payload = _require_mapping(
            await self._request_mobile(f"/rexxar/api/v2/{kind}/{normalized_id}"),
            f"{kind} detail",
        )
        return self._map_detail(payload, media_type)

    async def _trending_for_type(self, query: CatalogQuery) -> CatalogPage:
        if query.media_type is None:
            raise ValueError("热门查询缺少媒体类型")
        signature = _query_signature(f"trending:{query.media_type.value}", query)
        start = _decode_offset_token(query.continuation_token, signature, "page")
        items, next_start = await self._fetch_trending(query.media_type, start, query.limit)
        continuation = (
            _encode_offset_token(signature, "page", next_start)
            if next_start is not None
            else None
        )
        return CatalogPage(items=items, continuation_token=continuation)

    async def _categories_for_type(self, query: CatalogQuery) -> CatalogPage:
        if query.media_type is None:
            raise ValueError("分类查询缺少媒体类型")
        signature = _query_signature(f"categories:{query.media_type.value}", query)
        start = _decode_offset_token(query.continuation_token, signature, "page")
        items, next_start = await self._fetch_categories(query, start, query.limit)
        continuation = (
            _encode_offset_token(signature, "page", next_start)
            if next_start is not None
            else None
        )
        return CatalogPage(items=items, continuation_token=continuation)

    async def _fetch_trending(
        self,
        media_type: MediaType,
        start: int,
        limit: int,
    ) -> tuple[tuple[CatalogItem, ...], int | None]:
        payload = _require_mapping(
            await self._request_movie(
                "/j/search_subjects",
                {
                    "type": "movie" if media_type is MediaType.MOVIE else "tv",
                    "tag": "热门",
                    "page_limit": limit,
                    "page_start": start,
                },
            ),
            "trending",
        )
        rows = _require_sequence(payload.get("subjects"), "trending.subjects")
        items = self._map_rows(rows, media_type)
        return items, start + len(rows) if len(rows) >= limit else None

    async def _fetch_categories(
        self,
        query: CatalogQuery,
        start: int,
        limit: int,
    ) -> tuple[tuple[CatalogItem, ...], int | None]:
        if query.media_type is None:
            raise ValueError("分类查询缺少媒体类型")
        tags = ["电影" if query.media_type is MediaType.MOVIE else "电视剧"]
        if query.genres:
            tags.append(query.genres[0])
        payload = _require_mapping(
            await self._request_movie(
                "/j/new_search_subjects",
                {
                    "sort": _douban_sort(query.sort),
                    "range": "0,10",
                    "tags": ",".join(tags),
                    "start": start,
                    "limit": limit,
                },
            ),
            "categories",
        )
        rows = _require_sequence(payload.get("data"), "categories.data")
        items = self._map_rows(rows, query.media_type)
        return items, start + len(rows) if len(rows) >= limit else None

    async def _mixed_page(self, operation: str, query: CatalogQuery) -> CatalogPage:
        signature = _query_signature(f"{operation}:mixed", query)
        state = _decode_mixed_token(query.continuation_token, signature, operation)
        movie_start = _non_negative_int(state.get("movie_start")) or 0
        series_start = _non_negative_int(state.get("series_start")) or 0
        movie_done = bool(state.get("movie_done", False))
        series_done = bool(state.get("series_done", False))
        next_type = state.get("next", "movie")
        if next_type not in {"movie", "series"}:
            raise ValueError("豆瓣混合 continuation_token 无效")

        if movie_done and not series_done:
            movie_limit, series_limit = 0, query.limit
        elif series_done and not movie_done:
            movie_limit, series_limit = query.limit, 0
        elif next_type == "movie":
            movie_limit = (query.limit + 1) // 2
            series_limit = query.limit - movie_limit
        else:
            series_limit = (query.limit + 1) // 2
            movie_limit = query.limit - series_limit

        async def fetch(media_type: MediaType, start: int, limit: int):
            if operation == "trending":
                return await self._fetch_trending(media_type, start, limit)
            return await self._fetch_categories(
                replace(query, media_type=media_type, continuation_token=None),
                start,
                limit,
            )

        labels: list[str] = []
        calls = []
        if movie_limit:
            labels.append("movie")
            calls.append(fetch(MediaType.MOVIE, movie_start, movie_limit))
        if series_limit:
            labels.append("series")
            calls.append(fetch(MediaType.SERIES, series_start, series_limit))
        results = await asyncio.gather(*calls)
        pages = dict(zip(labels, results, strict=True))
        movie_items, next_movie = pages.get("movie", ((), movie_start))
        series_items, next_series = pages.get("series", ((), series_start))
        if movie_limit:
            movie_done = next_movie is None
            movie_start = next_movie if next_movie is not None else movie_start + len(movie_items)
        if series_limit:
            series_done = next_series is None
            series_start = next_series if next_series is not None else series_start + len(series_items)

        items = tuple(item for pair in zip(movie_items, series_items) for item in pair)
        if len(movie_items) > len(series_items):
            items += movie_items[len(series_items) :]
        elif len(series_items) > len(movie_items):
            items += series_items[len(movie_items) :]
        continuation = None
        if not movie_done or not series_done:
            following = (
                "series"
                if not series_done and next_type == "movie"
                else "movie"
                if not movie_done
                else "series"
            )
            continuation = _encode_token(
                {
                    "v": 1,
                    "kind": "mixed",
                    "op": operation,
                    "q": signature,
                    "movie_start": movie_start,
                    "series_start": series_start,
                    "movie_done": movie_done,
                    "series_done": series_done,
                    "next": following,
                }
            )
        return CatalogPage(items=items[: query.limit], continuation_token=continuation)

    def _map_rows(
        self,
        rows: Sequence[Any],
        media_type: MediaType,
    ) -> tuple[CatalogItem, ...]:
        items: list[CatalogItem] = []
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            try:
                items.append(self._map_summary(raw, media_type, suggestion=False))
            except (TypeError, ValueError, DoubanProviderError) as exc:
                self._logger.warning("豆瓣目录跳过无法映射的目录项：%s", type(exc).__name__)
        return tuple(items)

    def _map_summary(
        self,
        raw: Mapping[str, Any],
        media_type: MediaType,
        *,
        suggestion: bool,
    ) -> CatalogItem:
        external_id = _required_identifier(raw.get("id"))
        title = _optional_text(raw.get("title"))
        if title is None:
            raise DoubanProviderError("豆瓣目录项缺少标题")
        year = _parse_year(raw.get("year")) if suggestion else None
        poster_url = _https_url(raw.get("img" if suggestion else "cover"))
        original_title = _optional_text(raw.get("sub_title")) if suggestion else None
        rating = _rating(raw.get("rate"))
        return CatalogItem(
            external_id=external_id,
            external_id_provider="douban.subject",
            external_ids={"douban.subject": external_id},
            title=title,
            original_title=original_title,
            media_type=media_type,
            year=year,
            poster_url=poster_url,
            image_urls=(poster_url,) if poster_url else (),
            rating=rating,
        )

    def _map_detail(
        self,
        raw: Mapping[str, Any],
        media_type: MediaType,
    ) -> CatalogItem:
        external_id = _required_identifier(raw.get("id"))
        title = _optional_text(raw.get("title"))
        if title is None:
            raise DoubanProviderError("豆瓣详情缺少标题")
        release_date = _detail_release_date(raw)
        year = _parse_year(raw.get("year")) or (release_date.year if release_date else None)
        rating_payload = raw.get("rating")
        rating_map = rating_payload if isinstance(rating_payload, Mapping) else {}
        pic = raw.get("pic")
        pic_map = pic if isinstance(pic, Mapping) else {}
        poster_url = _https_url(pic_map.get("large")) or _https_url(
            pic_map.get("normal")
        ) or _https_url(raw.get("cover_url"))
        cover = raw.get("cover")
        alternate_cover_url = None
        if isinstance(cover, Mapping):
            image = cover.get("image")
            if isinstance(image, Mapping):
                large = image.get("large")
                if isinstance(large, Mapping):
                    alternate_cover_url = _https_url(large.get("url"))
        images = tuple(
            dict.fromkeys(url for url in (poster_url, alternate_cover_url) if url)
        )
        return CatalogItem(
            external_id=external_id,
            external_id_provider="douban.subject",
            external_ids={"douban.subject": external_id},
            title=title,
            original_title=_optional_text(raw.get("original_title")),
            media_type=media_type,
            year=year,
            release_date=release_date,
            poster_url=poster_url,
            image_urls=images,
            overview=_optional_text(raw.get("intro")),
            genres=_text_tuple(raw.get("genres")),
            regions=_text_tuple(raw.get("countries")),
            rating=_rating(rating_map.get("value")),
            vote_count=_non_negative_int(rating_map.get("count")),
        )

    async def _request_movie(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
    ) -> Any:
        return await self._request(MOVIE_BASE_URL, path, params)

    async def _request_mobile(self, path: str) -> Any:
        return await self._request(MOBILE_BASE_URL, path, None)

    async def _request(
        self,
        base_url: str,
        path: str,
        params: Mapping[str, object] | None,
    ) -> Any:
        query = urlencode(
            [(key, str(value)) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{base_url}{path}{'?' + query if query else ''}"
        referer = "https://m.douban.com/" if base_url == MOBILE_BASE_URL else "https://movie.douban.com/"
        try:
            return await self._http.get_json(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": referer,
                    "User-Agent": _USER_AGENT,
                },
            )
        except Exception as exc:
            status = getattr(exc, "code", None)
            if status in {401, 403}:
                message = "豆瓣拒绝了当前公开目录请求"
            elif status == 404:
                message = "豆瓣请求的媒体不存在"
            elif status == 429:
                message = "豆瓣请求受到限流"
            else:
                message = f"豆瓣请求失败（{type(exc).__name__}）"
            raise DoubanProviderError(message) from exc

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise DoubanProviderError("豆瓣 Provider 尚未初始化")

    def _validate_operation_query(
        self,
        operation: CatalogOperation,
        query: CatalogQuery,
    ) -> None:
        capabilities = self.describe_capabilities()
        requested: list[CatalogFilter] = []
        if query.media_type is not None:
            requested.append(CatalogFilter.MEDIA_TYPE)
        if query.genres:
            requested.append(CatalogFilter.GENRE)
        if query.regions:
            requested.append(CatalogFilter.REGION)
        if query.year_from is not None or query.year_to is not None:
            requested.append(CatalogFilter.YEAR)
        unsupported = [
            item.value
            for item in requested
            if item not in capabilities.filters_for(operation)
        ]
        if unsupported:
            raise ValueError(
                f"豆瓣 {operation.value} 不支持筛选：{'、'.join(unsupported)}"
            )
        if query.sort is not None and query.sort not in capabilities.sorts_for(operation):
            raise ValueError(f"豆瓣 {operation.value} 不支持排序：{query.sort.value}")


async def activate(context: Any) -> DoubanCatalogProvider:
    """Manifest v2 入口。"""

    factory = context.require("core.http.v1")
    client = factory.create(context.plugin_id)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if callable(close):
        context.register_cleanup(close)
    provider = DoubanCatalogProvider(client, logger=context.logger)
    await provider.initialize()
    return provider


def _media_type(value: object) -> MediaType | None:
    if value == "movie":
        return MediaType.MOVIE
    if value in {"tv", "series"}:
        return MediaType.SERIES
    return None


def _matches_search(
    raw: Mapping[str, Any],
    media_type: MediaType,
    query: CatalogQuery,
) -> bool:
    if query.media_type is not None and media_type is not query.media_type:
        return False
    year = _parse_year(raw.get("year"))
    if query.year_from is not None and (year is None or year < query.year_from):
        return False
    if query.year_to is not None and (year is None or year > query.year_to):
        return False
    return True


def _douban_sort(sort: CatalogSort | None) -> str:
    if sort is CatalogSort.RATING:
        return "R"
    if sort is CatalogSort.RELEASE_DATE:
        return "U"
    return "T"


def _query_signature(operation: str, query: CatalogQuery) -> str:
    payload = {
        "operation": operation,
        "keyword": query.keyword,
        "media_type": query.media_type.value if query.media_type else None,
        "genres": query.genres,
        "regions": query.regions,
        "year_from": query.year_from,
        "year_to": query.year_to,
        "sort": query.sort.value if query.sort else None,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _encode_offset_token(signature: str, kind: str, offset: int) -> str:
    return _encode_token({"v": 1, "kind": kind, "q": signature, "offset": offset})


def _decode_offset_token(token: str | None, signature: str, kind: str) -> int:
    if token is None:
        return 0
    state = _decode_token(token)
    offset = _non_negative_int(state.get("offset"))
    if state.get("v") != 1 or state.get("kind") != kind or state.get("q") != signature:
        raise ValueError("豆瓣 continuation_token 与当前查询不匹配")
    if offset is None:
        raise ValueError("豆瓣 continuation_token 无效")
    return offset


def _decode_mixed_token(
    token: str | None,
    signature: str,
    operation: str,
) -> dict[str, object]:
    if token is None:
        return {}
    state = _decode_token(token)
    if (
        state.get("v") != 1
        or state.get("kind") != "mixed"
        or state.get("op") != operation
        or state.get("q") != signature
    ):
        raise ValueError("豆瓣 continuation_token 与当前混合查询不匹配")
    return state


def _encode_token(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(token: str) -> dict[str, object]:
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("豆瓣 continuation_token 无效") from exc
    if not isinstance(payload, dict):
        raise ValueError("豆瓣 continuation_token 无效")
    return payload


def _require_mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DoubanProviderError(f"豆瓣 {location} 响应必须是对象")
    return value


def _require_sequence(value: object, location: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DoubanProviderError(f"豆瓣 {location} 响应必须是数组")
    return value


def _required_identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DoubanProviderError("豆瓣目录项缺少有效 ID")
    result = str(value).strip()
    if not result:
        raise DoubanProviderError("豆瓣目录项缺少有效 ID")
    return result


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _https_url(value: object) -> str | None:
    text = _optional_text(value)
    return text if text and text.startswith("https://") else None


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(text for item in value if (text := _optional_text(item))))


def _parse_year(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    text = _optional_text(value)
    if text is None:
        return None
    match = _YEAR_PATTERN.search(text)
    return int(match.group(1)) if match else None


def _detail_release_date(raw: Mapping[str, Any]) -> date | None:
    values: list[object] = [raw.get("release_date")]
    pubdate = raw.get("pubdate")
    if isinstance(pubdate, Sequence) and not isinstance(pubdate, (str, bytes)):
        values.extend(pubdate)
    for value in values:
        text = _optional_text(value)
        if text is None:
            continue
        match = _YEAR_PATTERN.search(text)
        if match and match.group(2) and match.group(3):
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def _rating(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 10 else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
