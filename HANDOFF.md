# Hermes Context Handoff 开发交接

> 交接时间：2026-08-26
>
> 交接边界：P0 已完成，P1 尚未开始
>
> 下一阶段：P1 配置与 Policy Resolver

## 1. Task

- 仓库：`https://github.com/Chrischenny/chris-hermes-agent`
- 本地目录：`/home/chen/code/chris-hermes-plugin`
- 分支：`main`
- P0 实现提交：`25f2261 feat: scaffold Hermes context handoff plugin`
- 目标 Hermes Profile：`chris-avatar`

## 2. Goal

为 Hermes 长时间 Coding 任务实现由 Agent 主动管理的 Context 生命周期：

- 不依赖正常路径下的 Hermes 默认 Compression；
- Agent 按用户为当前模型配置的 Context 甜区决定 Handoff 时机；
- 使用 Task State、Event Log 和 Checkpoint 保存可恢复状态；
- 在同一 Agent Turn 内完成 Active Context Rotation；
- 保留完整历史的可追溯性；
- 支持新任务隔离和显式配置的 Emergency Fallback。

需求和完整阶段计划分别见：

- `Hermes 长任务 Context Handoff 方案.md`
- `Hermes 长任务 Context Handoff 开发计划.md`

## 3. Confirmed Constraints

1. Hermes 可以升级，最低支持版本为 `v2026.8.19`。
2. Task State、Event Log、Checkpoint 和 Context Segment 由本插件实现。
3. 后续允许修改 `chris-avatar` 的 `SOUL.md`。
4. 第一版必须包含新任务/子任务识别和 Emergency Fallback。
5. 不允许在代码或 SOUL 中硬编码跨模型通用甜区。
6. 普通 Handoff 时机由用户配置和 Agent 当前任务状态共同决定。
7. 没有匹配当前模型的 Policy 时，只报告 Context 使用事实，不猜测阈值。
8. 当前阶段不得修改、安装或重启正在运行的 `chris-avatar`。

## 4. Architecture Decisions

### 4.1 一个插件发布包，内部职责分离

当前结构是有意设计，不是把 ContextEngine 业务逻辑写入 Plugin：

```text
plugin.py
  └── 只负责注册 ContextEngine、Task Tools 和 Skill

context_engine.py
  └── 只负责 Hermes ContextEngine 契约和 handoff_context

task_tools.py
  └── 只负责 Task 层工具 Schema/Handler
```

使用普通 Native Plugin 的 `ctx.register_context_engine()` 是 Hermes 官方支持的方式。
不采用独立 `plugins/context_engine/<name>` 发布单元，原因是本项目还需要普通
Task Tools 和 bundled Skill；拆成两个插件会增加版本、安装和数据契约维护成本。

### 4.2 工具职责

- 普通插件注册：
  - `task_state_manage`
  - `task_event_append`
  - `checkpoint_create`
- ContextEngine 自身暴露：
  - `handoff_context`

`plugin.yaml.provides_tools` 只列三个通过 `ctx.register_tool()` 注册的工具。
`handoff_context` 由 Hermes 在选中 ContextEngine 后注入，不能列入该 Manifest
字段，否则 `hermes plugins doctor` 会产生声明与注册不一致警告。

### 4.3 Policy 原则

计划支持：

- `ratio`
- `absolute_tokens`

匹配顺序：

```text
精确模型名
→ 最长模型名模式匹配
→ Provider 级策略
→ default_policy
```

示例阈值只属于文档示例，不能成为代码默认值。`gpt-5.6-sol` 的真实甜区和
Emergency 阈值将在上线前由用户配置，不阻塞 P1 开发。

## 5. Completed

P0 已完成：

- 创建 Hermes Native Plugin Manifest 和入口；
- 创建 Python 包；
- 注册 `ContextHandoffEngine`；
- 注册三个 Task Tools；
- 注册 `context-handoff` Skill；
- 建立 pytest、pytest-cov、ruff、mypy 和 uv 环境；
- 建立 Hermes ABC、Tool Schema、插件注册和 Plugin Doctor 契约测试；
- 修复 Hermes 隔离包命名空间下必须使用相对导入的问题；
- 更新 README 和开发计划；
- 提交并推送 P0。

## 6. Current State

P0 实现处于安全关闭状态：

- `ContextHandoffEngine.should_compress()` 恒为 `False`；
- `compress()` 严格原样返回输入消息列表；
- 没有覆盖 `select_context()`，因此 Hermes 跳过 Context 选择 Hook；
- 三个 Task Tool 均返回结构化 `phase_not_ready`；
- `handoff_context` 返回结构化 `phase_not_ready`；
- 不会创建数据库；
- 不会写 Task State；
- 不会切换 Context；
- 不会修改 Hermes Session；
- 未安装到 `chris-avatar`。

关键文件：

- `plugin.yaml`
- `__init__.py`
- `chris_hermes_agent/plugin.py`
- `chris_hermes_agent/context_engine.py`
- `chris_hermes_agent/task_tools.py`
- `skills/context-handoff/SKILL.md`
- `tests/contract/`
- `tests/integration/test_plugin_doctor.py`
- `pyproject.toml`

## 7. Verification Evidence

P0 最终验证结果：

- 16 个契约/集成测试全部通过；
- 代码覆盖率 100%；
- Ruff format/check 通过；
- Mypy strict 通过；
- `uv build` 通过；
- `hermes plugins doctor . --ci` 通过且无警告；
- Plugin Doctor 在临时 `HERMES_HOME` 中运行，没有触碰 `chris-avatar`。

复验命令：

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy chris_hermes_agent
HERMES_AGENT_ROOT="$HOME/.hermes/hermes-agent" \
  uv run pytest --cov=chris_hermes_agent --cov-report=term-missing
hermes plugins doctor . --ci
```

## 8. Known Issues / Expected Limitations

- Task Tools 和 Handoff Tool 目前有意不可用，不是缺陷；
- Policy 配置尚未实现；
- Runtime Status 尚未实现；
- SQLite 和 Task State 尚未实现；
- Context Rotation 尚未实现；
- Emergency Fallback 尚未实现；
- `chris-avatar` 仍使用 Hermes 默认 ContextEngine；
- `chris-avatar/SOUL.md` 尚未迁移。

## 9. Rejected Alternatives

### 固定 50%/70%/90% 阈值

已拒绝。不同模型的 Context 甜区不同，必须由用户按模型配置。

### 把阈值继续写在 SOUL.md

已拒绝。SOUL 只保存“读取当前模型 Policy”的长期规则，具体数值进入 Profile
配置。

### ContextEngine 与 Task Plugin 拆成两个发布包

当前拒绝。虽然技术上可行，但会增加部署、版本同步和共享持久化契约复杂度。
只有未来 ContextEngine 需要完全独立复用时再评估拆分。

### 使用纯 `plugins/context_engine/<name>` 目录承载全部功能

已拒绝。该加载路径主要面向 ContextEngine，不能自然承载本项目需要的普通
Task Tools 和 bundled Skill；会迫使 Task 语义进入 ContextEngine 或引入第二个插件。

## 10. Next Actions

新会话从 P1 开始，建议严格按以下顺序：

1. 阅读本交接文件和开发计划 P1；
2. 检查 `git status`，确认 `main` 与 `origin/main` 同步；
3. 使用 `documentation-lookup` 核对 Hermes `ctx.get_config()`、
   `ContextEngine.update_model()` 和插件设置 Schema；
4. 使用 `tdd-workflow`，先编写 Policy Resolver 测试；
5. 定义不可变 Policy 数据模型和错误模型；
6. 实现：
   - `ratio` Policy；
   - `absolute_tokens` Policy；
   - 精确模型匹配；
   - 最长模型模式匹配；
   - Provider Policy；
   - `default_policy`；
   - 未匹配和无效配置 fail closed；
7. 在 `register(ctx)` 中读取插件私有配置并注入 ContextEngine；
8. 测试模型切换时重新解析 Policy；
9. 运行完整 P0 回归、覆盖率、类型检查和 Plugin Doctor；
10. 更新计划进度，提交并推送 P1。

## 11. P1 Acceptance Criteria

- 不同模型可以解析出不同 Policy；
- 支持 ratio 和 absolute_tokens；
- 匹配优先级确定且有测试；
- 无匹配 Policy 时只观测，不启用 Handoff/Emergency；
- 无效 Policy fail closed，并返回可诊断错误；
- 没有任何硬编码甜区；
- 模型切换重新解析 Policy；
- P0 全部测试继续通过；
- 不修改或重启 `chris-avatar`。
