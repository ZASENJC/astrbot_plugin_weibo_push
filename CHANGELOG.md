# Changelog

仅记录独立发布后的版本更新。

## v2.1.0 (2026-03-15)

### Added

- 新增发送模式开关 `merge_forward_send`：
  - 关闭：分段发送（正文后媒体）
  - 开启：全部合并为一条转发消息
- 新增失败重试队列 + 指数退避策略：
  - `retry_enabled`
  - `retry_max_attempts`
  - `retry_base_delay`
  - `retry_max_delay`
  - `retry_jitter`
  - `retry_queue_max_size`

### Changed

- 项目命名调整为“微博推送 (Weibo Push)”。
- 插件标识更新为 `astrbot_plugin_weibo_push`。
- 元数据与文档链接更新为新仓库命名。
- README 重写为简洁专业版，并补充原仓库灵感致谢。

### Improved

- 新增旧数据迁移候选路径，兼容从旧插件名目录迁移状态文件。
- 重试队列独立后台任务，发送失败后不会阻塞监控主循环。

## v2.0.0 (2026-03-15)

### Breaking Changes

- 项目切换为独立维护分支，仓库与文档链接更新为当前仓库。
- 核心代码从单文件重构为模块化结构：`main.py` + `weibo_push/core.py`。
- 移除插件内 `/get_umo` 指令，改用 AstrBot 内置 `/sid` 获取会话 ID。
- 默认消息模板新增 `{topics}` 变量。

### Added

- 新增关注列表主动监控能力。
- 新增白名单话题匹配开关 `whitelist_match_topics`。
- 新增截图依赖自动安装开关 `auto_install_playwright`。

### Changed

- 推送发送改为并发目标发送，提升多会话推送效率。
- 自动关注同步状态写入改为批量落盘，减少磁盘 I/O。
