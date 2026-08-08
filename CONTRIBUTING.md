# Contributing / 参与贡献

欢迎缺陷修复、测量审查、文档改进与无障碍优化。提交前请先说明行为变化，并保持以下边界：

- 不改变单流串行、三个固定模式、usage 精确值优先、预热语义和速度公式。
- 不加入真实 Key、接口、账号、日志、报告或带个人信息的截图。
- 不在测试中访问真实模型 API 或产生费用。
- UI 变化需检查三套浅色主题、`#17191d` 暗色主题、桌面与窄屏。

本地验证：

```powershell
python -m pip install -r requirements-dev.txt
ruff format --check .
ruff check .
python -m pytest
python -m compileall -q stress_tool scripts tests benchmark_token_speed.py token_stress_test.py
```

Bug fixes, measurement reviews, documentation, and accessibility improvements are welcome. Preserve the locked methodology and privacy boundary, never call a live model from tests, and include focused tests for behavioral changes.
