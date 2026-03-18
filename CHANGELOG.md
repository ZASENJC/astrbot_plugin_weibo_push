# Changelog

仅记录独立发布后的版本更新。

## v2.1.7 (2026-03-19)

### Changed

- `passive_link_recognition` 新增 `targets` 配置，支持填写多个会话 ID；留空时仍默认回推到当前会话。
- “关注列表自动监控”改为 `monitoring_settings.auto_following` 子配置分组，统一收纳启用开关、来源账号、推送目标与刷新参数。
- 关注列表变更通知不再单独配置目标，会默认推送到 `auto_following.targets`。

## v2.1.6 (2026-03-19)

### Added

- 新增会话内被动微博链接识别：用户发送微博链接后，插件会自动解析并将微博内容推送回当前会话。
- `monitoring_settings` 新增 `passive_link_recognition` 配置分组，提供启用开关、命令消息忽略开关与单条消息最大解析链接数限制。

### Changed

- 配置面板中“被动链接识别”排序位于“监控规则”下、“关注列表自动监控”上，便于与主动监控配置并列管理。

## v2.1.5 (2026-03-15)

### Security

- `/weibo_export` 与 `/weibo_import` 新增 `@filter.permission_type(filter.PermissionType.ADMIN)`。
- 两个指令增加机器人主人校验，仅允许 `admins_id` 首位账号执行，阻断普通用户导入导出配置的越权风险。

### Fixed

- `weibo_import` 的 Base64/JSON 解析改为精确异常分支，不再使用宽泛 `except Exception` 回退，错误信息更明确。
- `WeiboDeliveryService.take_screenshot` 增加 `page/browser` 显式 `finally` 关闭，避免异常分支资源泄露。
- 截图链路改为复用单例 Browser，仅按次创建/关闭 `Page`，避免每条微博冷启动 Chromium 的性能开销。

### Refactor

- 删除 `Main` 中大批无意义透传方法，改为在调用点直接使用 `rule_resolver` / `delivery_service` / `weibo_parser`。

## v2.1.4 (2026-03-15)

### Changed

- 新增 `RetryManager`，将重试策略计算、入队与重试协程从 `Main` 中独立出来。
- 新增 `MonitorRuleResolver`，将手动规则、自动关注规则、UID 解析等逻辑集中到规则组件。
- 新增 `WeiboDeliveryService`，将推送构建、分发发送、媒体下载与截图逻辑独立为发送组件。
- 新增 `MediaCacheManager`，集中管理缓存文件创建、活跃标记、延迟释放与过期清理。
- `Main` 现在主要负责生命周期与流程编排，各能力通过委托调用。

## v2.1.3 (2026-03-15)

### Changed

- 将微博请求与响应校验逻辑拆分到 `WeiboHttpClient`，`Main` 仅保留调度与编排职责。
- 将微博正文/话题/媒体解析逻辑拆分到 `WeiboPostParser`，降低 `Main` 类职责密度。
- `Main` 中相关方法改为委托调用，保持现有行为与外部接口不变。

## v2.1.2 (2026-03-15)

### Changed

- 移除插件运行时自动安装 `playwright` / `chromium` 的逻辑，改为遵循 AstrBot 插件依赖规范，由 `requirements.txt` + 手动初始化方式管理依赖。
- `_conf_schema.json` 删除 `auto_install_playwright` 配置项，并补充截图功能的手动安装提示。

### Fixed

- `_request_json` 增加 JSON 顶层类型校验，仅接受对象结构，避免异常返回导致 `.get()` 崩溃。
- 微博正文解析流程改为通过 `asyncio.to_thread` 执行，降低 `BeautifulSoup` 解析对事件循环的阻塞影响。

## v2.1.1 (2026-03-15)

### Changed

- 按 AstrBot 官方新建插件文档收敛为单文件结构：核心实现回归 `main.py`。
- 移除 `weibo_push/` 子模块目录，取消跨文件转发包装。
- 插件主类统一为 `Main`，命令处理器直接定义在主类中。
- 移除已弃用的 `@register` 旧式注册方式，改为 `Star` 子类自动识别。

### Fixed

- 修复插件后台配置不显示问题（重复注册导致命中无配置对象）。
- `_conf_schema.json` 中列表项声明改为 `items` 结构，提升与当前配置面板兼容性。
- 修复微博 URL UID 解析正则错误，`https://weibo.com/u/<uid>` 可正常识别。

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
