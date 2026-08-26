# Hermes 长任务 Context Handoff 方案

## 1. 目标

解决 Hermes Agent 在长时间 Coding 任务中因为 Context 持续增长、自动压缩导致的：

- 指令执行偏移
- 任务目标遗失
- 关键决策遗失
- 历史 Tool Trace 污染上下文
- 多次压缩后 Agent 行为不稳定

核心原则：

> **不依赖 Hermes 默认 Context Compression，而由 Agent 自主管理 Context 生命周期。**

整体策略：

```text
长期规则        → SOUL.md / Skills
任务状态        → Task State / Checkpoint
完整历史        → Session / Event Log
当前工作上下文  → Active Context

Context 超出甜区
        ↓
Agent 自主判断
        ↓
生成 Checkpoint
        ↓
handoff_context
        ↓
切换 Active Context
        ↓
下一次 LLM Request 使用全新上下文
        ↓
继续当前 Agent Turn
```

Gateway、当前任务执行 Loop 都不需要中断。

---

# 2. Hermes 中各组件职责

## SOUL.md

保存长期稳定规则，例如：

- Agent 身份
- 行为原则
- 任务处理原则
- Handoff 强约束
- Runtime Status 检查规则

它不保存具体任务状态。

---

## Skills

保存能力与工作流程，例如：

- 需求分析
- Coding Agent 调度
- Code Review
- Task State 维护
- Checkpoint 生成
- 新任务识别
- Handoff 流程

Checkpoint 的生成逻辑主要应该属于 Skill / Agent，而不是 ContextEngine。

---

## Task State

保存当前任务的执行状态。

至少包含：

```text
Task ID
Goal
Constraints
Current Phase
Completed
In Progress
Known Issues
Next Actions
Decisions
Artifacts / Files
```

Task State 是 Handoff 后恢复任务的主要依据。

---

## ContextEngine

ContextEngine 不承担 Task Manager 职责。

它只负责：

1. 控制当前发送给 LLM 的 Context
2. 观察 Context 使用情况
3. 向 Agent 暴露 Context Control Tool
4. 执行 Active Context 切换

可以理解为：

```text
Session / History
        ↓
ContextEngine
        ↓
当前 LLM Request 应该看到什么
```

---

# 3. 禁用 Hermes 默认 Compression

自定义 ContextEngine 后：

```python
def should_compress(self, prompt_tokens):
    return False
```

即：

```text
Hermes 默认：

Context达到阈值
    ↓
自动 Summary Compression


修改后：

Context达到阈值
    ↓
Hermes 不处理
    ↓
由 Agent 自主管理
```

必要时可以额外保留一个接近模型硬上限的 emergency fallback，但正常情况下不进入 Hermes 默认压缩。

---

# 4. Context 使用率实时通知

这是整个方案的重要基础。

Hermes 的 `select_context()` 会在实际向 Provider 发出 LLM Request 前执行，因此一个 User Turn 内连续发生：

```text
LLM
 ↓
Tool
 ↓
LLM
 ↓
Tool
 ↓
LLM
```

每一次新的 LLM Request 都有机会观察 Context 状态。

因此 ContextEngine 可以维护：

```text
last_prompt_tokens
context_limit
context_ratio
```

例如：

```text
prompt_tokens = 108000
context_limit = 200000

context_ratio = 54%
```

---

# 5. Runtime Status 注入方式

不修改 System Prompt。

也不把 Runtime Status 永久写入 Session History。

而是在每一次 LLM Request 构造时，在 Context 尾部**临时追加一条 Runtime Message**：

```text
[Runtime Status]

Context usage: 54%
Context handoff available: true
```

下一次：

```text
[Runtime Status]

Context usage: 57%
Context handoff available: true
```

Agent 永远只需要关注最后一条 Runtime Status。

这样做还有一个重要优势：

## Prompt Cache 友好

原始 Context：

```text
System
SOUL
Skills
History
Tool Results
...
```

全部保持不变。

只有最后几十个 Runtime Status Token 发生变化。

对于 Prefix Cache：

```text
稳定的大段 Prefix    → 可以继续命中缓存
尾部 Runtime Status  → 每次重新计算
```

避免因为动态修改 System Prompt 或重新排序 Messages 导致大规模 Cache Miss。

---

# 6. Handoff 决策权

ContextEngine：

> 只提供 Context 使用率事实。

例如：

```text
Context Usage = 58%
```

是否应该 Handoff，由 Agent 判断。

Agent可以结合：

- Context 使用率
- 当前任务阶段
- 当前推理是否完整
- 当前修改是否已经形成稳定状态
- 是否存在尚未确认的关键结果

决定：

```text
继续当前 Context
```

或者：

```text
执行 Handoff
```

因此实现的是：

> **Runtime 提供观测，Agent 掌握控制权。**

---

# 7. Checkpoint 策略

Checkpoint 不由 ContextEngine 生成。

由 Agent + Skill 负责。

推荐结构：

```markdown
# Task Checkpoint

## Task
task-id / task-name

## Goal
当前任务最终目标

## Constraints
不可违反的约束

## Current Phase
当前执行阶段

## Completed
已经完成且确认有效的内容

## Current State
现在正在处理什么

## Decisions
已经确定的重要技术决策

## Rejected Alternatives
明确放弃的方案及原因

## Known Issues
当前已知问题

## Artifacts
涉及的重要文件、分支、Commit、测试结果

## Next Actions
Handoff 后第一步应该做什么
```

Checkpoint 的目标不是：

> 总结聊天。

而是：

> **让新的 Agent Context 可以继续执行任务。**

因此必须保存“未来执行所需要的信息”，而不是普通摘要。

---

# 8. 决策记录策略

重要决策不能只写：

```text
决定使用方案 A。
```

而应该保存：

```text
Decision:
使用方案 A

Reason:
方案 B 会造成数据库锁竞争

Rejected:
方案 B

Impact:
后续 Cache 层均按照方案 A 实现
```

推荐事件类型：

```text
TASK_CREATED

GOAL_CHANGED

CONSTRAINT_ADDED

DECISION_MADE

DECISION_REVOKED

PHASE_COMPLETED

FILE_CHANGED

TEST_FAILED

TEST_PASSED

CHECKPOINT_CREATED

HANDOFF_COMPLETED
```

长期可以形成：

```text
Event Log
    ↓
Task State
    ↓
Checkpoint
```

Event Log 负责可追溯性。

Task State 负责当前状态。

Checkpoint 负责 Context Handoff。

三者职责不要混在一起。

---

# 9. handoff_context Tool

`handoff_context` 建议直接由自定义 ContextEngine 暴露。

原因是它本质属于：

> Context Control Tool

而不是 Task Tool。

Agent负责：

```text
生成 Checkpoint
        ↓
保存 Task State
        ↓
调用 handoff_context
```

ContextEngine负责：

```text
handoff_context(checkpoint_reference)
        ↓
修改 Active Context 状态
        ↓
下一次 select_context()
        ↓
返回新的 Context
```

ContextEngine并不需要理解 Checkpoint 内部的 Task 语义。

它只需要知道：

```text
checkpoint_reference
active_context_id
handoff_pending
```

---

# 10. Active Context Pointer

ContextEngine 内部维护：

```text
active_context
```

正常状态：

```text
active_context = current_session
```

Handoff 后：

```text
active_context = checkpoint_003
```

然后下一次：

```text
select_context()
```

发现 Active Context 已经改变，就不再返回旧的完整 Conversation Context，而是重新构造：

```text
New Context
```

因此不需要真的删除历史。

只是：

> **旧历史不再进入 Active Context。**

---

# 11. Handoff 后的新 Context

新的 LLM Context 不是简单的：

```text
Checkpoint
```

而应该重新 Bootstrap Hermes Agent。

建议包含：

```text
Hermes System Instructions

        +

SOUL.md

        +

当前任务需要的 Skills

        +

User / Agent 长期 Memory

        +

Task State / Checkpoint

        +

必要的最近 Decisions / Events

        +

Runtime Status
```

同时 Hermes 的：

```text
Tool Schemas
```

继续正常提供，包括：

- Shell
- File
- Git
- Coding CLI
- Memory
- 其他 Hermes 默认 Tool
- handoff_context

这些属于 Runtime 能力，不应该因为 Handoff 丢失。

---

# 12. 不继承的内容

新的 Active Context 原则上不继承大量原始 Tool Trace，例如：

```text
read_file
read_file result
shell
shell result
grep
grep result
edit
edit result
...
```

这些往往是长任务 Context 最大的 Token 消耗来源。

应该将其中真正重要的信息沉淀为：

```text
Task State
Decision
Artifact
Known Issue
Checkpoint
```

然后丢弃原始执行噪声。

---

# 13. 一个 Turn 内完成 Handoff

这是本方案的重要要求。

Handoff 不能依赖：

```text
User Turn结束
        ↓
下一个User Message
        ↓
重新开始
```

因为 Hermes Gateway 中可能存在一个持续数小时的 Agent Turn。

正确流程：

```text
User
 ↓
Agent Loop
 ↓
LLM
 ↓
Tool
 ↓
LLM
 ↓
Runtime Status: 58%
 ↓
Agent判断需要Handoff
 ↓
生成Checkpoint
 ↓
handoff_context
 ↓
ContextEngine切换active_context
 ↓
下一次LLM Request
 ↓
select_context()
 ↓
使用新Context
 ↓
Agent继续执行
 ↓
Tool
 ↓
LLM
 ↓
...
```

整个过程中：

```text
Gateway Connection 不断
Agent Turn 不结束
Task 不结束
```

变化的只有：

```text
LLM Active Context
```

因此更准确地说，这不是传统的 Session Reset，而是：

> **Context Rotation / Context Handoff**

---

# 14. Session 与历史追溯

虽然执行层采用 Active Context Rotation，但完整历史应该保留。

建议逻辑关系：

```text
Task A

Session / Context Segment 001
        ↓
Checkpoint 001
        ↓
Context Segment 002
        ↓
Checkpoint 002
        ↓
Context Segment 003
```

记录：

```text
task_id
context_segment_id
parent_segment_id
checkpoint_id
start_time
end_time
handoff_reason
```

这样既能保证 Agent 当前 Context 足够干净，也能做到后续：

```text
为什么做这个决定？
        ↓
找到 Decision
        ↓
找到对应 Checkpoint
        ↓
追溯原始 Context Segment
```

---

# 15. 新任务识别与 Context 隔离

除了长任务内部 Handoff，还需要处理：

> 当前用户请求是不是一个新任务？

Agent在接收到新的用户目标时，通过 Skill 判断：

### 属于当前任务

例如：

```text
继续修复刚才支付模块的测试问题
```

继续使用：

```text
current_task
```

---

### 属于当前任务的子任务

例如：

```text
顺便检查一下这个支付接口的超时问题
```

建立：

```text
parent_task
    ↓
sub_task
```

但仍然可以共享必要 Task State。

---

### 完全新的任务

例如：

```text
现在帮我看看登录模块的问题
```

Agent应该：

```text
Finalize Current Task State
        ↓
Checkpoint
        ↓
Create New Task
        ↓
handoff_context
        ↓
建立新的Active Context
```

新的 Context 不继承旧任务执行历史。

只继承：

```text
SOUL
Skills
长期Memory
Hermes Runtime / Tools
```

从而避免项目和任务之间互相污染。

---

# 16. 最终职责划分

| 模块 | 职责 |
|---|---|
| SOUL.md | Agent 长期行为与身份 |
| Skills | 工作流程、Checkpoint、新任务识别规则 |
| Task State | 当前任务真实状态 |
| Event Log | 完整决策与关键事件追溯 |
| Checkpoint | 为 Handoff 准备的恢复快照 |
| ContextEngine | Context 观察、选择与切换 |
| `select_context()` | 每次 LLM Request 前构造 Active Context |
| `should_compress()` | 返回 False，关闭 Hermes 默认压缩 |
| Runtime Status | 每次 Request 尾部临时注入 Context 使用率 |
| `handoff_context` | Agent 主动执行 Context Rotation |
| Hermes Gateway | 保持外部机器人会话与执行 Loop 连续 |

---

# 17. 最终运行流程

```text
用户下达任务
        ↓
Agent识别 / 创建Task
        ↓
加载SOUL + Skills + Task State
        ↓
开始Agent Loop
        ↓
每次LLM Request
        ↓
ContextEngine.select_context()
        ↓
尾部注入Runtime Status
        ↓
LLM执行
        ↓
Tool执行
        ↓
继续LLM
        ↓
Context进入甜区
        ↓
Agent结合当前任务状态判断
        ↓
选择合适Handoff节点
        ↓
Skill生成Task Checkpoint
        ↓
记录Decision / Event
        ↓
Agent调用handoff_context
        ↓
ContextEngine更新Active Context
        ↓
下一次select_context()
        ↓
SOUL
+ Skills
+ Memory
+ Checkpoint
+ Relevant Decisions
+ Runtime Status
        ↓
继续当前Agent Loop
```

最终形成：

> **Hermes 负责 Runtime，ContextEngine 负责 Context Control，Skill 负责 Handoff 语义，Task State 负责恢复，Agent 自己决定何时切换。**

核心目标不是“压缩上下文”，而是让 Agent 在无限生命周期中不断运行一段段**短而稳定、可恢复、可追溯的 Context Segment**。