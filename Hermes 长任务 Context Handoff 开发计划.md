# Hermes 长任务 Context Handoff 开发计划

> 需求来源：[Hermes 长任务 Context Handoff 方案](./Hermes%20长任务%20Context%20Handoff%20方案.md)
>
> 目标 Profile：`chris-avatar`
>
> 文档状态：P7 已部署，初始观察通过，进入真实工作负载持续观察

## 1. 已确认决策

1. Hermes 可以升级，最低支持版本锁定为 `v2026.8.19`。
2. Task State、Event Log、Checkpoint 和 Context Segment 均由本插件实现。
3. 插件可以修改 `chris-avatar` Profile 的 `SOUL.md`。
4. 第一版需要包含：
   - 同一 Agent Turn 内 Context Rotation；
   - 新任务与子任务识别；
   - Emergency Fallback；
   - 完整事件追溯和重启恢复。
5. Context 甜区和交接阈值不在插件中硬编码，由用户按模型配置。
6. ContextEngine 只提供 Context 使用事实、解析用户配置并执行切换；是否在甜区内执行普通 Handoff，仍由 Agent 结合任务状态决定。

## 2. 当前环境基线

`chris-avatar` 当前运行环境：

- Profile 路径：`~/.hermes/profiles/chris-avatar/`
- Hermes：`v0.20.5 / 2026.8.19`
- 模型：`gpt-5.6-sol`
- Provider：`openai-codex`
- Gateway systemd 服务：`hermes-gateway-chris-avatar.service`
- Gateway 当前已启用并运行

当前版本已经提供本方案依赖的接口：

- `ContextEngine.select_context()`：每次 Provider Request 前选择本次请求 Context；
- `ContextEngine.update_from_response()`：接收 Provider 返回的 Token Usage；
- `ContextEngine.get_tool_schemas()` / `handle_tool_call()`：暴露 Context Control Tool；
- `ContextEngine.on_session_start()` / `on_session_reset()`：管理 Session 生命周期；
- `context.engine`：通过 Profile 配置选择 ContextEngine。

现有 `SOUL.md` 中“超过固定 Token 后输出交接文档并重开会话”的规则需要迁移。新规则不得硬编码某个模型的阈值，而应引用当前模型对应的 Handoff Policy。

## 3. 开发范围

### 3.1 第一版包含

- Hermes Native Plugin 骨架和安装配置；
- 自定义 `ContextHandoffEngine`；
- 模型级 Handoff Policy；
- Runtime Status 临时注入；
- Task State 管理；
- Event Log；
- Checkpoint 创建与校验；
- Active Context Pointer；
- `handoff_context` Tool；
- 同一 Agent Turn 内 Context Rotation；
- 新任务、子任务和当前任务延续识别；
- Emergency Fallback；
- SQLite 持久化、迁移、并发控制和重启恢复；
- Handoff Skill 和 `SOUL.md` 规则；
- 单元测试、集成测试、Profile 上线和回滚说明。

### 3.2 第一版不包含

- 删除或改写 Hermes 原始 Session History；
- 跨机器同步 Task State；
- Web 管理界面；
- 自动学习某个模型的甜区；
- 在没有用户策略时猜测 Handoff 阈值；
- 替换 Hermes Gateway 或 Agent Loop。

## 4. 总体架构

```text
Hermes Gateway / Agent Loop
            │
            ▼
ContextHandoffEngine.select_context()
            │
            ├── 读取当前模型和 Handoff Policy
            ├── 计算当前 Request 的估算 Token
            ├── 读取上一 Request 的真实 Token Usage
            ├── 根据 Active Context Pointer 选择消息
            └── 在尾部临时追加 Runtime Status
            │
            ▼
       Provider Request

Agent + Handoff Skill
            │
            ├── task_state_manage
            ├── task_event_append
            ├── checkpoint_create
            └── handoff_context
                    │
                    ▼
        Active Context Pointer 切换
                    │
                    ▼
          下一次 Provider Request
```

插件内部划分为两层：

### Task 层

负责 Task State、Event、Checkpoint 和任务关系，不参与 Provider Context 选择。

建议工具：

- `task_state_manage`
- `task_event_append`
- `checkpoint_create`

### ContextEngine 层

负责 Token 观测、Context 选择、Runtime Status、Active Pointer 和 Context Rotation。

由 ContextEngine 暴露：

- `handoff_context`

该分层确保 ContextEngine 不负责理解和总结 Task 语义。

## 5. 计划目录

```text
chris-hermes-agent/
├── plugin.yaml
├── __init__.py
├── pyproject.toml
├── README.md
├── chris_hermes_agent/
│   ├── config.py
│   ├── models.py
│   ├── store.py
│   ├── migrations.py
│   ├── task_service.py
│   ├── task_tools.py
│   ├── checkpoint_service.py
│   ├── policy.py
│   ├── token_usage.py
│   ├── context_builder.py
│   ├── context_engine.py
│   ├── emergency.py
│   └── errors.py
├── skills/
│   └── context-handoff/
│       ├── SKILL.md
│       └── references/
│           ├── checkpoint-template.md
│           ├── task-state-rules.md
│           └── new-task-detection.md
├── soul/
│   └── SOUL-snippet.md
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
    ├── install-profile.sh
    ├── verify-profile.sh
    └── rollback-profile.sh
```

运行数据不得写入插件安装目录。插件使用 Hermes 的 `plugin_data_dir()` 定位当前
Profile 的 `plugin-data/<plugin-name>/data.db`，并自行以 `0700/0600` 安全权限创建
目录和数据库，拒绝符号链接、非常规文件和多硬链接数据库。SQLite 版本安全时使用
WAL；命中已知 WAL-reset 损坏版本时自动使用 DELETE journal。

## 6. 模型级 Handoff Policy

### 6.1 原则

- 插件不提供跨模型固定甜区。
- 用户可以为每个模型使用比例阈值或绝对 Token 阈值。
- 普通 Handoff 阈值只作为 Agent 的决策依据，不强制打断任务。
- Emergency Fallback 只有在用户为当前模型显式启用时才生效。
- 模型切换后，ContextEngine 必须立即重新解析 Policy。
- 没有匹配策略时，只报告原始使用数据，不猜测 Handoff 时机。

### 6.2 配置结构

```yaml
context:
  engine: context-handoff

plugins:
  enabled:
    - chris-hermes-agent
  entries:
    chris-hermes-agent:
      settings:
        handoff:
          # 未匹配模型时默认只观测，不自动进入甜区或紧急兜底。
          default_policy:
            handoff_enabled: false
            emergency_enabled: false

          model_policies:
            gpt-5.6-sol:
              handoff_enabled: true
              sweet_zone:
                type: ratio
                start: 0.70       # chris-avatar 已部署值
              emergency:
                enabled: true
                type: ratio
                threshold: 0.85   # chris-avatar 已部署值

            glm-5.3:
              handoff_enabled: true
              sweet_zone:
                type: absolute_tokens
                start: 400000     # 从原 SOUL 迁移的用户策略示例
              emergency:
                enabled: true
                type: absolute_tokens
                threshold: 700000 # 示例值，上线前由用户确认
```

其他模型的示例值不能作为代码默认值。上述 `gpt-5.6-sol` 数值是用户为
`chris-avatar` 明确确认的 Profile 配置，也不是插件默认值。

### 6.3 策略匹配顺序

```text
精确模型名
    ↓
最长模型名模式匹配
    ↓
Provider 级策略（如果配置）
    ↓
default_policy
```

解析结果必须包含来源，例如：`exact:model:gpt-5.6-sol`，并写入诊断日志和 Runtime Status，便于发现配置匹配错误。

### 6.4 配置校验

- `ratio` 必须在 `(0, 1)`；
- `absolute_tokens` 必须为正整数；
- Emergency 阈值必须高于甜区起点；
- 阈值必须低于模型硬 Context Limit；
- 未知类型、缺失字段或冲突配置必须 fail closed；
- 配置无效时不得悄悄使用内置默认阈值。

## 7. Runtime Status

### 7.1 数据来源

同时维护：

- `estimated_prompt_tokens`：当前待发送 Request 的估算值；
- `last_prompt_tokens`：上一 Provider Response 返回的真实值；
- `context_limit`：当前模型 Context Window；
- `context_ratio`：当前估算值与 Context Limit 的比值；
- `policy_match`：当前模型匹配到的用户策略。

工具调用可能在同一 Turn 内快速增加 Context，真实 Usage 会滞后一轮，因此 Agent 判断时优先参考当前估算值，同时保留真实值用于校准。

### 7.2 消息格式

```text
[Runtime Status]

Model: gpt-5.6-sol
Prompt tokens, estimated: 108000
Prompt tokens, last actual: 104500
Context limit: 200000
Context usage: 54%
Handoff policy: exact:model:gpt-5.6-sol
Configured sweet zone starts at: 110000 tokens
Emergency fallback threshold: 180000 tokens
Context handoff available: true
Active task: task-001
Context segment: segment-002
```

要求：

- 只存在于本次 Provider Request；
- 不写入 Hermes Session History；
- 不写入 Event Log；
- 始终位于 Context 尾部；
- 每次 Request 最多一条；
- 不修改 System Prompt；
- 非 Handoff 请求不重新排列稳定 Prefix。

## 8. 数据模型

### 8.1 Task

至少包含：

- `task_id`
- `parent_task_id`
- `title`
- `created_session_id`
- `last_session_id`
- `goal`
- `constraints`
- `current_phase`
- `completed`
- `in_progress`
- `known_issues`
- `next_actions`
- `decisions`
- `artifacts`
- `status`
- `search_aliases`
- `tags`
- `paused_at`
- `last_resumed_at`
- `resume_count`
- `created_at`
- `updated_at`

Task 状态为 `active`、`paused`、`blocked`、`completed` 或 `cancelled`。新任务
开始时，尚未完成的当前任务默认进入 `paused`；Task 在当前 Profile 内可跨
Hermes Session 搜索和恢复，实际执行 Session 由 Context Segment 记录。

### 8.2 Event

支持以下事件类型：

- `TASK_CREATED`
- `TASK_PAUSED`
- `TASK_RESUMED`
- `TASK_BLOCKED`
- `TASK_COMPLETED`
- `TASK_CANCELLED`
- `GOAL_CHANGED`
- `CONSTRAINT_ADDED`
- `DECISION_MADE`
- `DECISION_REVOKED`
- `PHASE_COMPLETED`
- `FILE_CHANGED`
- `TEST_FAILED`
- `TEST_PASSED`
- `CHECKPOINT_CREATED`
- `HANDOFF_COMPLETED`
- `NEW_TASK_STARTED`
- `EMERGENCY_COMPRESSION_TRIGGERED`
- `EMERGENCY_COMPRESSION_COMPLETED`

### 8.3 Checkpoint

Checkpoint 使用结构化字段存储，同时可以生成 Markdown 展示文本：

- Task
- Goal
- Constraints
- Current Phase
- Completed
- Current State
- Decisions
- Rejected Alternatives
- Known Issues
- Artifacts
- Next Actions

### 8.4 Context Segment

- `context_segment_id`
- `session_id`
- `task_id`
- `parent_segment_id`
- `checkpoint_id`
- `start_message_index`
- `end_message_index`
- `start_time`
- `end_time`
- `handoff_reason`
- `handoff_policy_snapshot`
- `archived_context_reference`

### 8.5 Session Context State

- `session_id`
- `active_task_id`
- `active_context_segment_id`
- `handoff_pending`
- `pending_checkpoint_id`
- `last_handoff_at`
- `version`

`version` 用于乐观锁，防止同一 Session 的重复或并发 Handoff。

## 9. `handoff_context` Tool

建议 Schema：

```text
handoff_context(
    checkpoint_reference,
    handoff_reason,
    target_task_id,
    expected_active_task_id,
    expected_active_segment_id
)
```

执行步骤：

1. 校验 Checkpoint 存在且属于 `target_task_id`；
2. 校验 Checkpoint 字段完整，`Next Actions` 非空；
3. 使用 `expected_active_task_id` 和 `expected_active_segment_id` 校验当前
   Active Pointer 未被并发切换；
4. 在一个数据库事务中：
   - 关闭当前 Segment；
   - 创建下一 Segment；
   - 更新 Active Context Pointer；
   - 记录 `HANDOFF_COMPLETED`；
5. 返回结构化 Tool Result；
6. 下一次 `select_context()` 发现 Pointer 已改变，构造新 Context；
7. Agent Turn 和 Gateway Connection 保持连续。

Handoff Tool Result 必须简短、确定且包含：

- 新 Segment ID；
- Checkpoint ID；
- Task ID；
- 下一步动作摘要；
- `handoff_applied: true`。

## 10. Handoff 后的 Context Bootstrap

新的 Provider Context 包含：

```text
Hermes System Instructions
+ SOUL.md
+ 当前任务所需 Skills
+ 长期 Memory
+ Task State / Checkpoint
+ 必要 Decisions / Events
+ 触发 Handoff 的 Tool Call / Tool Result
+ Handoff 后新增消息
+ Runtime Status
```

实现要求：

- 从原 Request 中保留 Hermes 已构造的 System 内容，不自行重建系统指令；
- 使用 `start_message_index` 或稳定消息游标选择 Handoff 后消息；
- 保留触发 Handoff 的 assistant tool call 与对应 tool result，避免孤立 Tool Message；
- 不继承旧 Segment 中的大量 Shell、File、Git 和搜索结果；
- Checkpoint 必须包含继续执行原用户目标所需的信息；
- Hermes Tool Schemas 继续由 Runtime 正常提供；
- 完整旧历史不删除，只是不进入新的 Active Context。

## 11. 新任务与子任务识别

Handoff Skill 在收到新的用户目标时分类：

### 当前任务延续

- 继续当前 Task 和 Segment；
- 更新 Task State；
- 不进行 Context 隔离。

### 当前任务的子任务

- 创建带 `parent_task_id` 的子 Task；
- 继承显式选中的约束、Decision 和 Artifact；
- 不默认复制全部父任务执行历史。

### 完全新任务

1. 更新当前 Task State 并创建 Checkpoint；
2. 当前目标尚未完成时记录 `TASK_PAUSED` 并标记为 `paused`；
3. 创建新 Task；
4. 调用 `handoff_context`；
5. 新 Context 只继承 SOUL、Skills、长期 Memory 和 Hermes Runtime；
6. 不继承旧任务 Tool Trace。

识别信心不足且不同分类会造成明显状态差异时，Agent 必须向用户确认。

### 暂存任务恢复

- `task_state_manage(search)` 使用自然语言检索 `paused` / `blocked` Task；
- 搜索索引包含 Goal、Phase、Decision、Artifact、标签和 Next Actions，但不包含
  大量 Tool Trace；
- SQLite 优先使用 FTS5 trigram，运行环境不支持时使用规范化字符串回退；
- 唯一明确候选可以恢复，多个相近候选必须由用户确认；
- 恢复前先为当前未完成任务创建 Checkpoint 并暂存；
- 恢复目标 Task 时记录 `TASK_RESUMED`，增加 `resume_count`，更新
  `last_session_id`，并从其最新有效 Checkpoint 创建新 Segment；
- Task 可跨 Hermes Session 恢复，完整 Session/Segment 关系保持可追溯。

## 12. Emergency Fallback

### 12.1 原则

- 只有当前模型 Policy 显式启用时生效；
- 阈值完全由用户定义；
- 正常流程始终优先使用 Agent 主动 Handoff；
- Emergency Fallback 是防止触碰模型硬上限的最后保护；
- 不能把 Emergency 阈值写入 SOUL 或源代码常量。

### 12.2 流程

达到用户配置的 Emergency 阈值且 Agent 尚未完成 Handoff 时：

1. 将压缩前完整 Active Context 归档到插件数据目录；
2. 记录 `EMERGENCY_COMPRESSION_TRIGGERED`；
3. 调用 Hermes 内置 Compression Delegate；
4. 验证压缩后的请求已回到模型安全范围；
5. 记录 `EMERGENCY_COMPRESSION_COMPLETED`；
6. Runtime Status 标记本 Segment 曾发生 Emergency Compression；
7. Agent 在形成稳定状态后尽快创建正式 Checkpoint 并主动 Handoff。

如果当前模型没有 Emergency Policy，`should_compress()` 始终返回 `False`，插件不得自行启用默认压缩。

## 13. SOUL 与 Skill

### 13.1 SOUL 只保存长期规则

替换现有固定模型、固定 Token 和“必须重开会话”的描述，改为：

- 每次 LLM Request 检查最后一条 Runtime Status；
- 根据当前模型匹配到的 Handoff Policy 判断甜区；
- 没有匹配策略时不得猜测阈值；
- 进入甜区后结合任务阶段寻找稳定 Handoff 点；
- 未持久化 Task State 和 Checkpoint 时禁止调用 `handoff_context`；
- Handoff 后从 Checkpoint 的 `Next Actions` 继续；
- 新任务不得继承旧任务执行历史；
- Emergency Fallback 只按用户配置触发。

### 13.2 Skill 保存工作流程

Skill 负责：

- Task 创建和更新；
- Event 记录时机；
- Decision 完整格式；
- Checkpoint 生成和自检；
- 新任务识别；
- Handoff 前置检查；
- Handoff 后恢复步骤；
- Emergency 后补建正式 Checkpoint。

插件注册后的 Skill 使用命名空间，例如 `chris-hermes-agent:context-handoff`。SOUL 需要明确要求在长任务开始时加载该 Skill。

## 14. 开发阶段

### 当前进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| P0 工程骨架与契约测试 | 已完成 | 已提交并通过全部验证 |
| P1 配置与 Policy Resolver | 已完成 | 已实现配置注入、校验、匹配和模型切换 |
| P2 SQLite 与 Task State | 已完成 | 已实现持久化、任务工具、暂存、搜索和恢复 |
| P3 Runtime Status 与 Token 观测 | 已完成 | 已实现请求级状态、估算/真实 Usage 和 Active Pointer 观测 |
| P4 Context Rotation | 已完成 | 已实现原子 Segment 切换、Checkpoint Bootstrap 和同 Turn 继续 |
| P5 Skill、SOUL 与任务隔离 | 已完成 | 已交付 Agent 工作流、SOUL 片段和任务隔离验证 |
| P6 Emergency Fallback | 已完成 | 已实现安全归档、Hermes Delegate、验证和恢复 |
| P7 集成测试与上线 | 已完成 | 已安装固定 SHA、应用 Policy/SOUL、重启 Gateway 并通过初始观察 |

### P0：工程骨架与契约测试

状态：**已完成（2026-08-26）**

- 创建 `plugin.yaml`、插件入口和 Python 包；
- 注册 ContextEngine、Task Tools 和 Skill；
- 建立 pytest、lint 和类型检查；
- 为 Hermes ABC 和工具 Schema 编写契约测试。

完成标准：插件能被 `chris-avatar` 发现，但尚不启用 Context Rotation。

完成证据：Hermes Plugin Doctor 已通过隔离环境下的运行时发现、Manifest
解析、插件导入和注册验证；16 个契约/集成测试通过，代码覆盖率 100%。

### P1：配置与 Policy Resolver

状态：**已完成（2026-08-26）**

- 实现配置读取、校验和模型匹配；
- 支持 ratio/absolute_tokens；
- 支持模型切换；
- 对无策略和错误策略 fail closed。

完成标准：不同模型可以解析出不同 Policy，且无任何硬编码甜区。

完成证据：插件通过 `ctx.get_config("handoff")` 读取 Profile 私有配置；
Policy Resolver 支持 ratio、absolute_tokens、精确模型、最长模型子串、Provider
和 default_policy，并对未知、冲突、越界及未匹配配置 fail closed；
`ContextHandoffEngine.update_model()` 会重新解析策略。40 个单元、契约和集成测试
通过，分支覆盖率超过 90%，Ruff、Mypy strict、构建和 Plugin Doctor 均通过。

### P2：SQLite 与 Task State

状态：**已完成（2026-08-26）**

- 建立 Schema 和迁移；
- 实现 Task、Event、Checkpoint、Segment Repository；
- 实现事务、乐观锁、WAL 和重启恢复；
- 实现 Task 层工具；
- 实现 active/paused/blocked/completed/cancelled 状态；
- 实现 Profile 级自然语言搜索和跨 Session 暂存/恢复数据契约。

完成标准：Task 状态可持久化、查询、暂存、自然语言检索、跨 Session 恢复并
追溯；P2 只切换 Task Active Pointer，实际 Provider Context Rotation 仍在 P4
接通。

完成证据：在 Hermes Profile 的 `plugin-data/chris-hermes-agent/` 中惰性创建
数据库；Schema v1 包含 Task、Event、Checkpoint、Segment 和 Session Context
State，启用安全的 journal mode、外键、busy timeout、事务和乐观锁；Task Tool 已支持创建、
查询、更新、暂存、检索、恢复、阻塞、完成和取消。FTS5 trigram 支持中文子串
检索并带字符串回退，搜索文档包含 Task State、Checkpoint 和选择性 Decision
Event，不包含大段 Tool Trace。62 个测试通过，总覆盖率 87.24%，并发、回滚、
重启恢复、Checksum、防降级迁移、构建和 Plugin Doctor 均通过。

### P3：Runtime Status 与 Token 观测

状态：**已完成（2026-08-26）**

- 实现真实 Usage 更新；
- 实现当前 Request Token 估算；
- 实现 Runtime Status；
- 验证消息临时性和 Prefix 稳定性。

完成标准：一个 Tool Loop 内每次 Provider Request 都能看到最新状态，Session History 中没有 Runtime Status。

完成证据：使用 Hermes `estimate_messages_tokens_rough()` 计算包含最新 Runtime
Status 的自洽 Request 估算；`update_from_response()` 同时兼容 legacy/canonical
Usage 字段，并显式区分真实零值与无 Usage。`select_context()` 只创建请求列表浅
拷贝并在稳定 Prefix 尾部追加一条状态，重复调用不会累积状态；模型切换、Retry、
Tool Loop、无 Usage、无 Policy、无效 Policy 和 Session Active Task/Segment 均有
测试覆盖。74 个测试通过，总覆盖率 87.66%，Ruff、Mypy strict、构建和 Plugin
Doctor 均通过。

### P4：Context Rotation

状态：**已完成（2026-08-27）**

- 实现 `handoff_context`；
- 实现 Active Pointer 和 Segment 状态机；
- 实现 Context Bootstrap；
- 保证 Tool Call/Result 配对；
- 支持同一 Agent Turn 内继续执行。

完成标准：Handoff 后旧 Tool Trace 不再发送，Agent 不依赖下一条用户消息即可继续。

完成证据：`handoff_context` 校验 Checkpoint 所属关系、Checksum 和调用方预期的
Active Task/Segment；旧 Segment 关闭、新 Segment 创建、Session Pointer 更新与
`HANDOFF_COMPLETED` Event 在同一 SQLite 事务中提交。下一次
`select_context()` 保留 Hermes 稳定头，注入完整 Checkpoint Bootstrap，并从
触发 Handoff 的 assistant Tool Call 开始保留新 Segment 消息，因此 Tool
Call/Result 始终成对且旧 Tool Trace 不再发送。并发/重复调用、事务回滚、同 Turn
Tool Loop、进程重启、Checkpoint 损坏、历史遗留 Tool Call 和损坏消息游标均有
测试覆盖。90 个测试通过，总覆盖率超过 80%，Ruff、Mypy strict、构建和 Plugin
Doctor 均通过。

### P5：Skill、SOUL 与任务隔离

状态：**已完成（2026-08-27）**

- 编写 Handoff Skill；
- 编写 SOUL 迁移片段；
- 实现当前任务、子任务、新任务流程；
- 增加 Checkpoint 质量检查。

完成标准：新任务不会继承旧任务执行噪声，当前任务可以跨多次 Rotation 连续执行。

完成证据：bundled Skill 已从阶段 Stub 改为完整 Agent 操作入口，并按需路由到
Checkpoint 模板、任务分类和 Task State/继承规则；SOUL 迁移片段只读取当前
Runtime Policy，不包含固定模型或固定阈值。当前任务延续、子任务白名单继承、
完全新任务隔离、Resume 后显式 Rotation 和低置信度确认规则均有契约或集成验证；
新 Task Rotation 后的 Provider Request 不包含旧 Task User History 或 Tool Trace。
98 个测试通过，总覆盖率 86.49%，Ruff、Mypy strict、构建和隔离 Plugin Doctor
均通过。

### P6：Emergency Fallback

状态：**已完成（2026-08-27）**

- 实现 Emergency Policy；
- 实现压缩前归档；
- 封装 Hermes Compression Delegate；
- 实现恢复验证和事件记录。

完成标准：只有显式启用的模型策略能够触发兜底，且压缩前原始 Context 可追溯。

完成证据：ContextEngine 的 Host Compression 阈值只使用当前模型显式配置的
Emergency Policy；完整 Active Provider Request 先以随机文件名、Checksum 和
`0700/0600` 权限归档，再调用 Hermes `ContextCompressor` Delegate。返回结果会
重新估算并要求低于同一配置阈值；成功、Delegate 异常、无进展、仍超限、归档
损坏和存储失败均有 fail-closed 测试。成功后只替换下一次 Request Selection，
canonical Session History 保持原对象和原内容，重启后可从归档恢复。Runtime
Status 和 Triggered/Completed/Failed Event 提供不含 Context/异常详情的安全状态。
110 个测试通过，总覆盖率 86.94%，Ruff、Mypy strict、构建和隔离 Plugin
Doctor 均通过。

### P7：集成测试与 Profile 上线

状态：**已完成（2026-08-27，部署与初始观察通过）**

- 完成长 Tool Loop 集成测试；
- 测试 Gateway 重启；
- 测试连续多次 Rotation；
- 测试模型切换和 Policy 切换；
- 备份并修改 `chris-avatar`；
- 重启 Gateway 并进行观察期验证。

完成标准：所有验收标准通过，并具备一键回滚路径。

完成证据：123 个测试通过，总覆盖率 86.91%。真实 Hermes Host 测试覆盖 Tool
Schema Request Pressure、Compression no-progress 边界、Emergency 后正式
Checkpoint/Handoff、canonical Session 不变和独立进程恢复；隔离安装测试通过
标准 `plugins install`、installed-path Doctor、配置注入、ContextEngine 选择及
模型/Policy fail-closed 切换。连续 10 次 Rotation 保持 Prompt Cache 稳定头并在
中途重建 Engine。备份/回滚脚本通过隔离 E2E，可校验快照后恢复原 config、SOUL
和 Session 数据库，同时保留插件 SQLite 与 Emergency 归档。当前 Hermes 安装器
只接受 Manifest v1，因此发布包显式锁定 `manifest_version: 1`；加载器所需的
`api_version` 和 `config_schema` 仍保留。

用户确认 `gpt-5.6-sol` 使用 ratio Handoff `0.70`、Emergency `0.85`。插件已从
不可变 Commit `5adc9dc03fcb09957a6fefca32f74fcd2a7ba27d` 安装并启用，SOUL 规则已迁移，
`context.engine` 已切换为 `context-handoff`。按 272,000 Context Limit，运行时解析
阈值为 190,400/231,200 Token。上线前快照位于
`/home/chen/hermes-rollout-backups/chris-avatar-20260827T083122Z`，Checksum 与
SQLite 完整性通过；Gateway 重启后保持 active/running，启动日志无插件错误。

安全预检另发现当前 Hermes Python 链接 SQLite 3.50.4。最终实现会识别 SQLite
WAL-reset 风险版本并在此环境使用 DELETE journal，同时将插件数据目录和数据库
权限收紧为 `0700/0600`。初始线上库尚无 Task/Event/Segment，符合尚未运行真实
开发任务的状态；首次真实 Handoff/Emergency 证据转入正常使用过程持续观察。

## 15. 测试计划

### 15.1 单元测试

- Policy 匹配优先级；
- ratio 和 absolute_tokens 校验；
- 模型切换；
- Token 使用率计算；
- Runtime Status 格式和临时性；
- Task State 转换；
- Task 暂存、搜索候选排序和跨 Session 恢复；
- Checkpoint Schema；
- Segment 父子关系；
- Active Pointer 更新；
- Handoff 幂等性；
- SQLite 事务回滚；
- 并发 Handoff；
- Emergency Policy 显式启用约束。

### 15.2 集成测试

核心场景：

1. 用户发起长 Coding 任务；
2. LLM 和 Tool 在一个 Turn 内多次循环；
3. Runtime Status 按当前模型 Policy 展示甜区；
4. Agent 在稳定节点创建 Task Checkpoint；
5. Agent 调用 `handoff_context`；
6. 下一次 Provider Request 使用新 Context；
7. Gateway、Turn 和 Task 均未结束；
8. Agent 从 `Next Actions` 继续执行；
9. Hermes Session 和插件 Event Log 均可追溯旧 Segment。

补充场景：

- Provider 不返回 Usage；
- Context 估算偏差；
- Checkpoint 丢失或损坏；
- Handoff Tool 成功后 Provider 暂时失败；
- Gateway 在 Handoff 前后重启；
- 连续至少 10 次 Rotation；
- 新任务和子任务；
- 未配置模型 Policy；
- Policy 配置错误；
- Emergency Compression 成功、失败和恢复；
- Prompt Cache Prefix 稳定性。

## 16. 验收标准

- 普通路径不调用 Hermes 默认 Compression；
- Handoff 甜区完全来自当前模型的用户配置；
- 未配置模型不得使用猜测阈值；
- 每次 Provider Request 都包含最新 Runtime Status；
- Runtime Status 不进入 Session History；
- Handoff 不结束 Gateway Connection、Agent Turn 或 Task；
- Handoff 后旧 Tool Trace 不进入新的 Provider Request；
- System、SOUL、Skills、Memory 和 Tool Schemas 不丢失；
- 完整历史不删除，Task、Decision、Checkpoint 和 Segment 可追溯；
- Gateway 重启后恢复 Active Task 和 Segment；
- 新任务不继承旧任务 Tool Trace；
- Emergency Fallback 只在用户显式启用时触发；
- 支持至少连续 10 次 Context Rotation；
- 插件异常时不得静默破坏 Hermes 持久化会话。

## 17. `chris-avatar` 上线步骤

1. 记录当前 Hermes Commit 和 Gateway 状态；
2. 备份：
   - `config.yaml`
   - `SOUL.md`
   - `state.db`
   - 当前 Session 索引
3. 将插件安装到 `chris-avatar` Profile；
4. 启用 `chris-hermes-agent`；
5. 在 `config.yaml` 设置 `context.engine: context-handoff`；
6. 由用户填写并确认 `gpt-5.6-sol` 的甜区和 Emergency Policy；
7. 使用配置检查命令验证 Policy；
8. 合并 SOUL Handoff 规则，删除固定模型阈值和强制重开会话规则；
9. 运行单元测试和离线集成测试；
10. 重启 `hermes-gateway-chris-avatar.service`；
11. 初始观察通过后，使用真实小型开发任务持续观察 Runtime Status 和首次
    Handoff；
12. 检查 Gateway 日志、Token Usage、Event Log 和 SQLite 状态；
13. 验收后开放正式任务。

## 18. 回滚方案

出现异常时：

1. 停止 `chris-avatar` Gateway；
2. 将 `context.engine` 恢复为 `compressor`；
3. 禁用插件；
4. 恢复原 `SOUL.md` 和 `config.yaml`；
5. 保留插件 SQLite 和归档，不删除诊断证据；
6. 重启 Gateway；
7. 验证原 Hermes Session 可以继续使用。

插件上线不能要求不可逆迁移 Hermes 自有数据库。

## 19. 工期估算

| 阶段 | 预计时间 |
|---|---:|
| P0 工程骨架与契约测试 | 1 天 |
| P1 Policy Resolver | 1 天 |
| P2 SQLite 与 Task State | 2 天 |
| P3 Runtime Status | 1～1.5 天 |
| P4 Context Rotation | 2 天 |
| P5 Skill、SOUL 与任务隔离 | 1～1.5 天 |
| P6 Emergency Fallback | 1～1.5 天 |
| P7 集成测试与上线 | 2 天 |

完整版本预计 `11～13 个工作日`。

## 20. 已确认的上线配置

用户已确认并部署：

- `gpt-5.6-sol` 甜区使用 ratio `0.70`；
- Emergency Fallback 启用，使用 ratio `0.85`。

这些数值只属于 `chris-avatar` Profile 配置，没有进入代码或 SOUL 默认值。后续
模型切换或比例调整仍需显式配置并验证运行时解析结果。
