# Privacy / 隐私说明

本项目没有遥测、广告、分析 SDK 或后台账号系统。API Key 与 Base URL 仅在当前浏览器会话和服务进程内存中存在，不写入 `stress_state.json`、历史、报告或运行日志。升级旧状态时，程序会按白名单重建状态并移除旧凭据字段。

持久化内容包括非敏感界面草稿、样本标签、脱敏配置、测量统计和最多 24 条历史。运行日志只记录状态、计数和经过脱敏的错误；不会记录完整响应正文。报告下载令牌只在内存中存在且成功下载后立即失效。

启动真实测试时，提示词、API Key 和请求数据会发送到你填写的上游服务。该服务的隐私政策与留存行为不由本项目控制。浏览器扩展、系统代理、调试器、崩溃转储和具有本机读取权限的软件也超出本项目的防护边界。

This project has no telemetry, ads, analytics SDK, or hosted account system. API keys and Base URLs are memory-only and are excluded from state, history, reports, and runtime logs. Persisted data is limited to non-sensitive UI draft fields, run labels, sanitized configuration, statistics, and up to 24 history entries.

Starting a real run sends prompts, credentials, and request data to the upstream you configured. That provider's privacy and retention policies are outside this project's control. Browser extensions, system proxies, debuggers, crash dumps, and software with local read access are also outside the protection boundary.
