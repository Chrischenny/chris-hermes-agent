# Hermes Context Handoff 开发交接

> 交接时间：2026-09-04
>
> 交接边界：P7 `0.7.7` 已部署到 `chris-avatar`；Session/Task 所有权、首轮
> Handoff 前边界、Segment 锚点、异常降级与 Emergency archive 恢复均已加固，
> 并完成真实长会话只读回放。
>
> 下一阶段：继续真实任务，观察首次由 0.7.7 创建的带 checksum Segment，以及
> 新旧会话并行操作同一 Task 时的 fail-closed 行为

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
- P7 上线安全修复：`2927a05 fix: avoid vulnerable SQLite WAL mode`
- P7 状态权限修复：`5adc9dc fix: restrict plugin state permissions`
- P7 真机 Schema 修复：`cef29f6 fix: expose durable state tool schemas`
- P7 Deferred Tool 包装加固：`4773e31 fix: document deferred task tool wrapper`
- P7 Checkpoint 语义加固：`6c1e497 fix: clarify rejected alternative semantics`
- P7 跨会话 continuation 加固：`4e3a2c8 fix: prevent duplicate continuation task forks`
- P7 Context 状态边界加固：`27835c1 fix: harden context state boundaries`
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
8. `chris-avatar` 上线变更必须获得用户明确授权；本次 P7 已于 2026-08-27
   获得授权并完成。

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
closed。示例阈值只属于文档示例，不能成为代码默认值。用户已为
`gpt-5.6-sol` 确认并部署 ratio Policy：Handoff `0.70`、Emergency 启用且为
`0.85`。按当前缓存的 272,000 Context Limit，运行时解析阈值分别为 190,400 和
231,200 Token。

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
- P2 最初使用 Hermes `plugins.plugin_storage.plugin_db()` 惰性创建数据库；P7
  安全加固后改为通过 `plugin_data_dir()` 定位并安全创建 Profile 数据库；
- 建立 Schema v1、版本检查、安全 journal mode、外键、busy timeout 和幂等迁移；
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

P7 已于 2026-08-27 部署到 `chris-avatar`，Gateway 初始观察通过：

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
- `soul/SOUL-snippet.md` 的规则已合并到 `chris-avatar/SOUL.md`，原有固定模型阈值
  和强制重开会话规则已移除；
- 普通路径不会调用 Hermes 默认 Compression；显式 Emergency 路径委托给 Hermes
  原生 `ContextCompressor`；
- 不会修改 Hermes Session；
- 已从不可变 Commit `f7f1765e5d98325406af675ae8e80deae5a673ec` 安装到
  `chris-avatar`，插件版本为 `0.7.6`、bundled Skill 版本为 `0.4.4`；
- 插件已启用且未授予 built-in tool override 权限，`context.engine` 已切换为
  `context-handoff`；
- Profile Policy 为 ratio Handoff `0.70`、Emergency `0.85`；实际运行时解析到
  272,000 Context Limit、190,400 Handoff 阈值和 231,200 Emergency 阈值；
- 上线前快照保存在
  `/home/chen/hermes-rollout-backups/chris-avatar-20260827T083122Z`，Checksum 和
  SQLite 完整性校验均通过；
- `hermes-gateway-chris-avatar.service` 与承载 Desktop 的
  `hermes-dashboard.service` 均已重启并保持 active/running；serve 在 `9119`
  监听，`/api/health` 与 `/api/status` 均返回 HTTP 200；当前执行账户无 journal
  读取权限，因此本次没有把日志扫描冒充为已完成；
- 插件数据库目录权限为 `0700`、文件权限为 `0600`，完整性校验通过；当前 Hermes
  Python 链接 SQLite 3.50.4，因此插件安全使用 `journal_mode=DELETE`，不进入已知
  WAL-reset 损坏路径；
- 首次 Desktop 真实任务暴露了嵌套工具 Schema 未展开的问题：Skill 能加载，但模型
  混淆 Task `in_progress` 与 Checkpoint `current_state/rejected_alternatives`，两次
  创建均 fail closed 且没有半成品数据；0.7.1 已补全闭合 Schema、Skill 字段边界和
  可恢复错误提示；
- 后续真实任务发生一次可恢复的 Hermes deferred `tool_call` 包装错误：模型先遗漏
  外层 `name`，再把 `task_id` 放在外层而非 `arguments`；失败调用没有写入，正确
  重试后成功。0.7.2 已增加双层 JSON 示例并禁止猜测缺失的持久化 ID/版本；
- 真实 Checkpoint 随后两次把普通禁令和授权边界混入
  `rejected_alternatives`。0.7.3 已把该字段限定为“实际评估并否决的可行方案 +
  否决理由”，并明确将约束、接受的决策和未解决问题分别归入对应字段；没有候选
  方案时必须使用空数组；
- 0.7.3 部署后线上数据库完整性为 `ok`，保留 2 个 Task、70 个 Event、14 个
  Checkpoint、15 个 Segment 和 1 个活动 Session Pointer；重装未丢失持久化状态；
- 新会话 continuation 实测暴露出默认搜索仅查 `paused/blocked`：Agent 在旧 Task
  仍为 `active` 时错误推断其不存在，创建了重复 Task，并把旧 Task artifact 路径
  复制到新 Task。0.7.4 将默认搜索改为全部未结束状态，规定历史确切 Task ID 必须
  `get`，禁止为另一 Session 的已识别 active Task 调用 `create`；独立新 Task 若在
  创建时声明另一 Task 的 `task-artifacts/<task-id>/` 命名空间会由服务端拒绝，显式
  子 Task 只允许把父 Task artifact 当只读输入；
- 0.7.4 安装前快照位于
  `/home/chen/hermes-rollout-backups/chris-avatar-0.7.4-20260831T101054Z`。安装后线上
  SQLite 完整性为 `ok`，保留 3 个 Task、159 个 Event、45 个 Checkpoint、45 个
  Segment 和 2 个活动 Session Pointer；数据库目录/文件权限仍为 `0700/0600`；
- 2026-08-31 18:11 CST 已重启 `hermes-gateway-chris-avatar.service` 和承载 Desktop
  的 `hermes-dashboard.service`，两者均为 active/running、`NRestarts=0`，serve
  继续监听端口 9119，`/api/health` 返回 `ok`。Dashboard 在 multiple gateway
  模式下的聚合 `/api/status` 仍显示 gateway degraded/stopped，但独立 chris-avatar
  gateway 进程 PID 与 systemd 状态正常；当前账户无 journal 权限，未声称完成日志
  内容扫描；
- 2026-09-02 17:49 CST，真实会话在同一 Turn 调用 `task_state_manage(action=block)`
  后清空活动 Task/Segment 指针，0.7.4 的下一次 `select_context()` 回退完整 513 条
  canonical history；Provider 输入从 115,039 跳至约 873,368 Token，并最终以
  988,668 Token 超限。0.7.5 在活动指针为空时恢复本 Session 最新带 Checkpoint 的
  Segment，继续使用 Checkpoint Bootstrap 与 Segment Tail，不再回灌完整历史；
- 0.7.5 部署前快照位于
  `/home/chen/hermes-rollout-backups/chris-avatar-0.7.5-20260902T095824Z`，Checksum 与
  SQLite 完整性校验通过。2026-09-02 18:02 CST 已按固定 SHA 重装并重启 Gateway 与
  Desktop serve；两项服务均为 active/running、`NRestarts=0`，9119 `/api/health`
  返回 `ok`，installed-path Doctor 通过。当前账户仍无 journal 权限；
- 2026-09-03 10:19 CST，同一长会话恢复 blocked Task 时，0.7.5 的 resume Segment
  仍以 `start_message_index=0` 创建；下一次请求把 Checkpoint 与全部 1,357 条历史
  拼接，估算从 126,233 跳至 2,330,916 Token，最终以 2,736,509 Token 超限。
  0.7.6 会从 direct 或 deferred `task_state_manage(resume)` 调用反向定位当前 User
  Turn，把该 Turn 作为临时安全游标，直到显式 `handoff_context` 建立正式 Segment；
- 0.7.6 部署前快照位于
  `/home/chen/hermes-rollout-backups/chris-avatar-0.7.6-20260903T023444Z`。2026-09-03
  10:36 CST 已按固定 SHA 重装并重启 Gateway 与 Desktop serve；两项服务均为
  active/running、`NRestarts=0`。installed-path 只读回放将出错会话从 1,357 条
  canonical messages 收敛为 17 条、估算 29,819 Token，且保留 Checkpoint Bootstrap；
- 0.7.7 修复了审计发现的剩余 fail-open 路径：Task 创建或结束后尚未执行首次
  Handoff、零游标 resume 缺少 canonical 激活调用、Session pointer 部分为空或指向
  错误/已关闭 Segment、Repository/Engine 异常等场景，均只选择稳定头部与当前 User
  Turn，并加入诊断边界，不再把完整 canonical history 交给 Provider；
- Task update、event、checkpoint、pause/block/complete/cancel 和 resume 现在都在同一
  数据库事务内校验 Session 所有权；另一 Session 已持有 active Task 时全部 fail
  closed。Session pointer 必须成对存在，且只能指向同 Session、同 Task 的 open
  Segment；
- 新 Handoff Segment 持久化触发消息的语义 checksum。Emergency archive 升级为
  format v2，并保存 canonical conversation prefix checksum；消息被 rewind、编辑或
  替换后不再仅凭消息数量复用压缩结果，旧版 archive 也不会参与恢复；
- 0.7.7 部署前快照位于
  `/home/chen/hermes-rollout-backups/chris-avatar-0.7.7-20260904T041528Z`，同时包含
  Profile、插件数据库在线备份和全部 Emergency archives。2026-09-04 12:17 CST
  已按固定 SHA `27835c11c5d4847482fc6eb71336009488f43610` 重装并重启 Gateway 与
  Desktop serve；两项服务 active/running、`NRestarts=0`，9119 health 为 `ok`；
- 线上插件数据库已从 schema v1 迁移到 v2，新增
  `context_segments.start_message_checksum`；`integrity_check=ok` 且无 foreign-key
  violation。installed-path 只读回放将会话 `20260902_202600_d62df5` 的 1,373 条
  canonical messages 收敛为 18 条、估算 19,322 Token，保留当前 blocked Task
  Bootstrap、排除另一 active Task，且未恢复 legacy archive；
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

P7 最终验证与上线结果：

- 161 个单元/契约/集成/E2E 测试全部通过；
- 总覆盖率 85.54%，超过 80% 门槛；
- Ruff format/check 通过；
- Mypy strict 通过；
- `uv build` 通过；
- `hermes plugins doctor . --ci` 通过且无警告；
- Plugin Doctor 在临时 `HERMES_HOME` 中运行，没有触碰 `chris-avatar`。
- 标准插件安装、启用、配置和 installed-path Runtime 选择在临时 Profile 通过；
- 备份和一条命令回滚在临时 Profile 通过；
- 真机安装固定 SHA、Policy 解析、SOUL 迁移、配置检查、installed-path Doctor、
  Gateway 重启、日志扫描及 SQLite 完整性/权限检查均通过；
- 安全预检发现 Hermes Python 的 SQLite 3.50.4 处于已知 WAL-reset 风险范围；插件
  已增加版本边界检测并在该版本使用 DELETE journal。另将插件数据目录/数据库
  权限收紧为 `0700/0600`，并拒绝符号链接、非常规文件和多硬链接数据库；
- Desktop 真机回归确认插件 Skill/Tools 均可加载；0.7.1 的 installed-path Schema
  探针确认 12 个 Task 字段、10 个必填 Checkpoint 字段完整可见，两个嵌套对象均
  `additionalProperties: false`；
- 0.7.2 的 Skill 契约会解析 deferred `tool_call` JSON 示例并验证外层只含
  `name/arguments`、`task_id` 位于内层；集成测试继续验证缺失 `task_id` 时返回
  `invalid_argument`；
- 0.7.3 的契约测试同时固定 Skill、Checkpoint 模板和 Tool Schema 对
  `rejected_alternatives` 的排他边界，并验证空数组是合法的“没有评估候选方案”；
- 0.7.4 增加 exact-ID `get`、默认搜索三种未结束状态、跨 Session active 禁止重复
  创建的 Skill 契约，以及独立 Task artifact 命名空间拒绝/父子只读继承测试；
- 0.7.7 增加跨 Session 全部 Task 写操作、stale/partial/mismatched pointer、首轮
  Handoff 前 active/inactive Task、缺失 resume 激活调用、Repository/Engine 异常、
  handoff message rewrite、archive prefix rewrite 与 v1 archive 拒绝等回归测试；
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

- `gpt-5.6-sol` 的 ratio Policy 已写入 `chris-avatar`；后续更改比例或切换模型时
  仍必须显式配置并重新验证解析结果；
- 当前 Request Token 是 Hermes 的消息级粗估值；Tool Schema Token 不在
  `select_context()` 的参数中，因此不包含在此字段内；
- 上一 Response 真实 Usage 会滞后一轮，这是设计中的校准数据；
- Resume Tool 不直接执行 Rotation；它返回明确的下一步，Agent 必须再调用
  `handoff_context`；
- 当前版本尚未实现把另一 Session 的 active Task 接入新 Session；发现该候选后会
  保持状态不变并要求回到原 Session，而不是创建重复 Task；
- 搜索是 Profile 内的结构化/词法召回，不包含远程 Embedding；
- Emergency 归档包含完整 Active Request，可能含敏感信息；权限和 Checksum 已
  收紧，P7 runbook 要求观察期全部保留、验收后至少保留 30 天，清理必须另行
  获得用户决定；
- Emergency 成功后 canonical Session 保持完整，后续请求使用归档中的压缩
  Selection；Agent 仍需尽快补建正式 Checkpoint 并执行普通 Handoff；
- `select_context()` 的消息估算不含 Tool Schema Token；Hermes Host 传给
  `should_compress()` 的 Request Pressure 才是 Emergency 触发的权威输入；
- Hermes v0.20.5 自带 Python 当前链接 SQLite 3.50.4；插件已安全回退 DELETE
  journal，但后续可在独立维护窗口升级 Hermes/SQLite；
- exact-ID/continuation 分类和“另一 Session active Task 禁止 create”包含语义判断，
  因而属于模型协议加固；artifact 命名空间另有服务端 create-time 防线。插件仍须
  fail closed。观察期间应保留数据库、归档和对应时间戳，异常时不要先清理证据。

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

P7 部署与初始观察已完成，接下来按真实工作负载持续验证：

1. 用新会话描述旧任务但不复述精确标题，观察 0.7.7 是否先穷举全部未结束 Task、
   对历史确切 ID 使用 `get`，并在发现另一 Session 的 active Task 后停止而不 create；
2. 首次接近 Handoff 甜区时，核对 Runtime Status、Task、Checkpoint、Event 和
   Segment 是否可交叉追溯；
3. 若实际触发 Emergency，保留 Archive、数据库和日志，成功后尽快补建正式
   Checkpoint/Handoff；
4. 若出现异常，记录时间戳、Session ID 和 Runtime Status，不删除诊断证据；再按
   `docs/P7-chris-avatar-runbook.md` 定位或使用受保护快照回滚；
5. 在独立维护窗口评估升级 Hermes，使其 Python SQLite 离开已知风险版本范围。

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
