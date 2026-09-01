"""TMDb 媒体目录 Provider 实现。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
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


API_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/"
_MAX_REQUESTS_PER_CALL = 8


class JsonHttpClient(Protocol):
    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any: ...


class TmdbProviderError(RuntimeError):
    """TMDb 调用或响应无法满足公共合同。"""


class TmdbCatalogProvider:
    """把 TMDb v3 响应映射为 Sundarr 公共目录合同。"""

    id = "tmdb-catalog"

    def __init__(
        self,
        http_client: JsonHttpClient,
        *,
        api_read_access_token: str,
        language: str = "zh-CN",
        include_adult: bool = False,
        image_size: str = "w500",
        logger: logging.Logger | None = None,
    ) -> None:
        token = api_read_access_token.strip()
        if not token:
            raise ValueError("TMDb API Read Access Token 不能为空")
        normalized_language = language.strip()
        if not normalized_language:
            raise ValueError("TMDb 目录语言不能为空")
        self._http = http_client
        self._token = token
        self._language = normalized_language
        self._include_adult = include_adult
        self._image_size = image_size
        self._logger = logger or logging.getLogger("sundarr.plugin.tmdb-catalog")
        self._image_base_url = DEFAULT_IMAGE_BASE_URL
        self._movie_genres: dict[str, str] = {}
        self._series_genres: dict[str, str] = {}
        self._region_options: tuple[CatalogFilterOption, ...] = ()
        self._initialized = False

    async def initialize(self) -> None:
        """读取运行时元数据，使能力描述不依赖硬编码平台枚举。"""

        configuration, movie_genres, series_genres, countries = await asyncio.gather(
            self._request("/configuration"),
            self._request("/genre/movie/list", {"language": self._language}),
            self._request("/genre/tv/list", {"language": self._language}),
            self._request("/configuration/countries", {"language": self._language}),
        )
        config = _require_mapping(configuration, "configuration")
        images = _require_mapping(config.get("images"), "configuration.images")
        secure_base_url = images.get("secure_base_url")
        if isinstance(secure_base_url, str) and secure_base_url.startswith("https://"):
            self._image_base_url = secure_base_url
        supported_sizes = {
            str(item)
            for key in ("poster_sizes", "backdrop_sizes")
            for item in _require_sequence(images.get(key), f"configuration.images.{key}")
        }
        if self._image_size not in supported_sizes:
            raise TmdbProviderError(f"TMDb 当前不支持图片尺寸：{self._image_size}")

        self._movie_genres = _parse_genres(movie_genres, "movie")
        self._series_genres = _parse_genres(series_genres, "tv")
        self._region_options = _parse_countries(countries)
        self._initialized = True

    def describe_capabilities(self) -> CatalogCapabilities:
        self._require_initialized()
        merged_genres = dict(self._movie_genres)
        merged_genres.update(self._series_genres)
        genre_options = tuple(
            CatalogFilterOption(value=value, label=label)
            for value, label in sorted(merged_genres.items(), key=lambda item: item[1])
        )
        return CatalogCapabilities(
            operations=frozenset(CatalogOperation),
            media_types=frozenset(MediaType),
            filters=frozenset(CatalogFilter),
            sorts=frozenset(CatalogSort),
            operation_filters={
                CatalogOperation.SEARCH: frozenset(
                    {CatalogFilter.MEDIA_TYPE, CatalogFilter.GENRE, CatalogFilter.YEAR}
                ),
                CatalogOperation.TRENDING: frozenset({CatalogFilter.MEDIA_TYPE}),
                CatalogOperation.CATEGORIES: frozenset(CatalogFilter),
            },
            operation_sorts={
                CatalogOperation.SEARCH: frozenset(),
                CatalogOperation.TRENDING: frozenset(),
                CatalogOperation.CATEGORIES: frozenset(CatalogSort),
            },
            attribution=CatalogAttribution(
                provider_name="TMDB",
                homepage_url="https://www.themoviedb.org",
                notice=(
                    "This product uses the TMDB API but is not endorsed or "
                    "certified by TMDB."
                ),
                logo_url=(
                    "https://www.themoviedb.org/assets/2/v4/logos/v2/"
                    "blue_long_2-9665a76b1ae401a510ec1e0ca40ddcb3b0cfe45f1d51b77a"
                    "308fea0845885648.svg"
                ),
            ),
            identity_namespaces=frozenset({"tmdb.movie", "tmdb.tv"}),
            filter_options={
                CatalogFilter.GENRE: genre_options,
                CatalogFilter.REGION: self._region_options,
            },
        )

    def health_check(self) -> PluginHealthResult:
        if not self._initialized:
            return PluginHealthResult(ok=False, message="TMDb 运行时元数据尚未初始化")
        return PluginHealthResult(
            ok=True,
            message="TMDb 目录插件可用",
            details={
                "language": self._language,
                "movie_genres": str(len(self._movie_genres)),
                "series_genres": str(len(self._series_genres)),
                "regions": str(len(self._region_options)),
            },
        )

    async def search(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.SEARCH, query)
        keyword = (query.keyword or "").strip()
        if not keyword:
            raise ValueError("TMDb 搜索必须提供关键词")
        if query.media_type is MediaType.MOVIE:
            endpoint, fixed_type = "/search/movie", MediaType.MOVIE
        elif query.media_type is MediaType.SERIES:
            endpoint, fixed_type = "/search/tv", MediaType.SERIES
        else:
            endpoint, fixed_type = "/search/multi", None
        return await self._collect_page(
            operation="search",
            endpoint=endpoint,
            params={
                "query": keyword,
                "language": self._language,
                "include_adult": str(self._include_adult).lower(),
            },
            query=query,
            fixed_media_type=fixed_type,
        )

    async def trending(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.TRENDING, query)
        if query.media_type is MediaType.MOVIE:
            endpoint, fixed_type = "/trending/movie/day", MediaType.MOVIE
        elif query.media_type is MediaType.SERIES:
            endpoint, fixed_type = "/trending/tv/day", MediaType.SERIES
        else:
            endpoint, fixed_type = "/trending/all/day", None
        return await self._collect_page(
            operation="trending",
            endpoint=endpoint,
            params={"language": self._language},
            query=query,
            fixed_media_type=fixed_type,
            supports_remote_pages=False,
        )

    async def categories(self, query: CatalogQuery) -> CatalogPage:
        self._require_initialized()
        self._validate_operation_query(CatalogOperation.CATEGORIES, query)
        if query.media_type is None:
            return await self._mixed_categories(query)
        return await self._categories_for_type(query)

    async def get_detail(
        self,
        external_id: str,
        media_type: MediaType | None = None,
    ) -> CatalogItem:
        self._require_initialized()
        normalized_id = external_id.strip()
        if not normalized_id:
            raise ValueError("TMDb external_id 不能为空")
        if media_type is None:
            raise ValueError("TMDb 详情查询必须明确 movie 或 series，避免同号 ID 冲突")
        if media_type is MediaType.MOVIE:
            kind = "movie"
        elif media_type is MediaType.SERIES:
            kind = "tv"
        else:
            raise ValueError(f"TMDb 不支持媒体类型：{media_type}")
        payload = await self._request(
            f"/{kind}/{normalized_id}",
            {"language": self._language, "append_to_response": "external_ids"},
        )
        return self._map_item(
            _require_mapping(payload, f"{kind} detail"),
            media_type,
            detail=True,
        )

    async def _categories_for_type(self, query: CatalogQuery) -> CatalogPage:
        if query.media_type is None:
            raise ValueError("分类查询缺少媒体类型")
        is_movie = query.media_type is MediaType.MOVIE
        params: dict[str, object] = {
            "language": self._language,
            "include_adult": str(self._include_adult).lower(),
            "sort_by": _tmdb_sort(query.sort, query.media_type),
        }
        if query.genres:
            params["with_genres"] = ",".join(query.genres)
        if query.regions:
            params["with_origin_country"] = query.regions[0]
        if query.year_from is not None:
            key = "primary_release_date.gte" if is_movie else "first_air_date.gte"
            params[key] = f"{query.year_from:04d}-01-01"
        if query.year_to is not None:
            key = "primary_release_date.lte" if is_movie else "first_air_date.lte"
            params[key] = f"{query.year_to:04d}-12-31"
        if query.sort is CatalogSort.RATING:
            params["vote_count.gte"] = 1
        return await self._collect_page(
            operation=f"categories:{query.media_type.value}",
            endpoint="/discover/movie" if is_movie else "/discover/tv",
            params=params,
            query=query,
            fixed_media_type=query.media_type,
            apply_local_filters=False,
        )

    async def _mixed_categories(self, query: CatalogQuery) -> CatalogPage:
        signature = _query_signature("categories:mixed", query, {})
        state = _decode_mixed_token(query.continuation_token, signature)
        movie_token = state.get("movie")
        series_token = state.get("series")
        movie_done = bool(state.get("movie_done", False))
        series_done = bool(state.get("series_done", False))
        next_type = state.get("next", "movie")
        if next_type not in {"movie", "series"}:
            raise ValueError("TMDb 混合分类 continuation_token 无效")

        if movie_done and not series_done:
            movie_limit = 0
            series_limit = query.limit
        elif series_done and not movie_done:
            movie_limit = query.limit
            series_limit = 0
        elif next_type == "movie":
            movie_limit = (query.limit + 1) // 2
            series_limit = query.limit - movie_limit
        else:
            series_limit = (query.limit + 1) // 2
            movie_limit = query.limit - series_limit

        movie_page: CatalogPage | None = None
        series_page: CatalogPage | None = None
        calls = []
        labels = []
        if movie_limit:
            labels.append("movie")
            calls.append(
                self._categories_for_type(
                    replace(
                        query,
                        media_type=MediaType.MOVIE,
                        limit=movie_limit,
                        continuation_token=movie_token if isinstance(movie_token, str) else None,
                    )
                )
            )
        if series_limit:
            labels.append("series")
            calls.append(
                self._categories_for_type(
                    replace(
                        query,
                        media_type=MediaType.SERIES,
                        limit=series_limit,
                        continuation_token=series_token if isinstance(series_token, str) else None,
                    )
                )
            )
        results = await asyncio.gather(*calls)
        for label, page in zip(labels, results, strict=True):
            if label == "movie":
                movie_page = page
            else:
                series_page = page

        movie_items = movie_page.items if movie_page else ()
        series_items = series_page.items if series_page else ()
        items = tuple(item for pair in zip(movie_items, series_items) for item in pair)
        if len(movie_items) > len(series_items):
            items += movie_items[len(series_items):]
        elif len(series_items) > len(movie_items):
            items += series_items[len(movie_items):]

        next_movie = movie_page.continuation_token if movie_page else movie_token
        next_series = series_page.continuation_token if series_page else series_token
        movie_done = movie_done or (movie_page is not None and next_movie is None)
        series_done = series_done or (series_page is not None and next_series is None)
        continuation = None
        if not movie_done or not series_done:
            if not movie_done and not series_done:
                following_type = "series" if next_type == "movie" else "movie"
            elif not movie_done:
                following_type = "movie"
            else:
                following_type = "series"
            continuation = _encode_token(
                {
                    "v": 1,
                    "kind": "mixed",
                    "q": signature,
                    "movie": next_movie,
                    "series": next_series,
                    "movie_done": movie_done,
                    "series_done": series_done,
                    "next": following_type,
                }
            )
        return CatalogPage(items=items[: query.limit], continuation_token=continuation)

    async def _collect_page(
        self,
        *,
        operation: str,
        endpoint: str,
        params: Mapping[str, object],
        query: CatalogQuery,
        fixed_media_type: MediaType | None,
        apply_local_filters: bool = True,
        supports_remote_pages: bool = True,
    ) -> CatalogPage:
        signature = _query_signature(operation, query, params)
        state = _decode_page_token(query.continuation_token, signature)
        page_number = state["page"]
        offset = state["offset"]
        items: list[CatalogItem] = []
        total_pages = page_number
        requests = 0

        while len(items) < query.limit and requests < _MAX_REQUESTS_PER_CALL:
            request_params = dict(params)
            if supports_remote_pages:
                request_params["page"] = page_number
            payload = _require_mapping(
                await self._request(endpoint, request_params),
                operation,
            )
            requests += 1
            raw_results = _require_sequence(payload.get("results"), f"{operation}.results")
            total_pages = _positive_int(payload.get("total_pages"), default=page_number)
            ordered_results = list(raw_results)

            consumed_index = len(raw_results)
            for position, raw in enumerate(ordered_results):
                if position < offset:
                    continue
                consumed_index = position + 1
                if not isinstance(raw, Mapping):
                    self._logger.warning("TMDb %s 跳过非对象目录项", operation)
                    continue
                if not self._include_adult and raw.get("adult") is True:
                    continue
                media_type = _resolve_media_type(raw, fixed_media_type)
                if media_type is None:
                    continue
                if apply_local_filters and not _matches_query(raw, media_type, query):
                    continue
                try:
                    items.append(self._map_item(raw, media_type, detail=False))
                except (TypeError, ValueError, TmdbProviderError) as exc:
                    self._logger.warning(
                        "TMDb %s 跳过无法映射的目录项：%s",
                        operation,
                        type(exc).__name__,
                    )
                    continue
                if len(items) >= query.limit:
                    break

            if len(items) >= query.limit and consumed_index < len(raw_results):
                continuation = _encode_page_token(signature, page_number, consumed_index)
                return CatalogPage(items=tuple(items), continuation_token=continuation)
            if not supports_remote_pages:
                return CatalogPage(items=tuple(items), continuation_token=None)
            page_number += 1
            offset = 0
            if page_number > total_pages:
                return CatalogPage(items=tuple(items), continuation_token=None)

        continuation = _encode_page_token(signature, page_number, offset)
        return CatalogPage(items=tuple(items), continuation_token=continuation)

    def _map_item(
        self,
        raw: Mapping[str, Any],
        media_type: MediaType,
        *,
        detail: bool,
    ) -> CatalogItem:
        external_id = _required_identifier(raw.get("id"))
        is_movie = media_type is MediaType.MOVIE
        title = _optional_text(raw.get("title" if is_movie else "name"))
        original_title = _optional_text(
            raw.get("original_title" if is_movie else "original_name")
        )
        canonical_title = title or original_title
        if canonical_title is None:
            raise TmdbProviderError("TMDb 目录项缺少标题")
        release_date = _parse_date(raw.get("release_date" if is_movie else "first_air_date"))
        namespace = "tmdb.movie" if is_movie else "tmdb.tv"
        external_ids = {namespace: external_id}
        imdb_id = _optional_text(raw.get("imdb_id"))
        nested_external_ids = raw.get("external_ids")
        if imdb_id is None and isinstance(nested_external_ids, Mapping):
            imdb_id = _optional_text(nested_external_ids.get("imdb_id"))
        if imdb_id:
            external_ids["imdb"] = imdb_id

        poster_url = self._image_url(raw.get("poster_path"))
        backdrop_url = self._image_url(raw.get("backdrop_path"))
        genres = self._genres(raw, media_type, detail=detail)
        regions = _regions(raw)
        rating = _rating(raw.get("vote_average"))
        vote_count = _non_negative_int(raw.get("vote_count"))
        image_urls = tuple(dict.fromkeys(url for url in (poster_url, backdrop_url) if url))
        return CatalogItem(
            external_id=external_id,
            external_id_provider=namespace,
            external_ids=external_ids,
            title=canonical_title,
            original_title=original_title,
            media_type=media_type,
            year=release_date.year if release_date else None,
            release_date=release_date,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
            image_urls=image_urls,
            overview=_optional_text(raw.get("overview")),
            genres=genres,
            regions=regions,
            rating=rating,
            vote_count=vote_count,
        )

    def _genres(
        self,
        raw: Mapping[str, Any],
        media_type: MediaType,
        *,
        detail: bool,
    ) -> tuple[str, ...]:
        if detail:
            raw_genres = raw.get("genres")
            if isinstance(raw_genres, Sequence) and not isinstance(raw_genres, (str, bytes)):
                names = [
                    _optional_text(item.get("name"))
                    for item in raw_genres
                    if isinstance(item, Mapping)
                ]
                return tuple(name for name in names if name)
        lookup = self._movie_genres if media_type is MediaType.MOVIE else self._series_genres
        genre_ids = raw.get("genre_ids")
        if not isinstance(genre_ids, Sequence) or isinstance(genre_ids, (str, bytes)):
            return ()
        return tuple(
            lookup[str(item)]
            for item in genre_ids
            if str(item) in lookup
        )

    def _image_url(self, value: object) -> str | None:
        path = _optional_text(value)
        if path is None or not path.startswith("/"):
            return None
        return f"{self._image_base_url.rstrip('/')}/{self._image_size}{path}"

    async def _request(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
    ) -> Any:
        query = urlencode(
            [(key, str(value)) for key, value in (params or {}).items() if value is not None]
        )
        url = f"{API_BASE_URL}{path}{'?' + query if query else ''}"
        try:
            return await self._http.get_json(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception as exc:
            status = getattr(exc, "code", None)
            if status in {401, 403}:
                message = "TMDb 认证失败，请检查 API Read Access Token"
            elif status == 404:
                message = "TMDb 请求的媒体不存在"
            elif status == 429:
                message = "TMDb 请求受到限流"
            else:
                message = f"TMDb 请求失败（{type(exc).__name__}）"
            raise TmdbProviderError(message) from exc

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise TmdbProviderError("TMDb Provider 尚未初始化")

    def _validate_operation_query(
        self,
        operation: CatalogOperation,
        query: CatalogQuery,
    ) -> None:
        capabilities = self.describe_capabilities()
        requested_filters: list[CatalogFilter] = []
        if query.media_type is not None:
            requested_filters.append(CatalogFilter.MEDIA_TYPE)
        if query.genres:
            requested_filters.append(CatalogFilter.GENRE)
        if query.regions:
            requested_filters.append(CatalogFilter.REGION)
        if query.year_from is not None or query.year_to is not None:
            requested_filters.append(CatalogFilter.YEAR)
        unsupported = [
            item.value
            for item in requested_filters
            if item not in capabilities.filters_for(operation)
        ]
        if unsupported:
            raise ValueError(
                f"TMDb {operation.value} 不支持筛选：{'、'.join(unsupported)}"
            )
        if (
            query.sort is not None
            and query.sort not in capabilities.sorts_for(operation)
        ):
            raise ValueError(
                f"TMDb {operation.value} 不支持排序：{query.sort.value}"
            )


async def activate(context: Any) -> TmdbCatalogProvider:
    """Manifest v2 入口。"""

    factory = context.require("core.http.v1")
    client = factory.create(context.plugin_id)
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if callable(close):
        context.register_cleanup(close)
    provider = TmdbCatalogProvider(
        client,
        api_read_access_token=context.plugin_config["api_read_access_token"],
        language=context.plugin_config.get("language", "zh-CN"),
        include_adult=context.plugin_config.get("include_adult", False),
        image_size=context.plugin_config.get("image_size", "w500"),
        logger=context.logger,
    )
    await provider.initialize()
    return provider


def _parse_genres(payload: object, kind: str) -> dict[str, str]:
    mapping = _require_mapping(payload, f"genre.{kind}")
    rows = _require_sequence(mapping.get("genres"), f"genre.{kind}.genres")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identifier = _required_identifier(row.get("id"))
        name = _optional_text(row.get("name"))
        if name:
            result[identifier] = name
    if not result:
        raise TmdbProviderError(f"TMDb {kind} 题材列表为空")
    return result


def _parse_countries(payload: object) -> tuple[CatalogFilterOption, ...]:
    rows = _require_sequence(payload, "configuration.countries")
    options: list[CatalogFilterOption] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = _optional_text(row.get("iso_3166_1"))
        label = _optional_text(row.get("native_name")) or _optional_text(row.get("english_name"))
        if code and label:
            options.append(CatalogFilterOption(value=code, label=label))
    if not options:
        raise TmdbProviderError("TMDb 地区列表为空")
    return tuple(sorted(options, key=lambda item: item.label))


def _tmdb_sort(sort: CatalogSort | None, media_type: MediaType) -> str:
    if sort is CatalogSort.RATING:
        return "vote_average.desc"
    if sort is CatalogSort.RELEASE_DATE:
        return "primary_release_date.desc" if media_type is MediaType.MOVIE else "first_air_date.desc"
    return "popularity.desc"


def _matches_query(raw: Mapping[str, Any], media_type: MediaType, query: CatalogQuery) -> bool:
    if query.media_type is not None and media_type is not query.media_type:
        return False
    if query.genres:
        genre_ids = raw.get("genre_ids")
        if not isinstance(genre_ids, Sequence) or isinstance(genre_ids, (str, bytes)):
            return False
        available_genres = {str(item) for item in genre_ids}
        if not set(query.genres).issubset(available_genres):
            return False
    if query.regions:
        if query.regions[0] not in set(_regions(raw)):
            return False
    release_value = raw.get("release_date" if media_type is MediaType.MOVIE else "first_air_date")
    release_date = _parse_date(release_value)
    if query.year_from is not None and (release_date is None or release_date.year < query.year_from):
        return False
    if query.year_to is not None and (release_date is None or release_date.year > query.year_to):
        return False
    return True


def _resolve_media_type(
    raw: Mapping[str, Any],
    fixed_media_type: MediaType | None,
) -> MediaType | None:
    if fixed_media_type is not None:
        return fixed_media_type
    value = raw.get("media_type")
    if value == "movie":
        return MediaType.MOVIE
    if value == "tv":
        return MediaType.SERIES
    return None


def _regions(raw: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    origin_country = raw.get("origin_country")
    if isinstance(origin_country, Sequence) and not isinstance(origin_country, (str, bytes)):
        values.extend(item.strip() for item in origin_country if isinstance(item, str) and item.strip())
    production_countries = raw.get("production_countries")
    if isinstance(production_countries, Sequence) and not isinstance(production_countries, (str, bytes)):
        for item in production_countries:
            if isinstance(item, Mapping):
                code = _optional_text(item.get("iso_3166_1"))
                if code:
                    values.append(code)
    return tuple(dict.fromkeys(values))


def _query_signature(
    operation: str,
    query: CatalogQuery,
    params: Mapping[str, object],
) -> str:
    payload = {
        "operation": operation,
        "keyword": query.keyword,
        "media_type": query.media_type.value if query.media_type else None,
        "genres": query.genres,
        "regions": query.regions,
        "year_from": query.year_from,
        "year_to": query.year_to,
        "sort": query.sort.value if query.sort else None,
        "params": sorted((key, str(value)) for key, value in params.items()),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def _encode_page_token(signature: str, page: int, offset: int) -> str:
    return _encode_token({"v": 1, "kind": "page", "q": signature, "page": page, "offset": offset})


def _decode_page_token(token: str | None, signature: str) -> dict[str, int]:
    if token is None:
        return {"page": 1, "offset": 0}
    state = _decode_token(token)
    if state.get("v") != 1 or state.get("kind") != "page" or state.get("q") != signature:
        raise ValueError("TMDb continuation_token 与当前查询不匹配")
    page = _positive_int(state.get("page"), default=0)
    offset = _non_negative_int(state.get("offset"))
    if page < 1 or offset is None:
        raise ValueError("TMDb continuation_token 无效")
    return {"page": page, "offset": offset}


def _decode_mixed_token(token: str | None, signature: str) -> dict[str, object]:
    if token is None:
        return {}
    state = _decode_token(token)
    if state.get("v") != 1 or state.get("kind") != "mixed" or state.get("q") != signature:
        raise ValueError("TMDb continuation_token 与当前混合分类查询不匹配")
    return state


def _encode_token(payload: Mapping[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_token(token: str) -> dict[str, object]:
    try:
        padding = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("TMDb continuation_token 无效") from exc
    if not isinstance(payload, dict):
        raise ValueError("TMDb continuation_token 无效")
    return payload


def _require_mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TmdbProviderError(f"TMDb {location} 响应必须是对象")
    return value


def _require_sequence(value: object, location: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TmdbProviderError(f"TMDb {location} 响应必须是数组")
    return value


def _required_identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TmdbProviderError("TMDb 目录项缺少有效 ID")
    result = str(value).strip()
    if not result:
        raise TmdbProviderError("TMDb 目录项缺少有效 ID")
    return result


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _parse_date(value: object) -> date | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _rating(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if 0 <= result <= 10 else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value
