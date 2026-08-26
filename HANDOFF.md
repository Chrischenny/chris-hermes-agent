# Hermes Context Handoff 开发交接

> 交接时间：2026-08-26
>
> 交接边界：P3 已完成，P4 尚未开始
>
> 下一阶段：P4 Context Rotation

## 1. Task

- 仓库：`https://github.com/Chrischenny/chris-hermes-agent`
- 本地目录：`/home/chen/code/chris-hermes-plugin`
- 分支：`main`
- P0 实现提交：`25f2261 feat: scaffold Hermes context handoff plugin`
- P1 实现提交：`a3993fe feat: add model handoff policy resolver`
- P2 实现提交：`1b30c38 feat: persist task lifecycle and resume state`
- P3 实现提交：`bfb862e feat: add runtime context status observation`
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

已支持：

- `ratio`
- `absolute_tokens`

匹配顺序：

```text
精确模型名
→ 最长模型名模式匹配
→ Provider 级策略
→ default_policy
```

模型模式沿用 Hermes 的最长子串语义；等长模式同时命中时视为歧义并 fail
closed。示例阈值只属于文档示例，不能成为代码默认值。`gpt-5.6-sol` 的真实
甜区和 Emergency 阈值将在上线前由用户配置，不阻塞后续开发。

### 4.4 Task 暂存、搜索和恢复

- Task 属于当前 Profile，可跨 Hermes Session 搜索和恢复；
- 新任务开始时，尚未完成的当前任务必须先有有效 Checkpoint，然后默认进入
  `paused`；
- Task 状态为 active、paused、blocked、completed、cancelled；
- 搜索文档包含 Task State、Checkpoint 和选择性 Decision Event，不包含大段
  Tool Trace；
- 优先使用 FTS5 trigram 支持中文子串，运行环境不支持时回退到规范化字符串
  匹配；
- P2 Resume 只更新持久化 Active Pointer，并明确返回
  `context_rotation_applied: false`；真正的 Provider Context Rotation 在 P4。

### 4.5 Runtime Status 与 Token 观测

- 使用 Hermes `estimate_messages_tokens_rough()` 估算当前消息 Request；
- Runtime Status 是尾部临时 `user` 消息，只存在于 `select_context()` 返回的新
  Request 列表；
- 重复选择已经带有插件 Runtime Status 的 Request 时替换尾部状态，不累积；
- Provider Usage 同时兼容 legacy 和 canonical 字段；
- 没有 Usage 回调时，下一次 Request 将真实值标为 `unavailable`，不会沿用旧值；
- ContextEngine 和 Task Tools 共享同一个 Profile Repository，并在每次 Request
  查询当前 Session 的 Active Task/Segment Pointer；
- 模型切换会清除旧模型 Usage，并立即展示新模型 Policy 和 Context Limit。

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

P1 已完成：

- 使用 Manifest v2 `config_schema` 声明私有 `handoff` 配置；
- 在 `register(ctx)` 中通过 `ctx.get_config("handoff")` 读取 Profile 设置；
- 定义不可变 `Threshold`、`HandoffPolicy`、`PolicyResolution` 和
  `PolicyError`；
- 支持 `ratio` 和 `absolute_tokens`；
- 实现精确模型、最长模型子串、Provider 和 default_policy 四级匹配；
- 校验未知字段、缺失字段、类型、取值范围、Context Limit 和 Emergency
  顺序；
- 未匹配、无效和歧义配置均 fail closed，并保留结构化诊断；
- 覆盖 `ContextHandoffEngine.update_model()`，在初始模型、模型切换和
  fallback 时重新解析策略；
- 保持 Compression、Task Tools 和 Context Rotation 安全关闭；
- 更新 README 和开发计划并提交 P1。

P2 已完成：

- 将版本更新到 `0.2.0`；
- 使用 Hermes `plugins.plugin_storage.plugin_db()` 在首次 Task Tool 调用时惰性
  创建当前 Profile 数据库；
- 建立 Schema v1、版本检查、WAL、外键、busy timeout 和幂等迁移；
- 实现 Task、Event、Checkpoint、Context Segment 和 Session Context State；
- 实现事务回滚、Task/Session 乐观锁和并发更新保护；
- 实现 Checkpoint 完整字段、非空 Next Actions、SHA-256 Checksum 和损坏拒绝；
- 实现 active/paused/blocked/completed/cancelled 状态；
- 新任务默认暂存未完成的当前任务；
- 实现中文自然语言检索、精简候选和跨 Session Resume；
- 将三个 Task Tool 从 `phase_not_ready` 切换为真实 Handler；
- 调整 `handoff_context` 契约，区分目标 Task 与切换前 Active Task/Segment；
- 更新方案、README 和开发计划并提交 P2。

P3 已完成：

- 将版本更新到 `0.3.0`；
- 新增不可变 `ProviderTokenUsage`，规范化 Hermes legacy/canonical Usage；
- 新增 `RuntimeStatus`、格式化和自洽 Request Token 估算；
- 覆盖 `select_context()`，只在 Request 浅拷贝尾部追加一条 Runtime Status；
- 实现无 Usage 失效检测，避免把缺失值或旧值伪装成当前真实 Usage；
- 实现重复选择去重和稳定 Prefix 保留；
- 在 Session 生命周期中恢复 P2 Active Task/Segment Pointer；
- 覆盖 Tool Loop、Retry、模型切换、无 Usage、无/无效 Policy 和 Session Reset；
- 保持普通 Compression 和 Provider Context Rotation 安全关闭；
- 更新版本、README 和开发计划并提交 P3。

## 6. Current State

P3 已启用 Runtime 观测，Context Rotation 执行面仍处于安全关闭状态：

- `ContextHandoffEngine` 持有当前 `policy_resolution`；
- `threshold_tokens` 只反映已匹配且有效的普通 Handoff 起点；
- 无配置或无效配置时 `observation_only=True` 且阈值清零；
- `ContextHandoffEngine.should_compress()` 恒为 `False`；
- `compress()` 严格原样返回输入消息列表；
- `select_context()` 每次返回带有一条最新 Runtime Status 的新 Request 列表；
- 原消息列表、System Prompt、稳定 Prefix 和 Hermes Session History 不被修改；
- Runtime Status 包含模型、Context Limit、估算/真实 Token、Policy 来源与阈值、
  Active Task 和 Segment；
- `estimated_prompt_tokens` 使用 Hermes 消息粗估接口并包含 Runtime Status 自身；
- `last_prompt_tokens` 只代表最近一次 Provider 真实 Usage；缺失时明确不可用；
- `task_state_manage`、`task_event_append`、`checkpoint_create` 已可用；
- Repository 在首次 Task Tool 调用或绑定 Session 后的首次 Runtime Status 读取时
  惰性创建，路径为当前 Profile 的
  `plugin-data/chris-hermes-agent/data.db`；
- Resume 会更新持久化 Task/Segment/Session Pointer，但明确报告 Provider
  Context 尚未 Rotation；
- `handoff_context` 返回结构化 `phase_not_ready`；
- 不会切换 Provider Context；
- 不会修改 Hermes Session；
- 未安装到 `chris-avatar`。

关键文件：

- `plugin.yaml`
- `__init__.py`
- `chris_hermes_agent/plugin.py`
- `chris_hermes_agent/context_engine.py`
- `chris_hermes_agent/context_builder.py`
- `chris_hermes_agent/token_usage.py`
- `chris_hermes_agent/models.py`
- `chris_hermes_agent/errors.py`
- `chris_hermes_agent/policy.py`
- `chris_hermes_agent/task_models.py`
- `chris_hermes_agent/migrations.py`
- `chris_hermes_agent/store.py`
- `chris_hermes_agent/task_service.py`
- `chris_hermes_agent/checkpoint_service.py`
- `chris_hermes_agent/task_tools.py`
- `skills/context-handoff/SKILL.md`
- `tests/contract/`
- `tests/unit/test_policy.py`
- `tests/unit/test_context_builder.py`
- `tests/unit/test_token_usage.py`
- `tests/unit/test_store.py`
- `tests/unit/test_task_service.py`
- `tests/integration/test_task_tools.py`
- `tests/integration/test_runtime_status.py`
- `tests/integration/test_plugin_doctor.py`
- `pyproject.toml`

## 7. Verification Evidence

P3 最终验证结果：

- 74 个单元/契约/集成测试全部通过；
- 总覆盖率 87.66%，超过 80% 门槛；
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
uv build
hermes_temp_dir=$(mktemp -d)
HERMES_HOME="$hermes_temp_dir" hermes plugins doctor . --ci
```

## 8. Known Issues / Expected Limitations

- Handoff Tool 目前有意不可用，不是缺陷；
- Policy 数值尚未写入 `chris-avatar`，这是上线前的用户配置项；
- 当前 Request Token 是 Hermes 的消息级粗估值；Tool Schema Token 不在
  `select_context()` 的参数中，因此不包含在此字段内；
- 上一 Response 真实 Usage 会滞后一轮，这是设计中的校准数据；
- Task Resume 尚未连接 Provider Context Rotation；
- 搜索是 Profile 内的结构化/词法召回，不包含远程 Embedding；
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

新会话从 P4 开始，建议严格按以下顺序：

1. 阅读本交接文件和开发计划 P4；
2. 检查 `git status`，确认 `main` 与 `origin/main` 同步；
3. 使用 `documentation-lookup` 再核对 Hermes 当前 Request 消息游标、Tool
   Call/Result Sanitizer 和 ContextEngine Tool Handler 的运行时 kwargs；
4. 使用 `tdd-workflow`，先编写 Handoff 状态机和 Context Bootstrap 失败测试；
5. 实现 `handoff_context` 的参数、Checkpoint 所属关系/Checksum 和 Expected
   Active Pointer 校验；
6. 在单个 SQLite 事务中关闭旧 Segment、创建新 Segment、更新 Pointer 并记录
   `HANDOFF_COMPLETED`；
7. 让下一次 `select_context()` 检测 Segment 变化，并用 Checkpoint 构造新的
   Active Context；
8. 保留 Hermes System/Prefill 稳定头、触发 Handoff 的 assistant tool call 与
   对应 tool result，以及 Handoff 后新增消息；
9. 排除旧 Segment 的大量 Tool Trace，同时保留完整 Hermes Session History；
10. 测试同一 Agent Turn 连续执行、并发/重复 Handoff、Provider Retry、进程重启、
    Checkpoint 损坏和 Tool Pairing；
11. 运行完整 P0/P1/P2/P3 回归、覆盖率、类型检查、构建和 Plugin Doctor；
12. 更新计划进度，提交并推送 P4。

## 11. P4 Acceptance Criteria

- `handoff_context` 只接受属于目标 Task 的完整、Checksum 有效 Checkpoint；
- Expected Active Task/Segment 不一致时 fail closed，不产生部分状态；
- Segment 关闭、新 Segment、Pointer 和 Event 在一个事务中原子提交；
- 成功结果包含新 Segment、Checkpoint、Task、Next Actions 和
  `handoff_applied: true`；
- 下一次 Provider Request 使用新 Context，且无需等待下一条用户消息；
- 新 Context 保留 System/SOUL/Skills/Memory 等 Hermes 稳定头；
- 触发 Handoff 的 Tool Call/Result 保持成对，不产生孤立 Tool Message；
- 旧 Segment 大量 Tool Trace 不进入新 Request，但原始 Session History 不删除；
- Runtime Status 继续只存在于 Request 尾部，并显示新的 Active Segment；
- 并发或重复 Handoff 不得产生双 Segment 或重复 Event；
- `should_compress()` 继续恒为 `False`，普通路径不触发默认 Compression；
- P0/P1/P2/P3 全部测试继续通过；
- 不修改或重启 `chris-avatar`。
