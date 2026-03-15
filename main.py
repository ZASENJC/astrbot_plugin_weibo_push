from astrbot.api.event import AstrMessageEvent, filter

try:
    from .weibo_push import WeiboMonitor as _WeiboMonitor
except ImportError:
    from weibo_push import WeiboMonitor as _WeiboMonitor


class Main(_WeiboMonitor):
    @filter.command("weibo_export")
    async def weibo_export(self, event: AstrMessageEvent):
        return await super().weibo_export(event)

    @filter.command("weibo_import")
    async def weibo_import(self, event: AstrMessageEvent, config_str: str = ""):
        return await super().weibo_import(event, config_str)

    @filter.command("weibo_verify")
    async def weibo_verify(self, event: AstrMessageEvent):
        return await super().weibo_verify(event)

    @filter.command("weibo_check")
    async def weibo_check(self, event: AstrMessageEvent):
        return await super().weibo_check(event)

    @filter.command("weibo_check_all")
    async def weibo_check_all(self, event: AstrMessageEvent):
        return await super().weibo_check_all(event)


__all__ = ["Main"]
