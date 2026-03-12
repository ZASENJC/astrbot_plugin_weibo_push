import asyncio
import re
import httpx
import os
import json
import base64
from typing import List, Optional
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from bs4 import BeautifulSoup


@register("astrbot_plugin_weibo_monitor", "Sayaka", "定时监控微博用户动态并推送到指定会话。", "v1.6.0", "https://github.com/jiantoucn/astrbot_plugin_weibo_monitor")
class WeiboMonitor(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.monitor_task = None
        self.client = httpx.AsyncClient(timeout=20) # 增加超时时间到 20s，提高稳定性
        self.running = True
        self.session_initialized_uids = set() # 用于跟踪本会话已初始化的 UID

        # 确保数据目录存在
        self.data_dir = StarTools.get_data_dir()
        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "monitor_data.json"
        
        # 兼容旧路径迁移 (data/astrbot_plugin_weibo_monitor -> StarTools.get_data_dir())
        old_data_file = os.path.join("data", "astrbot_plugin_weibo_monitor", "monitor_data.json")
        if not self.data_file.exists() and os.path.exists(old_data_file):
            try:
                import shutil
                shutil.copy2(old_data_file, self.data_file)
                logger.info(f"WeiboMonitor: 已从旧路径迁移数据到 {self.data_file}")
            except Exception as e:
                logger.error(f"WeiboMonitor: 迁移数据失败: {e}")

        self._data = self._load_data()

        # 启动后台监控任务
        # 为了支持热重载和防止事件丢失，在 __init__ 中启动
        self.monitor_task = asyncio.create_task(self.run_monitor())

    def _load_data(self) -> dict:
        """从文件加载持久化数据"""
        if self.data_file.exists():
            try:
                return json.loads(self.data_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"WeiboMonitor: 加载数据文件失败: {e}")
        return {}

    def _save_data(self):
        """将持久化数据保存到文件"""
        try:
            self.data_file.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=4), 
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"WeiboMonitor: 保存数据文件失败: {e}")

    async def get_kv_data(self, key: str, default=None):
        """获取持久化键值对"""
        return self._data.get(key, default)

    async def put_kv_data(self, key: str, value):
        """设置并保存持久化键值对"""
        self._data[key] = value
        self._save_data()

    def get_headers(self, uid: str = "") -> dict:
        cookie = self.config.get("weibo_cookie", "")
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if uid:
            headers["Referer"] = f"https://m.weibo.cn/u/{uid}"
        else:
            headers["Referer"] = "https://m.weibo.cn/"

        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def terminate(self):
        self.running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()
        logger.info("WeiboMonitor 插件已停止")

    def get_targets(self) -> List[str]:
        targets_raw = self.config.get("target_conversation_id", [])
        if isinstance(targets_raw, str):
            return [t.strip() for t in targets_raw.split(",") if t.strip()]
        return [str(t).strip() for t in targets_raw if str(t).strip()]

    @filter.command("get_umo")
    async def get_umo(self, event: AstrMessageEvent):
        """获取当前会话的 ID (unified_msg_origin)，用于设置推送目标"""
        yield event.plain_result(
            f"当前会话 ID: {event.unified_msg_origin}\n请将此 ID 填入插件设置中的 target_conversation_id 项。"
        )

    @filter.command("weibo_export")
    async def weibo_export(self, event: AstrMessageEvent):
        """导出当前插件配置"""
        try:
            config_json = json.dumps(self.config, ensure_ascii=False)
            config_b64 = base64.b64encode(config_json.encode("utf-8")).decode("utf-8")
            yield event.plain_result(
                f"📦 WeiboMonitor 配置导出成功 (Base64格式):\n\n{config_b64}\n\n"
                f"💡 请妥善保管此字符串，在其他会话或环境中使用 /weibo_import [配置字符串] 即可导入。"
            )
        except Exception as e:
            logger.error(f"WeiboMonitor: 导出配置失败: {e}")
            yield event.plain_result(f"❌ 导出配置失败: {e}")

    @filter.command("weibo_import")
    async def weibo_import(self, event: AstrMessageEvent, config_str: str = ""):
        """从导出的字符串导入配置"""
        if not config_str:
            yield event.plain_result("❌ 请提供配置字符串。用法: /weibo_import <配置字符串>")
            return

        try:
            # 兼容直接 JSON 或 Base64
            try:
                decoded = base64.b64decode(config_str).decode("utf-8")
                new_config = json.loads(decoded)
            except Exception:
                new_config = json.loads(config_str)

            if not isinstance(new_config, dict):
                raise ValueError("配置格式不正确")

            # 兼容性合并：保持当前版本已有的键，仅更新导入的键
            # 即使未来增加了更多配置项，此导入逻辑依然稳健
            count = 0
            for key, value in new_config.items():
                self.config[key] = value
                count += 1

            # 尝试调用框架的配置保存接口（如果支持）
            try:
                if hasattr(self.context, "config_manager") and hasattr(self.context.config_manager, "save_config"):
                    self.context.config_manager.save_config()
            except:
                pass

            yield event.plain_result(
                f"✅ 成功导入 {count} 项配置！\n"
                f"注意：部分配置（如检查间隔）可能需要重启插件后才能完全生效。"
            )
        except Exception as e:
            logger.error(f"WeiboMonitor: 导入配置失败: {e}")
            yield event.plain_result(f"❌ 导入配置失败: {e}")

    @filter.command("weibo_verify")
    async def weibo_verify(self, event: AstrMessageEvent):
        """验证当前配置的 Cookie 是否有效"""
        cookie = self.config.get("weibo_cookie", "")
        if not cookie:
            yield event.plain_result("❌ 未配置 Cookie。")
            return

        yield event.plain_result("🔍 正在验证 Cookie 有效性...")
        try:
            resp = await self.client.get(
                "https://m.weibo.cn/api/config", headers=self.get_headers()
            )
            if resp.status_code == 200:
                data = resp.json()
                data_obj = data.get("data") or {}
                if data_obj.get("login"):
                    user = data_obj.get("user")
                    if user:
                        yield event.plain_result(
                            f"✅ Cookie 有效！\n当前登录用户: {user.get('screen_name')} (UID: {user.get('id')})"
                        )
                    else:
                        uid = data_obj.get("uid")
                        yield event.plain_result(
                            f"✅ Cookie 有效！\n已登录但未获取到详细用户信息 (UID: {uid})"
                        )
                else:
                    yield event.plain_result(
                        "❌ Cookie 已失效或未登录（接口返回 login: false）。"
                    )
            else:
                yield event.plain_result(f"❌ 验证请求失败，状态码: {resp.status_code}")
        except Exception as e:
            logger.error(f"WeiboMonitor: 验证过程中出现错误: {e}")
            yield event.plain_result(f"❌ 验证过程中出现错误: {e}")

    @filter.command("weibo_check")
    async def weibo_check(self, event: AstrMessageEvent):
        """立即检查并发送最新一条微博信息（用于测试）"""
        urls_raw = self.config.get("weibo_urls", [])
        if isinstance(urls_raw, str):
            urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
        else:
            urls = urls_raw

        targets = self.get_targets()
        msg_format = self.message_format
        req_interval = self.config.get("request_interval", 5)

        if not urls:
            yield event.plain_result("❌ 未在插件设置中配置监控 URL。")
            return

        yield event.plain_result(
            f"🔍 正在立即检查 {len(urls)} 个微博账号的最新动态 (间隔 {req_interval}s)..."
        )

        results = []
        for i, url in enumerate(urls):
            if i > 0:
                await asyncio.sleep(req_interval)

            uid = await self.parse_uid(url)
            if not uid:
                results.append(f"❌ 无法解析 URL: {url}")
                continue

            latest_posts = await self.check_weibo(uid, force_fetch=True)
            if latest_posts:
                post = latest_posts[0]
                content = msg_format.format(
                    name=post.get("username", "未知用户"),
                    weibo=post["text"],
                    link=post["link"],
                )
                chain = MessageChain().message(content)

                if not targets:
                    await self.context.send_message(event.unified_msg_origin, chain)
                else:
                    for target in targets:
                        await self.context.send_message(target, chain)

                results.append(f"✅ {post.get('username')} 已成功发送最新动态。")
            else:
                results.append(f"ℹ️ UID {uid} 未获取到有效微博。")

        yield event.plain_result("\n".join(results))

    @property
    def message_format(self) -> str:
        """获取并格式化消息模板"""
        return self.config.get(
            "message_format", "🔔 {name} 发微博啦！\n\n{weibo}\n\n链接: {link}"
        ).replace("\\n", "\n")

    async def run_monitor(self):
        logger.info("微博监控任务已启动")
        await asyncio.sleep(10)

        while self.running:
            try:
                urls_raw = self.config.get("weibo_urls", [])
                if isinstance(urls_raw, str):
                    urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
                else:
                    urls = urls_raw

                interval = self.config.get("check_interval", 10)
                req_interval = self.config.get("request_interval", 5)
                targets = self.get_targets()
                msg_format = self.message_format

                if not urls:
                    logger.debug("WeiboMonitor: 未配置监控 URL")
                elif not targets:
                    logger.debug("WeiboMonitor: 未配置推送目标会话 ID")
                else:
                    for i, url in enumerate(urls):
                        try:
                            if i > 0:
                                await asyncio.sleep(req_interval)

                            uid = await self.parse_uid(url)
                            if not uid:
                                continue

                            new_posts = await self.check_weibo(uid)
                            if new_posts:
                                for post in new_posts:
                                    content = msg_format.format(
                                        name=post.get("username", "未知用户"),
                                        weibo=post["text"],
                                        link=post["link"],
                                    )
                                    chain = MessageChain().message(content)
                                    for target in targets:
                                        await self.context.send_message(target, chain)
                                    logger.info(
                                        f"WeiboMonitor: 已向 {len(targets)} 个目标推送 {post.get('username')} 的更新"
                                    )
                        except Exception as e:
                            logger.error(f"WeiboMonitor: 检查 URL {url} 时出错: {e}")

                await asyncio.sleep(max(1, interval) * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WeiboMonitor 运行时错误: {e}")
                await asyncio.sleep(60)

    async def parse_uid(self, url: str) -> Optional[str]:
        """
        解析微博 URL 或用户名，提取 UID。
        支持:
        1. 直接输入 UID (如: 12345678)
        2. 个人主页 URL (如: https://m.weibo.cn/u/12345678)
        3. 微博域名 URL (如: https://weibo.com/u/12345678)
        4. 用户名跳转 URL (如: https://weibo.com/n/用户名)
        """
        url = url.strip()
        if url.isdigit():
            return url

        # 匹配 /u/UID 格式
        match = re.search(r"weibo\.(com|cn)/u/(\d+)", url)
        if match:
            return match.group(2)

        # 匹配 /n/用户名 格式
        match_name = re.search(r"weibo\.(com|cn)/n/([^/?#]+)", url)
        if match_name:
            name = match_name.group(2)
            try:
                # 请求跳转接口
                resp = await self.client.get(
                    f"https://m.weibo.cn/n/{name}",
                    headers=self.get_headers(),
                    follow_redirects=False,
                )
                if resp.status_code == 302:
                    location = resp.headers.get("Location", "")
                    match_uid = re.search(r"/u/(\d+)", location)
                    if match_uid:
                        return match_uid.group(1)
            except Exception as e:
                logger.error(f"WeiboMonitor: 解析用户名 {name} 失败: {e}")
        return None

    async def _fetch_weibo_cards(self, uid: str) -> List[dict]:
        """获取指定 UID 的微博卡片列表"""
        api_url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}"
        try:
            resp = await self.client.get(api_url, headers=self.get_headers(uid))
            if resp.status_code != 200:
                logger.error(f"WeiboMonitor: 接口请求失败 (状态码 {resp.status_code}), UID: {uid}")
                return []
            data = resp.json()
            if data.get("ok") != 1:
                logger.debug(f"WeiboMonitor: 接口返回数据状态异常, UID: {uid}")
                return []
            return (data.get("data") or {}).get("cards", [])
        except Exception as e:
            logger.error(f"WeiboMonitor: 获取 UID {uid} 数据时出错: {e}")
            return []

    def _extract_valid_mblogs(self, cards: List[dict]) -> tuple[List[dict], str]:
        """从卡片列表中提取有效的微博博文，并过滤置顶"""
        valid_mblogs = []
        username = "未知用户"
        for card in cards:
            if card.get("card_type") == 9 and card.get("mblog"):
                mblog = card["mblog"]
                # 极其严格的置顶过滤
                is_top = (
                    mblog.get("isTop") or 
                    mblog.get("is_top") or 
                    card.get("is_top") or 
                    mblog.get("top") or
                    (mblog.get("title") or {}).get("text") == "置顶"
                )
                if is_top:
                    continue
                valid_mblogs.append(mblog)
                if username == "未知用户":
                    username = (mblog.get("user") or {}).get("screen_name", "未知用户")
        return valid_mblogs, username

    async def check_weibo(self, uid: str, force_fetch: bool = False) -> List[dict]:
        """
        检查指定 UID 的最新微博。
        :param uid: 微博用户 ID
        :param force_fetch: 是否强制获取最新一条（不比较 last_id）
        :return: 包含新微博信息的列表
        """
        cards = await self._fetch_weibo_cards(uid)
        if not cards:
            return []

        valid_mblogs, username = self._extract_valid_mblogs(cards)
        if not valid_mblogs:
            return []

        # 获取上次记录的微博 ID
        last_id_key = f"last_id_{uid}"
        last_id_str = await self.get_kv_data(last_id_key, "0")
        last_id = int(last_id_str)

        # 1. 初始化检查：如果是全新监控或会话首次检查（非强制触发）
        if not force_fetch and (last_id == 0 or uid not in self.session_initialized_uids):
            latest_id_val = valid_mblogs[0].get("id")
            if latest_id_val:
                latest_id = int(latest_id_val)
                await self.put_kv_data(last_id_key, str(latest_id))
                self.session_initialized_uids.add(uid)
                if last_id == 0:
                    logger.info(f"WeiboMonitor: 已初始化全新监控 UID {uid} ({username})，起始 ID: {latest_id}")
                else:
                    logger.info(f"WeiboMonitor: 已同步会话初始状态，UID {uid} ({username})，当前最新 ID: {latest_id}")
            return []
        
        self.session_initialized_uids.add(uid)

        # 2. 遍历微博，找出新帖
        new_posts = []
        filter_keywords = self.config.get("filter_keywords", [])
        
        for mblog in valid_mblogs:
            current_id_val = mblog.get("id")
            if not current_id_val:
                continue
            current_id = int(current_id_val)
            
            # 停止条件：如果已经检查到旧帖
            if not force_fetch and current_id <= last_id:
                break

            text = self.clean_text(mblog.get("text", ""))
            
            # 屏蔽词过滤
            has_filter_keyword = False
            for keyword in filter_keywords:
                if keyword and keyword in text:
                    has_filter_keyword = True
                    logger.info(f"WeiboMonitor: 微博 {current_id} 包含屏蔽词 '{keyword}'，已跳过推送")
                    break
            
            if not has_filter_keyword:
                bid = mblog.get("bid")
                link = f"https://weibo.com/{uid}/{bid}"
                new_posts.append({"text": text, "link": link, "username": username})

            if force_fetch: # 强制获取模式只取第一条（不论是否被过滤，但如果有屏蔽词会变空）
                break

        # 3. 更新状态：无论 new_posts 是否为空，只要有更新的 valid_mblogs[0]，就应当更新 last_id
        if not force_fetch:
            latest_id_val = valid_mblogs[0].get("id")
            if latest_id_val:
                latest_id = int(latest_id_val)
                if latest_id > last_id:
                    await self.put_kv_data(last_id_key, str(latest_id))

        if new_posts:
            new_posts.reverse() # 旧到新排列

        return new_posts

    def clean_text(self, text: str) -> str:
        """清理微博正文中的 HTML 标签并处理换行"""
        if not text:
            return ""
        if not isinstance(text, str):
            return str(text)
        try:
            soup = BeautifulSoup(text, "html.parser")
            # 将 <br> 标签替换为换行符
            for br in soup.find_all("br"):
                br.replace_with("\n")
            return soup.get_text().strip()
        except Exception as e:
            logger.error(f"WeiboMonitor: 清理文本内容失败: {e}")
            return text
