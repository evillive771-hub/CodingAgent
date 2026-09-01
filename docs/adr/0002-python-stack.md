# ADR-0002：采用 Python 实现

- 状态：接受
- 日期：2026-09-01

## 决策

使用 Python 异步实现，核心依赖 Pydantic、HTTPX、Typer 和 pytest；交付集成使用 SQLAlchemy、PostgreSQL、Alembic 和 Playwright。依赖由 uv 管理，缓存和虚拟环境位于 D 盘。

## 原因

Python 能以较少样板代码实现异步 Agent Loop、模型适配和测试；Pydantic 可以承担工具和任务合同的运行时边界校验。架构模式不依赖 Claude Code 的 TypeScript 实现语言。

## 后果

所有 I/O 端口采用 `async`，阻塞 Git/文件库必须通过受控适配器或线程桥接。类型检查与运行时 Schema 校验同时保留。

