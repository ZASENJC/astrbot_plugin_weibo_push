try:
    # 官方文档示例中，插件内跨文件导入使用相对导入
    from .weibo_push import WeiboMonitor
except ImportError:
    # 兼容少数以脚本方式加载插件入口的场景
    from weibo_push import WeiboMonitor

__all__ = ["WeiboMonitor"]
