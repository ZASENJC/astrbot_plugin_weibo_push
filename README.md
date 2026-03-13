# AstrBot 微博监控插件 (Weibo Monitor)

定时监控微博用户动态并推送到指定会话。
由 Trae 配合 Gemini-3-Flash-Preview 模型开发。

## 功能特性

- **定时监控**：自定义检查频率。
- **多用户支持**：可同时监控多个微博账号。
- **精准推送**：仅推送最新更新，自动过滤置顶微博。
- **Cookie 支持**：支持配置 Cookie 以提高抓取稳定性。

## 部署步骤

1. **安装**：
   在 AstrBot 插件市场安装本插件。
   插件会自动安装 `requirements.txt` 中的依赖。如果手动安装，请在服务器环境下运行：
   ```bash
   pip install httpx beautifulsoup4
   ```

2. **获取会话 ID (unified_msg_origin)**：
   在你想接收推送的会话（群聊或私聊）中，向机器人发送指令：
   ```
   /get_umo
   ```
   机器人会返回当前会话的 ID，请记录下来。

3. **配置插件**：
   在 AstrBot 管理面板 -> 插件设置 -> `weibo_monitor` 中进行配置：
   - `weibo_urls`: 填入微博用户主页链接，如 `https://weibo.com/u/2803301701`。
    - `check_interval`: 检查间隔（分钟），建议不要设置得太短（如 5-10 分钟）。
    - `request_interval`: 账号请求间隔（秒），默认 5 秒。监控多个账号时，每个账号抓取之间会等待该时长，避免请求过快。
    - `target_conversation_id`: 填入第 2 步获取的会话 ID。支持填入多个 ID。
    - `weibo_cookie`: (可选) 如果抓取不到数据或频繁报 403/418 错误，请填写 Cookie。

## 自定义消息格式

你可以在插件设置的 `message_format` 中自定义推送消息的样式。支持以下变量：
- `{name}`: 微博用户的昵称。
- `{weibo}`: 微博正文内容（已自动处理换行）。
- `{link}`: 微博原文链接。

**默认格式示例：**
```
🔔 {name} 发微博啦！

{weibo}

链接: {link}
```

## 常用指令
- `/get_umo`: 获取当前会话 ID（用于配置推送目标）。
- `/weibo_verify`: 验证当前设置的 Cookie 是否有效。
- `/weibo_check`: 立即抓取列表里第一个账号并推送最新一条微博。
- `/weibo_check_all`: 立即抓取列表里所有账号并推送最新一条微博。
- `/weibo_export`: 将当前插件的所有配置导出为 Base64 字符串。
- `/weibo_import [配置字符串]`: 使用导出的配置字符串快速恢复或迁移配置。
## 如何获取微博 Cookie

1. 在电脑浏览器打开 [微博移动端官网](https://m.weibo.cn/) 并登录。
2. 按 `F12` 打开开发者工具，切换到 `网络 (Network)` 选项卡。
3. 刷新页面，在左侧列表中找到第一个 `m.weibo.cn` 的请求（或者任何一个 `getIndex` 请求）。
4. 在右侧的 `请求标头 (Request Headers)` 中找到 `Cookie` 字段。
5. 复制该字段的完整值，粘贴到插件设置 of `weibo_cookie` 中。
