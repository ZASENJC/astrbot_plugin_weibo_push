import asyncio
import base64
import binascii
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, StarTools
from bs4 import BeautifulSoup

DEFAULT_CHECK_INTERVAL_MINUTES = 10
DEFAULT_REQUEST_INTERVAL_SECONDS = 5
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MESSAGE_TEMPLATE = "🔔 {name} 发微博啦！\n\n{weibo}\n\n话题: {topics}\n链接: {link}"
STARTUP_DELAY_SECONDS = 10
CACHE_RETENTION_SECONDS = 6 * 60 * 60
DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS = 2
DEFAULT_RETRY_MAX_DELAY_SECONDS = 120
DEFAULT_RETRY_JITTER_SECONDS = 1
DEFAULT_RETRY_QUEUE_MAX_SIZE = 200

WEIBO_API_BASE = "https://m.weibo.cn/api/container/getIndex"
WEIBO_MOBILE_BASE = "https://m.weibo.cn"
WEIBO_WEB_BASE = "https://weibo.com"
WEIBO_CONFIG_API = "https://m.weibo.cn/api/config"
WEIBO_STATUS_API = f"{WEIBO_MOBILE_BASE}/statuses/show"

SUPPORTED_CONFIG_ROOT_KEYS = {
    "auth_settings",
    "monitoring_settings",
    "content_settings",
    "screenshot_settings",
    "runtime_settings",
}

UID_IN_URL_PATTERN = re.compile(r"(?:weibo|m\.weibo)\.(?:com|cn)/(?:u|profile)/(\d+)")
TOPIC_PATTERN = re.compile(r"#([^#]{1,80})#")
SCHEME_UID_PATTERN = re.compile(r"(?:uid=|/u/)(\d+)")
MBLOG_UID_PATTERN = re.compile(r"/(\d+)/")
WEIBO_URL_PATTERN = re.compile(r"https?://[^\s<>'\"）】]+")
WEIBO_STATUS_ID_IN_HTML_PATTERN = re.compile(r'"(?:status_id|mid)"\s*:\s*"?(\d{8,})"?')
WEIBO_STATUS_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
WEIBO_SHORT_HOSTS = {"t.cn"}
RESERVED_PATH_SEGMENTS = {"p", "u", "profile", "n", "status", "detail", "api"}
FOLLOWING_CONTAINER_TEMPLATES = (
    "231051_-_followers_-_{uid}",
    "231051_-_follow_-_{uid}",
    "231093_-_selffollowed",
)
DEFAULT_PASSIVE_LINK_MAX_PER_MESSAGE = 1


@dataclass(frozen=True)
class MonitorRule:
    uid: str
    targets: Tuple[str, ...]
    source: str
    is_auto_following: bool = False


@dataclass
class WeiboPost:
    text: str
    link: str
    username: str
    image_urls: List[str] = field(default_factory=list)
    video_url: Optional[str] = None
    topics: List[str] = field(default_factory=list)


@dataclass
class RetryTaskItem:
    target: str
    chain: MessageChain
    attempt: int
    delay_seconds: float
    reason: str = ""


class SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class WeiboHttpClient:
    def __init__(self, client: httpx.AsyncClient, cookie_getter: Callable[[], str]):
        self._client = client
        self._cookie_getter = cookie_getter

    def get_headers(self, uid: str = "") -> Dict[str, str]:
        cookie = self._cookie_getter()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{WEIBO_MOBILE_BASE}/u/{uid}" if uid else f"{WEIBO_MOBILE_BASE}/",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def request_json(self, url: str, *, uid: str = "") -> Optional[Dict[str, Any]]:
        try:
            response = await self._client.get(url, headers=self.get_headers(uid))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.error(f"WeiboMonitor: 请求失败 {url}: {err}")
            return None

        if response.status_code != 200:
            logger.warning(f"WeiboMonitor: 请求状态异常 {response.status_code} -> {url}")
            return None

        try:
            data = response.json()
        except Exception as err:
            logger.error(f"WeiboMonitor: 解析 JSON 失败 {url}: {err}")
            return None

        if not isinstance(data, dict):
            logger.warning(f"WeiboMonitor: 响应 JSON 结构异常，预期对象实际为 {type(data).__name__} -> {url}")
            return None

        return data


class WeiboPostParser:
    def extract_non_top_mblogs(self, cards: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
        mblogs: List[Dict[str, Any]] = []
        username = "未知用户"

        for card in cards:
            if not isinstance(card, dict):
                continue
            if card.get("card_type") != 9:
                continue

            mblog = card.get("mblog")
            if not isinstance(mblog, dict):
                continue

            is_top = any(
                [
                    mblog.get("isTop"),
                    mblog.get("is_top"),
                    card.get("is_top"),
                    mblog.get("top"),
                    ((mblog.get("title") or {}).get("text") == "置顶"),
                ]
            )
            if is_top:
                continue

            mblogs.append(mblog)
            if username == "未知用户":
                username = (mblog.get("user") or {}).get("screen_name", "未知用户")

        return mblogs, username

    def extract_uid_from_mblog(self, mblog: Dict[str, Any]) -> Optional[str]:
        user = mblog.get("user") or {}
        uid = user.get("id")
        if uid:
            return str(uid)

        scheme = str(mblog.get("scheme", ""))
        match = MBLOG_UID_PATTERN.search(scheme)
        if match:
            return match.group(1)

        return None

    def extract_username(self, mblog: Dict[str, Any], default: str = "未知用户") -> str:
        user = mblog.get("user") or {}
        name = str(user.get("screen_name") or user.get("remark") or "").strip()
        if name:
            return name
        return default

    def extract_topics(self, mblog: Dict[str, Any]) -> List[str]:
        topics: Set[str] = set()
        candidates = [mblog]
        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            candidates.append(retweeted)

        for candidate in candidates:
            topic_struct = candidate.get("topic_struct") or []
            for item in topic_struct:
                if not isinstance(item, dict):
                    continue
                name = (
                    item.get("topic_title")
                    or item.get("topic_name")
                    or item.get("title")
                    or ""
                )
                cleaned = str(name).strip().strip("#")
                if cleaned:
                    topics.add(cleaned)

            text = self.clean_text(candidate.get("text", ""))
            for match in TOPIC_PATTERN.findall(text):
                cleaned = match.strip().strip("#")
                if cleaned:
                    topics.add(cleaned)

        return sorted(topics)

    def extract_media(self, mblog: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
        image_urls: List[str] = []
        seen_images: Set[str] = set()
        video_url: Optional[str] = None

        candidates = [mblog]
        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            candidates.append(retweeted)

        for candidate in candidates:
            pics = candidate.get("pics") or []
            for pic in pics:
                if not isinstance(pic, dict):
                    continue
                image = (pic.get("large") or {}).get("url")
                if image and image not in seen_images:
                    image_urls.append(image)
                    seen_images.add(image)

            if video_url:
                continue

            page_info = candidate.get("page_info") or {}
            if page_info.get("type") == "video":
                media = page_info.get("media_info") or {}
                video_url = media.get("stream_url_hd") or media.get("stream_url")

        return image_urls, video_url

    def extract_post_text(self, mblog: Dict[str, Any]) -> str:
        candidates = [mblog]
        retweeted = mblog.get("retweeted_status")
        if isinstance(retweeted, dict):
            candidates.append(retweeted)

        text_candidates: List[Any] = []
        for candidate in candidates:
            long_text = candidate.get("longText") or {}
            page_info = candidate.get("page_info") or {}
            text_candidates.extend(
                [
                    candidate.get("text"),
                    candidate.get("raw_text"),
                    long_text.get("longTextContent"),
                    long_text.get("content"),
                    page_info.get("content1"),
                    page_info.get("content2"),
                    page_info.get("title"),
                ]
            )

        for raw_text in text_candidates:
            cleaned = self.clean_text(raw_text)
            if cleaned:
                return cleaned

        return ""

    def build_post(self, mblog: Dict[str, Any], fallback_uid: str = "", default_username: str = "未知用户") -> Optional[WeiboPost]:
        post_id = str(mblog.get("id") or "").strip()
        bid = str(mblog.get("bid") or "").strip()
        token = bid or post_id
        if not token:
            return None

        post_uid = self.extract_uid_from_mblog(mblog) or fallback_uid
        if not post_uid:
            return None

        if bid:
            link = f"{WEIBO_WEB_BASE}/{post_uid}/{bid}"
        else:
            link = f"{WEIBO_MOBILE_BASE}/detail/{token}"

        image_urls, video_url = self.extract_media(mblog)
        return WeiboPost(
            text=self.extract_post_text(mblog),
            link=link,
            username=self.extract_username(mblog, default=default_username),
            image_urls=image_urls,
            video_url=video_url,
            topics=self.extract_topics(mblog),
        )

    def clean_text(self, text: Any) -> str:
        if text is None:
            return ""

        if not isinstance(text, str):
            text = str(text)

        if not text:
            return ""

        try:
            text = re.sub(r"<a[^>]*>全文</a>", "", text)
            soup = BeautifulSoup(text, "html.parser")

            for img in soup.find_all("img"):
                img.replace_with(img.get("alt", ""))

            for anchor in soup.find_all("a"):
                anchor.replace_with(anchor.get_text())

            for br in soup.find_all("br"):
                br.replace_with("\n")

            cleaned = soup.get_text()
            cleaned = re.sub(r"\n\s+", "\n", cleaned)
            cleaned = re.sub(r"\s+\n", "\n", cleaned)
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            return cleaned.strip()
        except Exception as err:
            logger.error(f"WeiboMonitor: 清理微博文本失败: {err}")
            return text


class MediaCacheManager:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.active_files: Set[Path] = set()

    def create_cache_path(self, suffix: str, prefix: str) -> Path:
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        filename = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex}{suffix}"
        return self.cache_dir / filename

    def mark_active(self, path: Optional[str]) -> None:
        if path:
            self.active_files.add(Path(path))

    def mark_inactive(self, path: Optional[str]) -> None:
        if path:
            self.active_files.discard(Path(path))

    async def release_cached_files(self, paths: List[str]) -> None:
        if not paths:
            return
        await asyncio.sleep(1)
        for path in {str(Path(item)) for item in paths if item}:
            self.mark_inactive(path)

    def cleanup(self) -> None:
        expire_before = time.time() - CACHE_RETENTION_SECONDS

        try:
            for cache_file in self.cache_dir.iterdir():
                try:
                    if cache_file in self.active_files:
                        continue
                    if cache_file.is_file() and cache_file.stat().st_mtime < expire_before:
                        cache_file.unlink()
                except Exception as err:
                    logger.debug(f"WeiboMonitor: 清理缓存文件失败 {cache_file}: {err}")
        except FileNotFoundError:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as err:
            logger.warning(f"WeiboMonitor: 清理缓存目录失败: {err}")


class RetryManager:
    def __init__(
        self,
        runtime_config_getter: Callable[[], Dict[str, Any]],
        safe_int: Callable[[Any, int, Optional[int], Optional[int]], int],
        queue_max_size: int,
    ):
        self._runtime_config_getter = runtime_config_getter
        self._safe_int = safe_int
        self.queue: asyncio.Queue[RetryTaskItem] = asyncio.Queue(maxsize=queue_max_size)

    def retry_enabled(self) -> bool:
        return bool(self._runtime_config_getter().get("retry_enabled", True))

    def retry_max_attempts(self) -> int:
        return self._safe_int(
            self._runtime_config_getter().get("retry_max_attempts", DEFAULT_RETRY_MAX_ATTEMPTS),
            DEFAULT_RETRY_MAX_ATTEMPTS,
            min_value=1,
            max_value=10,
        )

    def retry_base_delay(self) -> int:
        return self._safe_int(
            self._runtime_config_getter().get("retry_base_delay", DEFAULT_RETRY_BASE_DELAY_SECONDS),
            DEFAULT_RETRY_BASE_DELAY_SECONDS,
            min_value=1,
            max_value=60,
        )

    def retry_max_delay(self) -> int:
        return self._safe_int(
            self._runtime_config_getter().get("retry_max_delay", DEFAULT_RETRY_MAX_DELAY_SECONDS),
            DEFAULT_RETRY_MAX_DELAY_SECONDS,
            min_value=1,
            max_value=3600,
        )

    def retry_jitter(self) -> int:
        return self._safe_int(
            self._runtime_config_getter().get("retry_jitter", DEFAULT_RETRY_JITTER_SECONDS),
            DEFAULT_RETRY_JITTER_SECONDS,
            min_value=0,
            max_value=30,
        )

    def calculate_retry_delay(self, attempt: int) -> float:
        exponent = max(0, attempt - 2)
        delay = min(self.retry_max_delay(), self.retry_base_delay() * (2 ** exponent))
        jitter = random.uniform(0, self.retry_jitter()) if self.retry_jitter() > 0 else 0.0
        return float(delay + jitter)

    async def enqueue_retry(
        self,
        target: str,
        chain: MessageChain,
        attempt: int,
        delay_seconds: float,
        reason: str = "",
    ) -> None:
        if not self.retry_enabled():
            return
        if attempt > self.retry_max_attempts():
            return
        if self.queue.full():
            logger.warning(f"WeiboMonitor: 重试队列已满，丢弃消息 target={target}, reason={reason}")
            return
        try:
            self.queue.put_nowait(
                RetryTaskItem(
                    target=target,
                    chain=chain,
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    reason=reason,
                )
            )
        except asyncio.QueueFull:
            logger.warning(f"WeiboMonitor: 重试队列已满，丢弃消息 target={target}, reason={reason}")
            return

        logger.info(
            f"WeiboMonitor: 已加入重试队列 target={target}, attempt={attempt}, delay={delay_seconds:.1f}s, reason={reason}"
        )

    async def retry_worker(
        self,
        is_running: Callable[[], bool],
        send_to_target_once: Callable[..., Awaitable[bool]],
    ) -> None:
        while is_running():
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                if item.delay_seconds > 0:
                    await asyncio.sleep(item.delay_seconds)

                sent = await send_to_target_once(
                    item.target,
                    item.chain,
                    reason=item.reason or "retry",
                    attempt=item.attempt,
                    is_retry=True,
                )
                if sent:
                    continue

                if item.attempt >= self.retry_max_attempts():
                    logger.error(
                        f"WeiboMonitor: 消息重试耗尽 target={item.target}, max_attempts={self.retry_max_attempts()}, reason={item.reason}"
                    )
                    continue

                next_attempt = item.attempt + 1
                next_delay = self.calculate_retry_delay(next_attempt)
                await self.enqueue_retry(
                    target=item.target,
                    chain=item.chain,
                    attempt=next_attempt,
                    delay_seconds=next_delay,
                    reason=item.reason,
                )
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"WeiboMonitor: 重试队列处理异常: {err}")
            finally:
                self.queue.task_done()


class MonitorRuleResolver:
    def __init__(
        self,
        monitor_config_getter: Callable[[], Dict[str, Any]],
        state_get: Callable[[str, Any], Any],
        state_update: Callable[[Dict[str, Any]], None],
        parse_multi_value: Callable[[Any], List[str]],
        safe_int: Callable[[Any, int, Optional[int], Optional[int]], int],
        request_json: Callable[..., Awaitable[Optional[Dict[str, Any]]]],
        send_chain_to_targets: Callable[[List[str], MessageChain, str], Awaitable[Tuple[int, int]]],
        client: httpx.AsyncClient,
        get_headers: Callable[[str], Dict[str, str]],
        uid_cache: Dict[str, str],
    ):
        self._monitor_config_getter = monitor_config_getter
        self._state_get = state_get
        self._state_update = state_update
        self._parse_multi_value = parse_multi_value
        self._safe_int = safe_int
        self._request_json = request_json
        self._send_chain_to_targets = send_chain_to_targets
        self._client = client
        self._get_headers = get_headers
        self._uid_cache = uid_cache

    def _get_auto_following_config(self) -> Dict[str, Any]:
        monitor_config = self._monitor_config_getter()
        nested = monitor_config.get("auto_following", {})
        nested = nested if isinstance(nested, dict) else {}

        return {
            "enabled": nested.get("enabled", monitor_config.get("auto_following_enabled", False)),
            "source": nested.get("source", monitor_config.get("auto_following_source", "")),
            "targets": nested.get("targets", monitor_config.get("auto_following_targets", "")),
            "refresh_interval": nested.get("refresh_interval", monitor_config.get("auto_following_refresh_interval", 30)),
            "max_pages": nested.get("max_pages", monitor_config.get("auto_following_max_pages", 3)),
            "remove_unfollowed": nested.get("remove_unfollowed", monitor_config.get("auto_following_remove_unfollowed", False)),
            "notify_changes": nested.get("notify_changes", monitor_config.get("auto_following_notify_changes", True)),
        }

    async def resolve_monitor_rules(self, force_following_refresh: bool) -> List[MonitorRule]:
        manual_rules = await self.resolve_manual_rules()
        auto_rules = await self.resolve_auto_following_rules(force_following_refresh=force_following_refresh)

        merged_targets: Dict[str, Set[str]] = {}
        merged_source: Dict[str, str] = {}

        for rule in [*manual_rules, *auto_rules]:
            if not rule.targets:
                continue
            merged_targets.setdefault(rule.uid, set()).update(rule.targets)
            merged_source.setdefault(rule.uid, rule.source)

        merged_rules: List[MonitorRule] = []
        auto_rule_uids = {rule.uid for rule in auto_rules}
        for uid, targets in merged_targets.items():
            merged_rules.append(
                MonitorRule(
                    uid=uid,
                    targets=tuple(sorted(targets)),
                    source=merged_source.get(uid, uid),
                    is_auto_following=uid in auto_rule_uids,
                )
            )

        return merged_rules

    async def resolve_manual_rules(self) -> List[MonitorRule]:
        rules_raw = self._monitor_config_getter().get("subscription_rules", [])
        if not isinstance(rules_raw, list):
            return []

        rules: List[MonitorRule] = []
        for item in rules_raw:
            if not isinstance(item, dict):
                continue

            sources = self._parse_multi_value(item.get("source", ""))
            if not sources:
                continue

            targets = tuple(self._parse_multi_value(item.get("allowed_targets", "")))
            for source in sources:
                uid = await self.parse_uid(source)
                if not uid:
                    logger.warning(f"WeiboMonitor: 订阅规则无法解析 UID -> {source}")
                    continue

                rules.append(MonitorRule(uid=uid, targets=targets, source=source))

        return rules

    async def resolve_auto_following_rules(self, force_following_refresh: bool) -> List[MonitorRule]:
        auto_following_config = self._get_auto_following_config()
        if not auto_following_config.get("enabled", False):
            return []

        targets = self._parse_multi_value(auto_following_config.get("targets", ""))
        if not targets:
            logger.warning("WeiboMonitor: 已开启关注列表监控，但未配置 auto_following.targets")
            return []

        source = str(auto_following_config.get("source", "")).strip()
        source_uid = await self.resolve_auto_following_source_uid(source)
        if not source_uid:
            logger.warning("WeiboMonitor: 无法确定关注列表来源 UID，已跳过自动关注监控")
            return []

        refresh_interval = self._safe_int(
            auto_following_config.get("refresh_interval", 30),
            30,
            min_value=5,
            max_value=24 * 60,
        )
        max_pages = self._safe_int(
            auto_following_config.get("max_pages", 3),
            3,
            min_value=1,
            max_value=20,
        )
        remove_unfollowed = bool(auto_following_config.get("remove_unfollowed", False))
        notify = bool(auto_following_config.get("notify_changes", True))

        snapshot_key = f"auto_following_snapshot_{source_uid}"
        history_key = f"auto_following_history_{source_uid}"
        names_key = f"auto_following_name_map_{source_uid}"
        effective_key = f"auto_following_effective_{source_uid}"
        refreshed_at_key = f"auto_following_refreshed_at_{source_uid}"

        now = int(time.time())
        last_refreshed = self._safe_int(self._state_get(refreshed_at_key, 0), 0, min_value=0)
        use_cache_only = (
            not force_following_refresh
            and now - last_refreshed < refresh_interval * 60
            and isinstance(self._state_get(effective_key, None), list)
        )

        if use_cache_only:
            cached_effective = self._state_get(effective_key, [])
            return [
                MonitorRule(uid=str(uid), targets=tuple(targets), source=f"following:{source_uid}", is_auto_following=True)
                for uid in cached_effective
                if str(uid).isdigit()
            ]

        users = await self.fetch_following_users(source_uid, max_pages)
        if not users:
            cached_effective = self._state_get(effective_key, [])
            if isinstance(cached_effective, list) and cached_effective:
                logger.warning(
                    f"WeiboMonitor: 关注列表抓取为空，回退缓存监控列表 UID={source_uid}, 数量={len(cached_effective)}"
                )
                return [
                    MonitorRule(
                        uid=str(uid),
                        targets=tuple(targets),
                        source=f"following:{source_uid}",
                        is_auto_following=True,
                    )
                    for uid in cached_effective
                    if str(uid).isdigit()
                ]

        current_ids = {item["uid"] for item in users if item.get("uid")}

        previous_snapshot = set(self._state_get(snapshot_key, []))
        history_ids = set(self._state_get(history_key, []))

        if remove_unfollowed:
            effective_ids = set(current_ids)
        else:
            history_ids.update(current_ids)
            effective_ids = history_ids

        name_map = self._state_get(names_key, {})
        if not isinstance(name_map, dict):
            name_map = {}
        for user in users:
            uid = user.get("uid")
            name = user.get("name")
            if uid and name:
                name_map[uid] = name

        added = sorted(current_ids - previous_snapshot)
        removed = sorted(previous_snapshot - current_ids)

        self._state_update(
            {
                snapshot_key: sorted(current_ids),
                history_key: sorted(history_ids),
                names_key: name_map,
                effective_key: sorted(effective_ids),
                refreshed_at_key: now,
            }
        )

        if notify and (added or removed):
            await self.notify_following_changes(source_uid, added, removed, name_map, targets, len(effective_ids))

        return [
            MonitorRule(uid=uid, targets=tuple(targets), source=f"following:{source_uid}", is_auto_following=True)
            for uid in sorted(effective_ids)
        ]

    async def resolve_auto_following_source_uid(self, source: str) -> Optional[str]:
        if source:
            return await self.parse_uid(source)
        return await self.fetch_login_uid()

    async def fetch_login_uid(self) -> Optional[str]:
        payload = await self._request_json(WEIBO_CONFIG_API)
        if not payload:
            return None

        data = payload.get("data") or {}
        if not data.get("login"):
            return None

        user = data.get("user") or {}
        uid = user.get("id") or data.get("uid")
        if uid:
            return str(uid)
        return None

    async def fetch_following_users(self, source_uid: str, max_pages: int) -> List[Dict[str, str]]:
        for template in FOLLOWING_CONTAINER_TEMPLATES:
            users = await self.fetch_following_users_by_template(source_uid, template, max_pages)
            if users:
                logger.debug(
                    f"WeiboMonitor: 关注列表抓取成功 UID={source_uid}, 模板={template}, 数量={len(users)}"
                )
                return users

        logger.warning(f"WeiboMonitor: 未从任何容器模板获取到关注列表，UID={source_uid}")
        return []

    async def fetch_following_users_by_template(self, source_uid: str, template: str, max_pages: int) -> List[Dict[str, str]]:
        users: Dict[str, str] = {}

        for page in range(1, max_pages + 1):
            container_id = template.format(uid=source_uid)
            url = f"{WEIBO_API_BASE}?containerid={container_id}&page={page}"
            payload = await self._request_json(url, uid=source_uid)
            if not payload or payload.get("ok") != 1:
                if page == 1:
                    return []
                break

            data = payload.get("data")
            if not isinstance(data, dict):
                if page == 1:
                    return []
                break

            cards = data.get("cards")
            if not isinstance(cards, list):
                if page == 1:
                    return []
                break

            page_users = self.extract_users_from_cards(cards)
            if not page_users:
                if page == 1:
                    return []
                break

            for uid, name in page_users.items():
                users[uid] = name

            if len(page_users) < 5:
                break

        return [{"uid": uid, "name": name} for uid, name in users.items()]

    def extract_users_from_cards(self, cards: List[Dict[str, Any]]) -> Dict[str, str]:
        users: Dict[str, str] = {}

        def collect_from_item(item: Dict[str, Any]) -> None:
            user_obj = item.get("user") if isinstance(item.get("user"), dict) else {}
            uid = user_obj.get("id") or item.get("user_id")
            name = user_obj.get("screen_name") or item.get("desc1") or item.get("title_sub") or ""

            if not uid:
                scheme = str(item.get("scheme", ""))
                match = SCHEME_UID_PATTERN.search(scheme)
                if match:
                    uid = match.group(1)

            if uid:
                uid_text = str(uid)
                users[uid_text] = str(name or uid_text)

        for card in cards:
            if not isinstance(card, dict):
                continue

            card_group = card.get("card_group")
            if isinstance(card_group, list):
                for item in card_group:
                    if isinstance(item, dict):
                        collect_from_item(item)
                continue

            collect_from_item(card)

        return users

    async def notify_following_changes(
        self,
        source_uid: str,
        added: List[str],
        removed: List[str],
        name_map: Dict[str, str],
        targets: List[str],
        total_monitored: int,
    ) -> None:
        if not targets:
            return

        def format_item(uid: str) -> str:
            name = name_map.get(uid, uid)
            return f"{name}({uid})"

        lines = [f"👀 关注列表发生更新（来源 UID: {source_uid}）"]
        if added:
            lines.append("新增关注: " + "，".join(format_item(uid) for uid in added[:10]))
            if len(added) > 10:
                lines.append(f"新增关注其余 {len(added) - 10} 个账号未展开")
        if removed:
            lines.append("取消关注: " + "，".join(format_item(uid) for uid in removed[:10]))
            if len(removed) > 10:
                lines.append(f"取消关注其余 {len(removed) - 10} 个账号未展开")
        lines.append(f"当前自动纳入监控账号数: {total_monitored}")

        chain = MessageChain()
        chain.chain.append(Plain("\n".join(lines)))
        await self._send_chain_to_targets(targets, chain, "following_change_notify")

    async def parse_uid(self, source: str) -> Optional[str]:
        text = str(source or "").strip()
        if not text:
            return None

        cached = self._uid_cache.get(text)
        if cached is not None:
            return cached or None

        if text.isdigit():
            self._uid_cache[text] = text
            return text

        match = UID_IN_URL_PATTERN.search(text)
        if match:
            uid = match.group(1)
            self._uid_cache[text] = uid
            return uid

        nickname = self.extract_nickname_from_input(text)
        if nickname:
            uid = await self.resolve_uid_from_nickname(nickname)
            self._uid_cache[text] = uid or ""
            return uid

        self._uid_cache[text] = ""
        return None

    def extract_nickname_from_input(self, text: str) -> Optional[str]:
        if text.startswith("http://") or text.startswith("https://"):
            parsed = urlparse(text)
            segments = [segment for segment in parsed.path.split("/") if segment]
            if not segments:
                return None

            if segments[0] == "n" and len(segments) > 1:
                return unquote(segments[1])

            if len(segments) == 1 and segments[0] not in RESERVED_PATH_SEGMENTS:
                segment = unquote(segments[0])
                if not segment.isdigit():
                    return segment
            return None

        if "/" in text:
            return None
        if text.startswith("@"):
            text = text[1:]
        cleaned = text.strip()
        if not cleaned or cleaned.isdigit():
            return None
        return cleaned

    async def resolve_uid_from_nickname(self, nickname: str) -> Optional[str]:
        url = f"{WEIBO_MOBILE_BASE}/n/{nickname}"
        try:
            response = await self._client.get(url, headers=self._get_headers(""))
        except Exception as err:
            logger.error(f"WeiboMonitor: 通过昵称解析 UID 失败 {nickname}: {err}")
            return None

        final_url = str(response.url)
        match = UID_IN_URL_PATTERN.search(final_url)
        if match:
            return match.group(1)

        match = re.search(r"/u/(\d+)", final_url)
        if match:
            return match.group(1)

        return None


class WeiboDeliveryService:
    def __init__(
        self,
        context: Context,
        client: httpx.AsyncClient,
        get_headers: Callable[[str], Dict[str, str]],
        content_config_getter: Callable[[], Dict[str, Any]],
        screenshot_config_getter: Callable[[], Dict[str, Any]],
        safe_int: Callable[[Any, int, Optional[int], Optional[int]], int],
        retry_manager: RetryManager,
        cache_manager: MediaCacheManager,
        auth_config_getter: Callable[[], Dict[str, Any]],
    ):
        self._context = context
        self._client = client
        self._get_headers = get_headers
        self._content_config_getter = content_config_getter
        self._screenshot_config_getter = screenshot_config_getter
        self._safe_int = safe_int
        self._retry_manager = retry_manager
        self._cache_manager = cache_manager
        self._auth_config_getter = auth_config_getter
        self._playwright: Any = None
        self._browser: Any = None
        self._browser_lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._browser_lock:
            browser = self._browser
            playwright = self._playwright
            self._browser = None
            self._playwright = None

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    async def _ensure_browser(self) -> Optional[Any]:
        if self._browser is not None and self._playwright is not None:
            return self._browser

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.debug("WeiboMonitor: playwright 未安装，跳过截图")
            return None

        async with self._browser_lock:
            if self._browser is not None and self._playwright is not None:
                return self._browser

            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(args=["--no-sandbox", "--disable-setuid-sandbox"])
            except Exception:
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                self._playwright = None
                self._browser = None
                raise

            return self._browser

    async def send_new_posts(self, posts: List[WeiboPost], targets: List[str], template: str) -> Dict[str, int]:
        content_config = self._content_config_getter()
        screenshot_config = self._screenshot_config_getter()

        send_images = bool(content_config.get("send_images", True))
        send_videos = bool(content_config.get("send_videos", True))
        send_screenshot = bool(screenshot_config.get("weibo_screenshot", True))
        merge_forward_send = bool(content_config.get("merge_forward_send", False))

        summary = {
            "posts_total": len(posts),
            "posts_sent": 0,
            "target_success": 0,
            "target_failure": 0,
        }

        unique_targets = list(dict.fromkeys(targets))
        if merge_forward_send:
            return await self.send_new_posts_merged_forward(
                posts=posts,
                targets=unique_targets,
                template=template,
                send_images=send_images,
                send_videos=send_videos,
                send_screenshot=send_screenshot,
                summary=summary,
            )

        return await self.send_new_posts_segmented(
            posts=posts,
            targets=unique_targets,
            template=template,
            send_images=send_images,
            send_videos=send_videos,
            send_screenshot=send_screenshot,
            summary=summary,
        )

    async def send_new_posts_segmented(
        self,
        posts: List[WeiboPost],
        targets: List[str],
        template: str,
        send_images: bool,
        send_videos: bool,
        send_screenshot: bool,
        summary: Dict[str, int],
    ) -> Dict[str, int]:
        for post in posts:
            rendered = self.render_post_text(template, post)

            screenshot_path = await self.take_screenshot(post.link) if send_screenshot else None
            cached_paths: List[str] = []
            if screenshot_path:
                self._cache_manager.mark_active(screenshot_path)
                cached_paths.append(screenshot_path)

            try:
                text_chain = self.build_text_chain(rendered, screenshot_path)
                media_chain, media_cache_paths = await self.build_media_chain(
                    post,
                    rendered,
                    send_images,
                    send_videos,
                )
                cached_paths.extend(media_cache_paths)

                text_success, text_failure = await self.send_chain_to_targets(
                    targets,
                    text_chain,
                    reason="segmented_text",
                )
                summary["target_success"] += text_success
                summary["target_failure"] += text_failure

                media_success, media_failure = (0, 0)
                if media_chain:
                    media_success, media_failure = await self.send_chain_to_targets(
                        targets,
                        media_chain,
                        reason="segmented_media",
                    )
                    summary["target_success"] += media_success
                    summary["target_failure"] += media_failure

                if text_success > 0:
                    summary["posts_sent"] += 1
            finally:
                await self._cache_manager.release_cached_files(cached_paths)

        return summary

    async def send_new_posts_merged_forward(
        self,
        posts: List[WeiboPost],
        targets: List[str],
        template: str,
        send_images: bool,
        send_videos: bool,
        send_screenshot: bool,
        summary: Dict[str, int],
    ) -> Dict[str, int]:
        cached_paths: List[str] = []
        nodes: List[Node] = []

        try:
            for post in posts:
                rendered = self.render_post_text(template, post)

                text_components: List[Any] = [Plain(rendered)]
                screenshot_path = await self.take_screenshot(post.link) if send_screenshot else None
                if screenshot_path:
                    self._cache_manager.mark_active(screenshot_path)
                    cached_paths.append(screenshot_path)
                    try:
                        text_components.append(Image.fromFileSystem(screenshot_path))
                    except Exception as err:
                        logger.warning(f"WeiboMonitor: 合并转发附加截图失败: {err}")

                if post.video_url and send_videos:
                    nodes.append(
                        Node(
                            uin="0",
                            name=post.username,
                            content=text_components,
                        )
                    )
                    nodes.append(
                        Node(
                            uin="0",
                            name=post.username,
                            content=[Video.fromURL(post.video_url)],
                        )
                    )
                elif post.image_urls and send_images:
                    media_components = list(text_components)
                    for image_url in post.image_urls:
                        image_path = await self.download_to_cache(image_url, ".jpg", "img")
                        if not image_path:
                            continue
                        cached_paths.append(image_path)
                        try:
                            media_components.append(Image.fromFileSystem(image_path))
                        except Exception as err:
                            logger.warning(f"WeiboMonitor: 合并转发附加图片失败 {image_path}: {err}")
                    nodes.append(
                        Node(
                            uin="0",
                            name=post.username,
                            content=media_components,
                        )
                    )
                else:
                    nodes.append(
                        Node(
                            uin="0",
                            name=post.username,
                            content=text_components,
                        )
                    )

            if not nodes:
                return summary

            merged_chain = MessageChain()
            merged_chain.chain.append(Nodes(nodes=nodes))

            success, failure = await self.send_chain_to_targets(
                targets,
                merged_chain,
                reason="merged_forward",
            )
            summary["target_success"] += success
            summary["target_failure"] += failure
            if success > 0:
                summary["posts_sent"] = len(posts)
            return summary
        finally:
            await self._cache_manager.release_cached_files(cached_paths)

    def render_post_text(self, template: str, post: WeiboPost) -> str:
        topics = "、".join(f"#{topic}#" for topic in post.topics) if post.topics else "无"
        values = SafeFormatDict(
            name=post.username,
            weibo=post.text or "（无正文）",
            link=post.link,
            topics=topics,
        )
        return template.format_map(values)

    def build_text_chain(self, content: str, screenshot_path: Optional[str]) -> MessageChain:
        chain = MessageChain()
        chain.chain.append(Plain(content))
        if screenshot_path:
            try:
                chain.chain.append(Image.fromFileSystem(screenshot_path))
            except Exception as err:
                logger.warning(f"WeiboMonitor: 附加截图失败: {err}")
        return chain

    async def build_media_chain(
        self,
        post: WeiboPost,
        rendered_text: str,
        send_images: bool,
        send_videos: bool,
    ) -> Tuple[Optional[MessageChain], List[str]]:
        nodes: List[Node] = []
        cached_paths: List[str] = []

        if post.video_url and send_videos:
            nodes.append(
                Node(
                    uin="0",
                    name=post.username,
                    content=[Plain(rendered_text)],
                )
            )
            nodes.append(
                Node(
                    uin="0",
                    name=post.username,
                    content=[Video.fromURL(post.video_url)],
                )
            )
        elif post.image_urls and send_images:
            images = []
            for image_url in post.image_urls:
                image_path = await self.download_to_cache(image_url, ".jpg", "img")
                if not image_path:
                    continue
                cached_paths.append(image_path)
                try:
                    images.append(Image.fromFileSystem(image_path))
                except Exception as err:
                    logger.warning(f"WeiboMonitor: 图片组件生成失败 {image_path}: {err}")

            if images:
                nodes.append(Node(uin="0", name=post.username, content=images))

        if not nodes:
            return None, cached_paths

        chain = MessageChain()
        chain.chain.append(Nodes(nodes=nodes))
        return chain, cached_paths

    async def send_to_target_once(
        self,
        target: str,
        chain: MessageChain,
        reason: str = "",
        attempt: int = 1,
        is_retry: bool = False,
    ) -> bool:
        try:
            await self._context.send_message(target, chain)
            if is_retry:
                logger.info(f"WeiboMonitor: 重试发送成功 target={target}, attempt={attempt}, reason={reason}")
            return True
        except Exception as err:
            stage = "重试" if is_retry else "首次"
            logger.error(f"WeiboMonitor: {stage}发送失败 target={target}, attempt={attempt}, reason={reason}, err={err}")
            return False

    async def send_chain_to_targets(self, targets: List[str], chain: MessageChain, reason: str = "") -> Tuple[int, int]:
        async def send_single(target: str) -> bool:
            sent = await self.send_to_target_once(target, chain, reason=reason, attempt=1, is_retry=False)
            if sent:
                return True

            if self._retry_manager.retry_enabled() and self._retry_manager.retry_max_attempts() > 1:
                await self._retry_manager.enqueue_retry(
                    target=target,
                    chain=chain,
                    attempt=2,
                    delay_seconds=self._retry_manager.calculate_retry_delay(2),
                    reason=reason,
                )
            return False

        if not targets:
            return 0, 0

        results = await asyncio.gather(*(send_single(target) for target in targets))
        success = sum(1 for ok in results if ok)
        failure = len(results) - success
        return success, failure

    async def download_to_cache(self, url: str, suffix: str, prefix: str) -> Optional[str]:
        cache_path = self._cache_manager.create_cache_path(suffix, prefix)
        try:
            response = await self._client.get(url, headers=self._get_headers(""), follow_redirects=True)
            if response.status_code != 200:
                return None
            cache_path.write_bytes(response.content)
            self._cache_manager.mark_active(str(cache_path))
            return str(cache_path)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.error(f"WeiboMonitor: 下载媒体失败 {url}: {err}")
            try:
                if cache_path.exists():
                    cache_path.unlink()
            except Exception:
                pass
            return None

    async def take_screenshot(self, url: str) -> Optional[str]:
        browser = await self._ensure_browser()
        if browser is None:
            return None

        screenshot_config = self._screenshot_config_getter()
        width = self._safe_int(screenshot_config.get("screenshot_width", 1280), 1280, min_value=320, max_value=4096)
        height = self._safe_int(screenshot_config.get("screenshot_height", 720), 720, min_value=240, max_value=4096)
        quality = self._safe_int(screenshot_config.get("screenshot_quality", 80), 80, min_value=1, max_value=100)
        wait_ms = self._safe_int(screenshot_config.get("screenshot_wait_time", 2000), 2000, min_value=0, max_value=30000)
        full_page = bool(screenshot_config.get("screenshot_full_page", False))

        image_type = str(screenshot_config.get("screenshot_format", "jpeg")).lower()
        if image_type not in {"jpeg", "png"}:
            image_type = "jpeg"

        screenshot_path = self._cache_manager.create_cache_path(f".{image_type}", "screenshot")

        page = None
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})

            cookie_str = self._auth_config_getter().get("weibo_cookie", "")
            if cookie_str:
                cookies = []
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if "=" not in part:
                        continue
                    name, _, value = part.partition("=")
                    cookies.append(
                        {
                            "name": name.strip(),
                            "value": value.strip(),
                            "domain": ".weibo.com",
                            "path": "/",
                        }
                    )
                if cookies:
                    await page.context.add_cookies(cookies)

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(wait_ms)

            shot_args: Dict[str, Any] = {
                "path": str(screenshot_path),
                "full_page": full_page,
                "type": image_type,
            }
            if image_type == "jpeg":
                shot_args["quality"] = quality

            await page.screenshot(**shot_args)
            return str(screenshot_path)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.error(f"WeiboMonitor: 截图失败 {url}: {err}")
            try:
                if screenshot_path.exists():
                    screenshot_path.unlink()
            except Exception:
                pass
            return None
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass


class Main(Star):
    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config = config or {}
        self.running = True

        self.monitor_task: Optional[asyncio.Task] = None
        self.retry_worker_task: Optional[asyncio.Task] = None

        self.session_initialized_uids: Set[str] = set()
        self.uid_cache: Dict[str, str] = {}

        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.data_file = self.data_dir / "monitor_data.json"
        self.cache_dir = self.data_dir / "media_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_manager = MediaCacheManager(self.cache_dir)

        self._migrate_legacy_data()
        self._state = self._load_state()
        self.cache_manager.cleanup()

        transport = httpx.AsyncHTTPTransport(retries=2)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_SECONDS,
            transport=transport,
            follow_redirects=True,
            limits=limits,
        )
        self.weibo_http = WeiboHttpClient(
            client=self.client,
            cookie_getter=lambda: str(self.auth_config.get("weibo_cookie", "")),
        )
        self.weibo_parser = WeiboPostParser()

        cookie = self.auth_config.get("weibo_cookie", "")
        if not cookie:
            logger.warning("WeiboMonitor: 未配置微博 Cookie，监控将保持空转。")

        retry_queue_max_size = self._safe_int(
            self.runtime_config.get("retry_queue_max_size", DEFAULT_RETRY_QUEUE_MAX_SIZE),
            DEFAULT_RETRY_QUEUE_MAX_SIZE,
            min_value=10,
            max_value=5000,
        )
        self.retry_manager = RetryManager(
            runtime_config_getter=lambda: self.runtime_config,
            safe_int=self._safe_int,
            queue_max_size=retry_queue_max_size,
        )
        self.retry_queue = self.retry_manager.queue

        self.delivery_service = WeiboDeliveryService(
            context=self.context,
            client=self.client,
            get_headers=self.weibo_http.get_headers,
            content_config_getter=lambda: self.content_config,
            screenshot_config_getter=lambda: self.screenshot_config,
            safe_int=self._safe_int,
            retry_manager=self.retry_manager,
            cache_manager=self.cache_manager,
            auth_config_getter=lambda: self.auth_config,
        )
        self.rule_resolver = MonitorRuleResolver(
            monitor_config_getter=lambda: self.monitor_config,
            state_get=self._state_get,
            state_update=self._state_update,
            parse_multi_value=self._parse_multi_value,
            safe_int=self._safe_int,
            request_json=self.weibo_http.request_json,
            send_chain_to_targets=self.delivery_service.send_chain_to_targets,
            client=self.client,
            get_headers=self.weibo_http.get_headers,
            uid_cache=self.uid_cache,
        )

        self.monitor_task = asyncio.create_task(self.run_monitor())
        self.retry_worker_task = asyncio.create_task(self._retry_worker())

    @property
    def auth_config(self) -> Dict[str, Any]:
        return self.config.get("auth_settings", {}) or {}

    @property
    def monitor_config(self) -> Dict[str, Any]:
        return self.config.get("monitoring_settings", {}) or {}

    @property
    def content_config(self) -> Dict[str, Any]:
        return self.config.get("content_settings", {}) or {}

    @property
    def screenshot_config(self) -> Dict[str, Any]:
        return self.config.get("screenshot_settings", {}) or {}

    @property
    def runtime_config(self) -> Dict[str, Any]:
        return self.config.get("runtime_settings", {}) or {}

    @property
    def passive_link_config(self) -> Dict[str, Any]:
        config = self.monitor_config.get("passive_link_recognition", {})
        config = config if isinstance(config, dict) else {}
        return {
            "enabled": config.get("enabled", True),
            "targets": config.get("targets", ""),
            "ignore_commands": config.get("ignore_commands", True),
            "max_links_per_message": config.get("max_links_per_message", DEFAULT_PASSIVE_LINK_MAX_PER_MESSAGE),
        }

    @property
    def message_template(self) -> str:
        template = self.content_config.get("message_format", DEFAULT_MESSAGE_TEMPLATE)
        return str(template).replace("\\n", "\n")

    def _migrate_legacy_data(self) -> None:
        if self.data_file.exists():
            return

        legacy_candidates = [
            Path("data") / "astrbot_plugin_weibo_monitor" / "monitor_data.json",
            Path("data") / "plugin_data" / "astrbot_plugin_weibo_monitor" / "monitor_data.json",
        ]

        for old_data_file in legacy_candidates:
            if not old_data_file.exists():
                continue
            try:
                import shutil

                shutil.copy2(old_data_file, self.data_file)
                logger.info(f"WeiboMonitor: 已迁移旧数据文件 -> {self.data_file}")
                return
            except Exception as err:
                logger.error(f"WeiboMonitor: 迁移旧数据失败: {err}")

    def _load_state(self) -> Dict[str, Any]:
        if not self.data_file.exists():
            return {}
        try:
            return json.loads(self.data_file.read_text(encoding="utf-8"))
        except Exception as err:
            logger.error(f"WeiboMonitor: 加载状态文件失败: {err}")
            try:
                backup_file = self.data_file.with_suffix(f".bak.{int(time.time())}")
                self.data_file.rename(backup_file)
                logger.warning(f"WeiboMonitor: 状态文件已损坏，已备份到 {backup_file}")
            except Exception as backup_err:
                logger.error(f"WeiboMonitor: 备份损坏文件失败: {backup_err}")
            return {}

    def _save_state(self) -> None:
        temp_file = self.data_file.with_suffix(".tmp")
        try:
            temp_file.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_file.replace(self.data_file)
        except Exception as err:
            logger.error(f"WeiboMonitor: 保存状态文件失败: {err}")
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass

    def _state_get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def _state_set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self._save_state()

    def _state_update(self, values: Dict[str, Any]) -> None:
        self._state.update(values)
        self._save_state()

    def _safe_int(self, value: Any, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
        try:
            num = int(value)
        except Exception:
            num = default

        if min_value is not None and num < min_value:
            num = min_value
        if max_value is not None and num > max_value:
            num = max_value
        return num

    async def _retry_worker(self) -> None:
        await self.retry_manager.retry_worker(
            is_running=lambda: self.running,
            send_to_target_once=self.delivery_service.send_to_target_once,
        )

    def _parse_multi_value(self, raw: Any) -> List[str]:
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, list):
            candidates = [str(item) for item in raw]
        else:
            return []

        values: List[str] = []
        for item in candidates:
            for part in item.replace("\n", ",").split(","):
                value = part.strip()
                if value:
                    values.append(value)

        # 去重并保持顺序
        return list(dict.fromkeys(values))

    def _parse_keyword_list(self, raw: Any) -> List[str]:
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw if str(item).strip()]
            return list(dict.fromkeys(values))
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

    def _pick_interval(self, base: int, jitter: int, minimum: int = 1) -> int:
        if jitter <= 0:
            return max(minimum, base)
        return max(minimum, random.randint(base - jitter, base + jitter))

    def _get_bot_owner_id(self) -> str:
        try:
            cfg = self.context.get_config()
            admins = cfg.get("admins_id", [])
        except Exception:
            admins = []

        if isinstance(admins, list) and admins:
            return str(admins[0])
        return ""

    def _is_bot_owner(self, event: AstrMessageEvent) -> bool:
        owner_id = self._get_bot_owner_id()
        if not owner_id:
            return False
        return str(event.get_sender_id()) == owner_id

    def _is_message_from_self(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id()) if hasattr(event, "get_sender_id") else ""
        self_id = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        if not sender_id or not self_id:
            return False
        return sender_id == self_id

    def _is_weibo_host(self, host: str) -> bool:
        normalized = str(host or "").lower().split(":")[0]
        if not normalized:
            return False
        return (
            normalized in WEIBO_SHORT_HOSTS
            or normalized == "weibo.com"
            or normalized.endswith(".weibo.com")
            or normalized == "weibo.cn"
            or normalized.endswith(".weibo.cn")
        )

    def _extract_weibo_urls(self, text: str) -> List[str]:
        urls: List[str] = []
        for raw_url in WEIBO_URL_PATTERN.findall(text):
            url = raw_url.rstrip(".,!?;:)]}>。，！？；：】）")
            host = urlparse(url).netloc
            if host and self._is_weibo_host(host):
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _extract_status_ref_from_url(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        parsed = urlparse(url)
        segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment.strip()]
        query = parse_qs(parsed.query)

        uid: Optional[str] = None
        token: Optional[str] = None

        for key in ("id", "bid", "mid", "status_id"):
            values = query.get(key) or []
            if not values:
                continue
            candidate = str(values[0]).strip()
            if candidate:
                token = candidate
                break

        if segments:
            if segments[0] in {"detail", "status"} and len(segments) >= 2:
                token = token or segments[1]
            elif segments[0] in {"u", "profile"} and len(segments) >= 3 and segments[1].isdigit():
                uid = segments[1]
                token = token or segments[2]
            elif len(segments) >= 2 and segments[0] not in RESERVED_PATH_SEGMENTS:
                if segments[0].isdigit():
                    uid = segments[0]
                token = token or segments[1]

        token_text = str(token or "").strip()
        if token_text and WEIBO_STATUS_TOKEN_PATTERN.fullmatch(token_text):
            return uid, token_text
        return uid, None

    def _extract_status_id_from_html(self, html: str) -> Optional[str]:
        match = WEIBO_STATUS_ID_IN_HTML_PATTERN.search(html)
        if match:
            return match.group(1)
        return None

    def _extract_mblog_from_status_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        candidates: List[Any] = [payload, payload.get("data"), payload.get("status"), payload.get("card")]

        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("status"), data.get("mblog"), data.get("card")])
            data_card = data.get("card")
            if isinstance(data_card, dict):
                candidates.append(data_card.get("mblog"))

        card = payload.get("card")
        if isinstance(card, dict):
            candidates.append(card.get("mblog"))

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("id") is None and not candidate.get("bid"):
                continue
            if not isinstance(candidate.get("user"), dict):
                continue
            return candidate

        return None

    def _find_mblog_in_cards(self, cards: List[Dict[str, Any]], token: str) -> Optional[Dict[str, Any]]:
        for card in cards:
            if not isinstance(card, dict) or card.get("card_type") != 9:
                continue

            mblog = card.get("mblog")
            if not isinstance(mblog, dict):
                continue

            if token in {str(mblog.get("id") or "").strip(), str(mblog.get("bid") or "").strip()}:
                return mblog

        return None

    async def _resolve_status_page(self, url: str) -> Tuple[str, Optional[str]]:
        try:
            response = await self.client.get(url, headers=self.weibo_http.get_headers(""), follow_redirects=True)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning(f"WeiboMonitor: 解析微博链接跳转失败 {url}: {err}")
            return url, None

        html: Optional[str] = None
        content_type = str(response.headers.get("content-type", "")).lower()
        if response.status_code == 200 and "text/html" in content_type:
            html = response.text
        return str(response.url), html

    async def _fetch_status_mblog(self, token: str) -> Optional[Dict[str, Any]]:
        payload = await self.weibo_http.request_json(f"{WEIBO_STATUS_API}?id={token}")
        if not payload:
            return None
        return self._extract_mblog_from_status_payload(payload)

    async def _resolve_weibo_post_from_url(self, url: str) -> Optional[WeiboPost]:
        original_uid, original_token = self._extract_status_ref_from_url(url)
        final_url, html = await self._resolve_status_page(url)
        final_uid, final_token = self._extract_status_ref_from_url(final_url)
        html_status_id = self._extract_status_id_from_html(html or "")

        fallback_uid = final_uid or original_uid or ""
        tokens: List[str] = []
        for token in (final_token, original_token, html_status_id):
            token_text = str(token or "").strip()
            if token_text and token_text not in tokens:
                tokens.append(token_text)

        for token in tokens:
            mblog = await self._fetch_status_mblog(token)
            if not mblog:
                continue

            post = self.weibo_parser.build_post(mblog, fallback_uid=fallback_uid)
            if post:
                return post

        if fallback_uid and tokens:
            cards = await self._fetch_weibo_cards(fallback_uid)
            if cards:
                for token in tokens:
                    mblog = self._find_mblog_in_cards(cards, token)
                    if not mblog:
                        continue

                    post = self.weibo_parser.build_post(mblog, fallback_uid=fallback_uid)
                    if post:
                        return post

        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def passive_parse_weibo_link(self, event: AstrMessageEvent):
        passive_config = self.passive_link_config
        if not passive_config.get("enabled", True):
            return

        if not self.auth_config.get("weibo_cookie", ""):
            return

        if self._is_message_from_self(event):
            return

        message_str = str(
            getattr(event, "message_str", "")
            or getattr(getattr(event, "message_obj", None), "message_str", "")
            or ""
        ).strip()
        if not message_str:
            return

        if bool(passive_config.get("ignore_commands", True)) and message_str.lstrip().startswith("/"):
            return

        urls = self._extract_weibo_urls(message_str)
        if not urls:
            return

        current_session_id = str(
            getattr(getattr(event, "message_obj", None), "session_id", "")
            or (event.get_session_id() if hasattr(event, "get_session_id") else "")
            or ""
        )
        if not current_session_id:
            return

        configured_targets = self._parse_multi_value(passive_config.get("targets", ""))
        targets = configured_targets or [current_session_id]

        max_links = self._safe_int(
            passive_config.get("max_links_per_message", DEFAULT_PASSIVE_LINK_MAX_PER_MESSAGE),
            DEFAULT_PASSIVE_LINK_MAX_PER_MESSAGE,
            min_value=1,
            max_value=5,
        )

        posts: List[WeiboPost] = []
        for url in urls[:max_links]:
            try:
                post = await self._resolve_weibo_post_from_url(url)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.error(f"WeiboMonitor: 被动解析微博链接失败 {url}: {err}")
                continue

            if post is not None:
                posts.append(post)

        if not posts:
            return

        result = await self.delivery_service.send_new_posts(posts, targets, self.message_template)
        if result["posts_sent"] == 0:
            logger.warning(f"WeiboMonitor: 被动链接解析发送失败 session={current_session_id}, targets={len(targets)}, urls={len(posts)}")

    async def terminate(self):
        self.running = False

        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

        if self.retry_worker_task:
            self.retry_worker_task.cancel()
            try:
                await self.retry_worker_task
            except asyncio.CancelledError:
                pass

        await self.delivery_service.close()
        await self.client.aclose()
        logger.info("WeiboMonitor: 插件已停止")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_export")
    async def weibo_export(self, event: AstrMessageEvent):
        if not self._is_bot_owner(event):
            yield event.plain_result("❌ 此指令仅机器人主人可用。")
            return

        try:
            config_json = json.dumps(self.config, ensure_ascii=False)
            encoded = base64.b64encode(config_json.encode("utf-8")).decode("utf-8")
            yield event.plain_result(
                "📦 配置导出成功（Base64）:\n\n"
                f"{encoded}\n\n"
                "可使用 /weibo_import <配置字符串> 导入。"
            )
        except Exception as err:
            logger.error(f"WeiboMonitor: 导出配置失败: {err}")
            yield event.plain_result(f"❌ 导出失败: {err}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("weibo_import")
    async def weibo_import(self, event: AstrMessageEvent, config_str: str = ""):
        if not self._is_bot_owner(event):
            yield event.plain_result("❌ 此指令仅机器人主人可用。")
            return

        if not config_str:
            yield event.plain_result("❌ 缺少配置字符串。用法: /weibo_import <配置字符串>")
            return

        try:
            new_config: Dict[str, Any]
            try:
                decoded_bytes = base64.b64decode(config_str, validate=True)
            except (binascii.Error, ValueError):
                decoded_bytes = None

            if decoded_bytes is not None:
                try:
                    decoded = decoded_bytes.decode("utf-8")
                except UnicodeDecodeError as err:
                    raise ValueError("Base64 配置不是有效的 UTF-8 文本") from err
                try:
                    new_config = json.loads(decoded)
                except json.JSONDecodeError as err:
                    raise ValueError("Base64 解码后的内容不是合法 JSON") from err
            else:
                try:
                    new_config = json.loads(config_str)
                except json.JSONDecodeError as err:
                    raise ValueError("配置字符串既不是合法 Base64(JSON) 也不是合法 JSON") from err

            if not isinstance(new_config, dict):
                raise ValueError("配置必须是对象")

            root_keys = set(new_config.keys())
            if not root_keys.issubset(SUPPORTED_CONFIG_ROOT_KEYS):
                raise ValueError("检测到旧版或无效配置结构，请使用当前分区配置")

            changed = 0
            for key, value in new_config.items():
                self.config[key] = value
                changed += 1

            try:
                if hasattr(self.context, "config_manager") and hasattr(self.context.config_manager, "save_config"):
                    self.context.config_manager.save_config()
            except Exception:
                pass

            yield event.plain_result(
                f"✅ 导入完成，共更新 {changed} 个配置分区。\n"
                "部分运行参数会在下一个监控周期生效。"
            )
        except Exception as err:
            logger.error(f"WeiboMonitor: 导入配置失败: {err}")
            yield event.plain_result(f"❌ 导入失败: {err}")

    @filter.command("weibo_verify")
    async def weibo_verify(self, event: AstrMessageEvent):
        cookie = self.auth_config.get("weibo_cookie", "")
        if not cookie:
            yield event.plain_result("❌ 当前未配置微博 Cookie。")
            return

        yield event.plain_result("🔍 正在验证微博 Cookie...")
        payload = await self.weibo_http.request_json(WEIBO_CONFIG_API)
        if not payload:
            yield event.plain_result("❌ 验证失败：接口无响应或返回异常。")
            return

        data = payload.get("data") or {}
        if not data.get("login"):
            yield event.plain_result("❌ Cookie 无效或登录态已过期。")
            return

        user = data.get("user") or {}
        user_id = user.get("id") or data.get("uid")
        screen_name = user.get("screen_name") or "未知"
        yield event.plain_result(f"✅ Cookie 有效，当前账号: {screen_name} (UID: {user_id})")

    @filter.command("weibo_check")
    async def weibo_check(self, event: AstrMessageEvent):
        rules = await self.rule_resolver.resolve_monitor_rules(force_following_refresh=True)
        if not rules:
            yield event.plain_result("❌ 没有可用的监控规则，请先配置订阅规则。")
            return

        rule = rules[0]
        posts = await self.check_weibo(rule.uid, force_fetch=True)
        if not posts:
            yield event.plain_result(f"ℹ️ UID {rule.uid} 未获取到可推送微博。")
            return

        result = await self.delivery_service.send_new_posts(posts[:1], list(rule.targets), self.message_template)
        if result["posts_sent"] == 0:
            yield event.plain_result("❌ 已抓取到微博，但推送失败，请检查日志。")
            return

        yield event.plain_result(
            f"✅ 推送完成：成功目标 {result['target_success']}，失败目标 {result['target_failure']}。"
        )

    @filter.command("weibo_check_all")
    async def weibo_check_all(self, event: AstrMessageEvent):
        rules = await self.rule_resolver.resolve_monitor_rules(force_following_refresh=True)
        if not rules:
            yield event.plain_result("❌ 没有可用的监控规则，请先配置订阅规则。")
            return

        yield event.plain_result(f"🔍 正在立即检查 {len(rules)} 条监控规则...")

        summaries: List[str] = []
        req_interval = self._safe_int(
            self.runtime_config.get("request_interval", DEFAULT_REQUEST_INTERVAL_SECONDS),
            DEFAULT_REQUEST_INTERVAL_SECONDS,
            min_value=1,
            max_value=60,
        )
        req_jitter = self._safe_int(self.runtime_config.get("request_interval_jitter", 0), 0, min_value=0, max_value=30)

        for index, rule in enumerate(rules):
            if index > 0:
                await asyncio.sleep(self._pick_interval(req_interval, req_jitter, minimum=1))

            posts = await self.check_weibo(rule.uid, force_fetch=True)
            if not posts:
                summaries.append(f"ℹ️ UID {rule.uid} 未获取到可推送微博")
                continue

            result = await self.delivery_service.send_new_posts(posts[:1], list(rule.targets), self.message_template)
            if result["posts_sent"] == 0:
                summaries.append(f"❌ UID {rule.uid} 推送失败")
            elif result["target_failure"] > 0:
                summaries.append(
                    f"⚠️ UID {rule.uid} 部分成功：成功 {result['target_success']}，失败 {result['target_failure']}"
                )
            else:
                summaries.append(f"✅ UID {rule.uid} 推送成功")

        yield event.plain_result("\n".join(summaries))

    async def run_monitor(self):
        logger.info("WeiboMonitor: 监控任务已启动")
        await asyncio.sleep(STARTUP_DELAY_SECONDS)

        while self.running:
            try:
                cookie = self.auth_config.get("weibo_cookie", "")
                check_interval = self._safe_int(
                    self.runtime_config.get("check_interval", DEFAULT_CHECK_INTERVAL_MINUTES),
                    DEFAULT_CHECK_INTERVAL_MINUTES,
                    min_value=1,
                    max_value=24 * 60,
                )
                check_jitter = self._safe_int(self.runtime_config.get("check_interval_jitter", 0), 0, min_value=0, max_value=180)
                sleep_minutes = self._pick_interval(check_interval, check_jitter, minimum=1)

                self.cache_manager.cleanup()

                if not cookie:
                    logger.warning("WeiboMonitor: 未配置微博 Cookie，跳过本轮检查。")
                else:
                    rules = await self.rule_resolver.resolve_monitor_rules(force_following_refresh=False)
                    if not rules:
                        logger.debug("WeiboMonitor: 当前无可用监控规则")
                    else:
                        await self._run_monitor_cycle(rules)

                logger.debug(f"WeiboMonitor: 下次检查将在 {sleep_minutes} 分钟后执行")
                await asyncio.sleep(sleep_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error(f"WeiboMonitor: 监控循环异常: {err}")
                await asyncio.sleep(60)

    async def _run_monitor_cycle(self, rules: List[MonitorRule]) -> None:
        req_interval = self._safe_int(
            self.runtime_config.get("request_interval", DEFAULT_REQUEST_INTERVAL_SECONDS),
            DEFAULT_REQUEST_INTERVAL_SECONDS,
            min_value=1,
            max_value=60,
        )
        req_jitter = self._safe_int(self.runtime_config.get("request_interval_jitter", 0), 0, min_value=0, max_value=30)

        for index, rule in enumerate(rules):
            if index > 0:
                await asyncio.sleep(self._pick_interval(req_interval, req_jitter, minimum=1))

            try:
                posts = await self.check_weibo(rule.uid)
                if posts:
                    await self.delivery_service.send_new_posts(posts, list(rule.targets), self.message_template)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.error(f"WeiboMonitor: 检查 UID {rule.uid} 失败: {err}")

    async def _fetch_weibo_cards(self, uid: str) -> List[Dict[str, Any]]:
        url = f"{WEIBO_API_BASE}?type=uid&value={uid}&containerid=107603{uid}"
        payload = await self.weibo_http.request_json(url, uid=uid)
        if not payload or payload.get("ok") != 1:
            return []

        data = payload.get("data")
        if not isinstance(data, dict):
            logger.warning(f"WeiboMonitor: UID={uid} 返回 data 结构异常，已跳过本轮。")
            return []

        cards = data.get("cards")
        if not isinstance(cards, list):
            logger.warning(f"WeiboMonitor: UID={uid} 返回 cards 结构异常，已跳过本轮。")
            return []

        return cards

    async def check_weibo(self, uid: str, force_fetch: bool = False) -> List[WeiboPost]:
        try:
            cards = await self._fetch_weibo_cards(uid)
            if not cards:
                return []

            mblogs, username = self.weibo_parser.extract_non_top_mblogs(cards)
            if not mblogs:
                return []

            last_id_key = f"last_id_{uid}"
            last_id = self._safe_int(self._state_get(last_id_key, 0), 0, min_value=0)

            if not force_fetch and (last_id == 0 or uid not in self.session_initialized_uids):
                latest_id = self._safe_int(mblogs[0].get("id", 0), 0, min_value=0)
                if latest_id:
                    self._state_set(last_id_key, latest_id)
                    self.session_initialized_uids.add(uid)
                    logger.info(f"WeiboMonitor: 初始化 UID={uid}, 基准微博 ID={latest_id}")
                return []

            self.session_initialized_uids.add(uid)
            # 微博正文清洗依赖 BeautifulSoup，放在线程池中避免阻塞事件循环。
            posts = await asyncio.to_thread(self._collect_new_posts, uid, username, mblogs, last_id, force_fetch)

            if not force_fetch:
                latest_id = self._safe_int(mblogs[0].get("id", 0), 0, min_value=0)
                if latest_id > last_id:
                    self._state_set(last_id_key, latest_id)

            if posts:
                posts.reverse()
            return posts
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.error(f"WeiboMonitor: 检查 UID {uid} 异常: {err}")
            return []

    def _collect_new_posts(
        self,
        uid: str,
        username: str,
        mblogs: List[Dict[str, Any]],
        last_id: int,
        force_fetch: bool,
    ) -> List[WeiboPost]:
        posts: List[WeiboPost] = []

        filter_keywords = self._parse_keyword_list(self.content_config.get("filter_keywords", []))
        whitelist_keywords = self._parse_keyword_list(self.content_config.get("whitelist_keywords", []))
        whitelist_match_topics = bool(self.content_config.get("whitelist_match_topics", True))

        send_original = bool(self.content_config.get("send_original", True))
        send_forward = bool(self.content_config.get("send_forward", True))

        for mblog in mblogs:
            post_id = self._safe_int(mblog.get("id", 0), 0, min_value=0)
            if post_id == 0:
                continue

            if not force_fetch and post_id <= last_id:
                break

            is_forward = isinstance(mblog.get("retweeted_status"), dict)
            if is_forward and not send_forward:
                continue
            if (not is_forward) and not send_original:
                continue

            text = self.weibo_parser.extract_post_text(mblog)
            if self._contains_any_keyword(text, filter_keywords):
                continue

            topics = self.weibo_parser.extract_topics(mblog)
            if not self._passes_whitelist(text, topics, whitelist_keywords, whitelist_match_topics):
                continue

            post = self.weibo_parser.build_post(mblog, fallback_uid=uid, default_username=username)
            if post is None:
                continue
            posts.append(post)

            if force_fetch:
                break

        return posts

    def _contains_any_keyword(self, text: str, keywords: List[str]) -> bool:
        return any(keyword and keyword in text for keyword in keywords)

    def _passes_whitelist(self, text: str, topics: List[str], whitelist: List[str], whitelist_match_topics: bool) -> bool:
        if not whitelist:
            return True

        text_hit = any(keyword and keyword in text for keyword in whitelist)
        topic_hit = False
        if whitelist_match_topics:
            topic_hit = any(
                keyword and any(keyword in topic for topic in topics)
                for keyword in whitelist
            )

        return text_hit or topic_hit

__all__ = ["Main"]
