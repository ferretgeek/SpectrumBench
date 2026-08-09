# Changelog

## Unreleased

- 将 Starlette 测试客户端依赖从 `httpx2 2.9.1` 更新到 `2.10.0`，保持测试路径使用受维护的 HTTPX2 实现并消除回退到旧 `httpx` 时的弃用告警。
- Updated the Starlette test-client dependency from `httpx2 2.9.1` to `2.10.0`, keeping tests on the maintained HTTPX2 path and avoiding the deprecation warning produced by an old-HTTPX fallback environment.

## 1.0.1 — 2026-08-09

- Reissued the release from a fully rewritten, two-engine-clean public history after a test function name matched a third-party credential pattern.
- Retired the immutable `v1.0.0` tag and moved the supported release to `v1.0.1`; no real secret or personal information was involved.
- Includes the fixed report identifier boundary, same-origin download validation, safer filenames, UI polish, packaging, and documentation updates from the final audit.

## 1.0.0 — 2026-08-09

- 首个公开版本：三种固定单流测量模式、实时看板、历史、逐请求明细与一次性 JSON 报告。
- 凭据和接口地址改为仅在当前会话内存中使用；加入同源 WebSocket、严格 URL 校验、CSP 与安全响应头。
- 加入鸢尾、青玉、晨曦三套浅色主题和深灰暗色主题，以及 SVG / PNG / ICO 品牌图标。
- 加入 Windows、本地 Python、Docker Compose、systemd + SSH 隧道部署路径。

## 1.0.0 — English

- First public release with three locked single-stream methods, a live dashboard, history, request details, and one-time JSON reports.
- Credentials and endpoints are memory-only; same-origin WebSockets, strict URL validation, CSP, and security headers are enforced.
- Added three light palettes, a deep-gray dark theme, and SVG / PNG / ICO brand icons.
- Added Windows, local Python, Docker Compose, and systemd + SSH tunnel deployment paths.
