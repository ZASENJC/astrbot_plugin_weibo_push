# 微博推送

AstrBot 的微博动态监控与推送插件，支持订阅规则、关注列表同步和关键词过滤。

## 致谢

本项目的早期灵感来源于 [jiantoucn/astrbot_plugin_weibo_monitor](https://github.com/jiantoucn/astrbot_plugin_weibo_monitor)，当前仓库为独立维护版本。

## 版本概览

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

1. 在插件配置中填写 `auth_settings.weibo_cookie`。
2. 在目标会话发送 AstrBot 内置命令 `/sid` 获取会话 ID。
3. 在 `monitoring_settings.subscription_rules` 中配置 `source` 和 `allowed_targets`。
4. 使用 `/weibo_verify` 验证 Cookie。
5. 使用 `/weibo_check` 验证首轮推送。

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
- `/weibo_export`
- `/weibo_import <配置字符串>`

## 官方参考

- [AstrBot 文档首页](https://docs.astrbot.app/)
