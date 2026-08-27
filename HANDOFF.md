# Hermes Context Handoff 开发交接

> 交接时间：2026-08-27
>
> 交接边界：P7 隔离发布候选已完成，Profile 上线待授权
>
> 下一阶段：确认 Policy、执行受控 Profile 上线与观察

## 1. Task

- 仓库：`https://github.com/Chrischenny/chris-hermes-agent`
- 本地目录：`/home/chen/code/chris-hermes-plugin`
- 分支：`main`
- P0 实现提交：`25f2261 feat: scaffold Hermes context handoff plugin`
- P1 实现提交：`a3993fe feat: add model handoff policy resolver`
- P2 实现提交：`1b30c38 feat: persist task lifecycle and resume state`
- P3 实现提交：`bfb862e feat: add runtime context status observation`
- P4 实现提交：`5b9d06f feat: add atomic context rotation`
- P5 实现提交：`bd71a47 feat: add task isolation handoff workflow`
- P6 实现提交：`d2a9046 feat: add emergency context fallback`
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
8. 未获得用户明确上线授权前，不得修改、安装或重启正在运行的
   `chris-avatar`。

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
- Resume 先更新持久化 Active Pointer，并明确返回
  `context_rotation_applied: false`、`context_rotation_required: true` 和
  `next_required_action: call_handoff_context`；随后由显式 Handoff 调用完成
  Provider Request Context Rotation。

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

### 4.6 Context Rotation 边界

- Hermes 在执行 ContextEngine Tool Handler 前已经把当前 assistant Tool Call
  写入 canonical messages，Tool Result 会在 Handler 返回后追加；因此新 Segment
  的 `start_message_index` 指向该 assistant 消息；
- `select_context()` 保留 Hermes 已组装的稳定 Prefix，只在本次 Provider Request
  中加入 Checkpoint Bootstrap 和新 Segment Tail，不修改 Session History；
- Handler 只接受消息尾部当前 assistant 中的 `handoff_context`，不得向前扫描并
  误用历史 Tool Call；
- ContextEngine 只旋转当前 Active Task；新任务、子任务与 Resume 先由 Skill/Task
  层完成持久化 Active Pointer 编排，再显式 Rotation；
- 普通 Compression 继续关闭，只有显式 Emergency Policy 能启用 Delegate。

### 4.7 Agent 工作流与任务隔离

- 任务分类保留在 bundled Skill，不进入 ContextEngine；
- 分类结果为 continuation、subtask、new_task 或 ambiguous；会改变持久化状态且
  置信度不足时必须先向用户确认；
- 子任务只允许按项继承父 Task 的 constraints、decisions 和 artifacts，并记录
  `parent_task_id`；
- 新任务不继承父 Task 状态；新任务和子任务均需创建目标 Task 自己的 Checkpoint；
- 旧 Task Checkpoint、目标 Task 创建、目标 Checkpoint 和显式
  `handoff_context` 共同构成隔离流程；
- Resume 更新 Pointer 后仍必须显式 Rotation，不能把 Task 激活当作 Context 已切换；
- `soul/SOUL-snippet.md` 只引用 Runtime Status 中当前模型的 Policy，不包含固定
  模型、Token 或比例阈值。

### 4.8 Emergency Fallback 边界

- `threshold_tokens` 只反映当前模型显式启用的 Emergency 阈值；甜区仍只用于
  Agent 主动 Handoff 决策；
- Host 按包含 Tool Schema 的 Request Pressure 调用 `should_compress()`，插件只在
  已绑定 Active Task/Segment、已有 Request Snapshot 且阈值到达时返回 `True`；
- 完整 Active Request 先归档到 Profile 插件数据目录，再调用 Hermes 原生
  `ContextCompressor`，不复制其压缩算法；
- Delegate 结果重新估算并要求严格低于同一 Emergency 阈值；失败、无进展和仍
  超限都 fail closed；
- 成功结果只缓存为下一次 `select_context()` 的 Request Selection；`compress()`
  向 Host 返回原 canonical conversation 对象，阻止 Session Boundary Rewrite；
- 归档同时保存压缩前和压缩后消息、Conversation Anchor 和 SHA-256 Checksum，
  重启后恢复压缩 Selection 并追加 Anchor 后的新消息；
- 归档目录/文件权限为 `0700/0600`，文件名为随机 UUID；Event 只包含引用、
  Checksum、Token 计数和安全错误码，不包含 Context 或 Provider 异常详情。

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

P4 已完成：

- 将版本更新到 `0.4.0`；
- 新增 `HandoffService`，校验 Checkpoint 所属关系、Checksum、目标 Task 状态和
  Expected Active Task/Segment；
- 在单个 `BEGIN IMMEDIATE` 事务中关闭旧 Segment、创建新 Segment、CAS 更新
  Session Pointer 并追加 `HANDOFF_COMPLETED`；
- 启用 ContextEngine 的 `handoff_context` Handler，并只接受当前消息尾部的
  assistant Tool Call；
- 新增完整 Checkpoint Bootstrap，保留稳定 Prefix、Handoff Tool Call/Result 和
  Handoff 后消息，同时排除旧 Segment Tool Trace；
- Context 选择保持 request-only，不删除或改写 Hermes Session History；
- Checkpoint/游标损坏时返回隔离的诊断 Bootstrap，不重新引入旧 Trace；
- 覆盖事务回滚、真实双连接并发、重复调用、同 Turn Tool Loop、进程重启和
  Tool Pairing；
- 更新版本、README、bundled Skill 状态和开发计划并提交 P4。

P5 已完成：

- 将插件版本更新到 `0.5.0`，bundled Skill 版本更新到 `0.2.0`；
- 将 P4 状态 Stub 重写为完整 Agent 工作流，覆盖任务对账、普通 Handoff 决策、
  Checkpoint 前置检查、Rotation 结果校验和同 Turn 恢复；
- 新增 `checkpoint-template.md`、`new-task-detection.md` 和
  `task-state-rules.md` 三个按需加载的参考文档；
- 明确 continuation、subtask、new_task、ambiguous 四类语义及低置信度确认边界；
- 明确子任务只按项继承 constraints、decisions 和 artifacts，禁止继承父 Task
  Checkpoint、Event、Session、Segment、conversation 和 Tool Trace；
- 明确新任务和子任务均先保存旧 Task，再创建目标 Task 自己的 Checkpoint，最后
  使用目标 Task/Segment 显式调用 `handoff_context`；
- 明确 Resume 返回 `context_rotation_required: true` 后必须继续显式 Rotation；
- 新增 `soul/SOUL-snippet.md`，只引用当前 Runtime Policy，不写固定阈值；
- 新增任务延续、子任务继承、完全新任务隔离和 Resume Rotation 集成测试；
- 更新 README 和开发计划并提交 P5。

P6 已完成：

- 将插件版本更新到 `0.6.0`，bundled Skill 版本更新到 `0.3.0`；
- 新增 `emergency.py`，实现 not_triggered/triggered/completed/failed 状态机；
- 将 Host Compression 阈值从普通 Handoff 甜区改为显式 Emergency 阈值；
- 在 Delegate 调用前安全归档完整 Active Request，并将引用绑定到 Context Segment；
- 复用 Hermes `ContextCompressor`，重新验证压缩结果低于配置阈值；
- 新增 Triggered/Completed/Failed Event，所有 Event Payload 均不含 Context；
- 成功时保持 canonical Session History 原对象和原内容，只替换后续 Request
  Selection；
- 支持重启恢复、Conversation Anchor 后 Tail 追加、损坏归档拒绝和重复触发阻止；
- Runtime Status 展示 Emergency 状态，Skill 要求成功后尽快补建正式
  Checkpoint/Handoff；
- 覆盖成功、异常、无进展、仍超限、归档损坏、存储失败、模型/Policy 切换和
  重启恢复。

## 6. Current State

P7 隔离发布候选已完成，`chris-avatar` Profile 上线仍未执行：

- `ContextHandoffEngine` 持有当前 `policy_resolution`；
- `threshold_tokens` 只反映已匹配且显式启用的 Emergency 阈值；
- 无配置或无效配置时 `observation_only=True` 且阈值清零；
- `should_compress()` 在无 Policy、阈值未到、无 Active Pointer、状态已完成/失败或
  Delegate 不可用时 fail closed；
- `compress()` 归档并压缩 Active Request，但始终把 canonical conversation 原对象
  返回 Host，避免 Hermes Session Rewrite；
- `emergency.py` 保存完整压缩前/后 Request、Anchor、状态与 Checksum；
- Profile 归档位于 `plugin-data/chris-hermes-agent/archives/`，使用随机文件名和
  `0700/0600` 权限；
- Runtime Status 展示 Emergency 的 not_triggered/triggered/completed/failed 状态；
- Delegate 结果必须重新估算并低于用户阈值，否则记录安全失败码；
- 完成后的 Request Selection 可在 Gateway/Engine 重启后从归档恢复；
- 初始 Segment 仍使用完整 conversation；带 Checkpoint 的 Active Segment 会使用
  Checkpoint Bootstrap 和 `start_message_index` 之后的新 Tail；
- `select_context()` 每次返回新的 Request 列表，并在尾部加入一条最新 Runtime
  Status；
- 原消息列表、System Prompt、稳定 Prefix 和 Hermes Session History 不被修改；
- Runtime Status 包含模型、Context Limit、估算/真实 Token、Policy 来源与阈值、
  Active Task/Segment 和 Emergency 状态；
- `estimated_prompt_tokens` 使用 Hermes 消息粗估接口并包含 Runtime Status 自身；
- `last_prompt_tokens` 只代表最近一次 Provider 真实 Usage；缺失时明确不可用；
- `task_state_manage`、`task_event_append`、`checkpoint_create` 已可用；
- Repository 在首次 Task Tool 调用或绑定 Session 后的首次 Runtime Status 读取时
  惰性创建，路径为当前 Profile 的
  `plugin-data/chris-hermes-agent/data.db`；
- Resume 会更新持久化 Task/Segment/Session Pointer，并明确要求下一步调用
  `handoff_context`；
- `handoff_context` 已可用，成功结果包含新 Segment、Checkpoint、Task、Next
  Actions 和 `handoff_applied: true`；
- Handoff 仍只允许目标 Task 等于当前 Active Task；bundled Skill 会先创建或恢复
  目标 Task 并更新 Active Pointer，再使用目标 Checkpoint 执行 Rotation；
- `skills/context-handoff/SKILL.md` 已包含 P6 Emergency 后正式 Handoff 工作流；
- 当前任务延续不会创建新 Task 或 Rotation；
- 子任务会记录 `parent_task_id`，只继承显式选择的白名单状态；
- 独立新任务不继承旧任务结构化状态，Rotation 后也不包含旧 User History 或
  Tool Trace；
- 低置信度分类和多个相近 Resume 候选会在改变状态前要求用户确认；
- `soul/SOUL-snippet.md` 已准备好但尚未合并到 `chris-avatar/SOUL.md`；
- 普通路径不会调用 Hermes 默认 Compression；显式 Emergency 路径委托给 Hermes
  原生 `ContextCompressor`；
- 不会修改 Hermes Session；
- 未安装到 `chris-avatar`。
- 连续 10 次 Rotation、Engine 中途重建、稳定 Prefix 和 Segment 父链已验证；
- 真实 Hermes Host 已验证 Tool Schema Request Pressure、Emergency
  no-progress 边界、canonical Session 不变、Emergency 后正式 Handoff 和独立
  进程恢复；
- 标准 Hermes 安装器、installed-path Doctor、Profile 配置注入、ContextEngine
  选择和模型/Policy 切换已在临时 Profile 通过；
- 当前 Hermes 加载器支持 Manifest v2，但安装器仍只接受 v1，因此 P7 发布包锁定
  `manifest_version: 1` 并保留扩展配置字段；
- `scripts/chris-avatar-rollout.sh` 提供带 Checksum/SQLite 完整性校验的备份和
  一条命令回滚，回滚不会删除插件 SQLite 或 Emergency 归档；
- `docs/P7-chris-avatar-runbook.md` 定义预检、安装、配置、SOUL 迁移、观察、
  回滚和至少 30 天的保留策略。

关键文件：

- `plugin.yaml`
- `__init__.py`
- `chris_hermes_agent/plugin.py`
- `chris_hermes_agent/context_engine.py`
- `chris_hermes_agent/context_builder.py`
- `chris_hermes_agent/emergency.py`
- `chris_hermes_agent/handoff_service.py`
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
- `skills/context-handoff/references/checkpoint-template.md`
- `skills/context-handoff/references/new-task-detection.md`
- `skills/context-handoff/references/task-state-rules.md`
- `soul/SOUL-snippet.md`
- `docs/P7-chris-avatar-runbook.md`
- `scripts/chris-avatar-rollout.sh`
- `tests/e2e/test_p7_host_workflow.py`
- `tests/e2e/test_p7_rollout_script.py`
- `tests/contract/`
- `tests/unit/test_policy.py`
- `tests/unit/test_context_builder.py`
- `tests/unit/test_handoff_bootstrap.py`
- `tests/unit/test_handoff_service.py`
- `tests/unit/test_token_usage.py`
- `tests/unit/test_store.py`
- `tests/unit/test_task_service.py`
- `tests/unit/test_emergency.py`
- `tests/integration/test_task_tools.py`
- `tests/integration/test_runtime_status.py`
- `tests/integration/test_context_rotation.py`
- `tests/integration/test_task_isolation.py`
- `tests/integration/test_emergency_fallback.py`
- `tests/integration/test_plugin_doctor.py`
- `pyproject.toml`

## 7. Verification Evidence

P7 隔离发布候选验证结果：

- 116 个单元/契约/集成/E2E 测试全部通过；
- 总覆盖率 86.94%，超过 80% 门槛；
- Ruff format/check 通过；
- Mypy strict 通过；
- `uv build` 通过；
- `hermes plugins doctor . --ci` 通过且无警告；
- Plugin Doctor 在临时 `HERMES_HOME` 中运行，没有触碰 `chris-avatar`。
- 标准插件安装、启用、配置和 installed-path Runtime 选择在临时 Profile 通过；
- 备份和一条命令回滚在临时 Profile 通过；
- Hermes Skill Linter 无 Error；仅保留一个 `license` 未声明的 advisory warning，
  因仓库当前没有授权文件，本阶段不代替用户指定许可证；
- `skill-creator` 的 Codex 专用 `quick_validate.py` 不接受 Hermes 标准顶层
  `version` 和 `author` 字段，因此不作为此 Hermes Skill 的权威校验；Hermes
  Skill Linter 与 Plugin Doctor 均已通过。

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
PYTHONPATH="$HOME/.hermes/hermes-agent" \
  python3 -m tools.skill_linter skills/context-handoff
```

## 8. Known Issues / Expected Limitations

- Policy 数值尚未写入 `chris-avatar`，这是上线前的用户配置项；
- 当前 Request Token 是 Hermes 的消息级粗估值；Tool Schema Token 不在
  `select_context()` 的参数中，因此不包含在此字段内；
- 上一 Response 真实 Usage 会滞后一轮，这是设计中的校准数据；
- Resume Tool 不直接执行 Rotation；它返回明确的下一步，Agent 必须再调用
  `handoff_context`；
- 搜索是 Profile 内的结构化/词法召回，不包含远程 Embedding；
- Emergency 归档包含完整 Active Request，可能含敏感信息；权限和 Checksum 已
  收紧，P7 runbook 要求观察期全部保留、验收后至少保留 30 天，清理必须另行
  获得用户决定；
- Emergency 成功后 canonical Session 保持完整，后续请求使用归档中的压缩
  Selection；Agent 仍需尽快补建正式 Checkpoint 并执行普通 Handoff；
- `select_context()` 的消息估算不含 Tool Schema Token；Hermes Host 传给
  `should_compress()` 的 Request Pressure 才是 Emergency 触发的权威输入；
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

严格按以下顺序完成剩余 P7：

1. 获取用户对 `gpt-5.6-sol` Handoff 类型/数值、Emergency 开关/阈值的确认；
2. 提交并推送 0.7.0 发布候选，记录用于安装的不可变 40 位 Commit SHA；
3. 再次向用户取得修改 `chris-avatar` 和重启 Gateway 的明确上线授权；
4. 按 `docs/P7-chris-avatar-runbook.md` 记录预检并执行受保护备份；
5. 安装固定 SHA、写入确认后的 Policy、迁移 SOUL、启用插件并通过配置检查；
6. 重启 Gateway，用隔离 Session 观察日志、Token、Event、Segment 和 Archive；
7. 完成验收或运行一条命令回滚；无论哪条路径都保留插件 SQLite 和 Emergency
   诊断归档；
8. 记录真实上线证据，将 P7 标记为最终完成并推送交接文档。

## 11. P7 Acceptance Criteria

- P6 的显式 Policy、归档、Delegate、验证、恢复和 canonical History 约束在真实
  Hermes Host 中成立；
- 普通路径优先 Handoff，Emergency 后能尽快补建正式 Checkpoint/Rotation；
- 连续至少 10 次 Rotation、长 Tool Loop、模型切换和 Gateway 重启通过；
- Runtime Status、Task/Event/Checkpoint/Segment 与 Archive 可交叉追溯；
- `chris-avatar` 的 Policy 数值由用户确认，不引入代码/SOUL 默认阈值；
- 上线前完成备份，出现异常时能一键恢复 Hermes 默认 Compressor 和原配置；
- 插件异常不得静默改写或破坏 Hermes Session；
- 上线变更需得到用户明确授权。
