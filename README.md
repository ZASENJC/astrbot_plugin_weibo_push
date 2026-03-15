# 微博推送助手

面向 AstrBot 的微博监控推送插件，支持多账号订阅、关注列表同步、关键词过滤和图片/视频解析。

## 致谢

本项目的早期灵感来源于 [jiantoucn/astrbot_plugin_weibo_monitor](https://github.com/jiantoucn/astrbot_plugin_weibo_monitor)，当前仓库为独立维护版本。

## 版本概览

- `v2.1.5`: 高危指令改为仅机器人主人可用，修复截图异常分支显式回收，并移除 Main 中冗余代理方法。
- `v2.1.4`: 完成主流程职责拆解，新增规则解析、重试调度、发送分发与缓存管理组件。
- `v2.1.3`: 将网络请求与微博解析能力拆分为独立组件，降低 `Main` 类耦合度。
- `v2.1.2`: 按审核建议移除运行时依赖安装，修复 JSON 类型假设并将微博文本清洗移入线程池。
- `v2.1.1`: 按官方新建插件文档回归单文件结构，修复后台配置不显示问题。
- `v2.1.0`: 新增分段发送 / 合并转发开关，新增失败重试队列与退避策略。
- `v2.0.0`: 完成模块化重构，新增关注列表主动监控与话题白名单匹配。

## 功能

- 多微博账号监控与定向会话推送
- 关注列表自动同步监控
- 白名单支持正文与话题匹配
- 分段发送与合并转发两种推送模式
- 失败重试队列与指数退避
- 图片、视频、网页截图推送

## 快速开始

1. 安装依赖：`pip install -r requirements.txt`。
2. 如需网页截图功能，额外执行：`playwright install chromium`。
3. 在插件配置中填写 `auth_settings.weibo_cookie`。
4. 在目标会话发送 AstrBot 内置命令 `/sid` 获取会话 ID。
5. 在 `monitoring_settings.subscription_rules` 中配置 `source` 和 `allowed_targets`。
6. 使用 `/weibo_verify` 验证 Cookie。
7. 使用 `/weibo_check` 验证首轮推送。

## 如何获取微博 Cookie

1. 在电脑浏览器打开 [微博移动端官网](https://m.weibo.cn/) 并登录。
2. 按 `F12` 打开开发者工具，切换到 `网络 (Network)` 选项卡。
3. 刷新页面，在左侧列表中找到第一个 `m.weibo.cn` 的请求（或者任何一个 `getIndex` 请求）。
4. 在右侧的 `请求标头 (Request Headers)` 中找到 `Cookie` 字段。
5. 复制该字段的完整值，粘贴到插件设置的 `weibo_cookie` 中。

## 常用配置

- `content_settings.merge_forward_send`: `false` 为分段发送，`true` 为合并转发。
- `content_settings.whitelist_match_topics`: 白名单是否匹配微博话题。
- `monitoring_settings.auto_following_enabled`: 是否启用关注列表自动同步。
- `runtime_settings.retry_enabled`: 是否启用失败重试队列。
- `runtime_settings.retry_max_attempts`: 最大发送尝试次数，包含首次发送。
- `runtime_settings.retry_base_delay`、`retry_max_delay`、`retry_jitter`: 退避参数。

## 指令

- `/weibo_verify`
- `/weibo_check`
- `/weibo_check_all`
- `/weibo_export`（仅机器人主人）
- `/weibo_import <配置字符串>`（仅机器人主人）

## 官方参考

- [AstrBot 文档首页](https://docs.astrbot.app/)
