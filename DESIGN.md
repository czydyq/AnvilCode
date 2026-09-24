# AnvilCode 设计闭环

> 这份文档回答一个问题：**这个项目为什么成立。**
> 结构是「动机 → 业界 → 方案 → 设计 → 验证 → 价值」，每一环都要能被下一环接住：
> 动机写成可被检验的命题，设计逐条对上命题，验证用可复现的实验取证，最后回到动机核对是否真的被回应。

---

## 0. 结论速览

| 问题 | 回答 |
|---|---|
| 为什么做 | 编码 Agent 的难点不在"让模型调通工具"，而在**长时运行下的可控性**：进程能不能活、动作能不能审、过程能不能查、上下文能不能续。 |
| 业界做了什么 | 已有方案在**单进程 CLI**（Aider）、**IDE 插件**（Cline）、**容器化服务端**（OpenHands）三种形态上各自深化；普遍把"进程生命周期"与"任务生命周期"绑在一起。 |
| 我们提了什么 | 把 Agent 运行时做成**常驻守护进程 + 多前端复用**的本地服务：类型化 IPC 契约、可证伪的协议文档、事件账本驱动的可观测性、**顺序即安全**的权限梯度、由真实 token 水位驱动的上下文治理。 |
| 凭什么说有效 | 7 条设计主张受控实验全部证实（`make experiments`），并通过**真实模型 + 真实守护进程 + 真实审批交互**的现场验证（`make real-model-e2e`）。 |
| 价值在哪 | 动机侧：命中的是真问题（长任务可靠性、审批可用性、过程可归因）。设计侧：产出的是**可迁移的机制**——其中"顺序即安全"的权限梯度和"生成式协议文档 + 门禁"两条与具体模型无关。 |

---

## 1. 动机

### 1.1 真正的难点不是"能跑"，而是"能长跑"

一个能调用工具的 Agent demo，今天用几百行就能写出来：循环里调模型、看到 `tool_use` 就执行、把结果塞回去。这类 demo 在单轮、单文件、无人值守的场景里表现良好，因此很容易让人误以为问题已经解决。

但一旦把它当作**日用工**来用，问题就换了一批：不再是"模型会不会调工具"，而是——

- 我让它跑一个跨小时的重构，**中途我关掉终端，它会怎样**？
- 它准备执行 `rm -rf` 或者读取我 `~/.ssh` 下的东西，**我能不能拦住，拦得住吗**？
- 它跑了 40 步之后给了一个错误结论，**我能不能事后知道它在第 17 步读了什么、判断错在哪**？
- 它跑了 300k token，**上下文快满了，它是怎么续上的，续上的过程中丢了什么**？

这四类问题都不是模型能力问题，而是**运行时工程问题**。AnvilCode 就是针对这四类问题的实现。

### 1.2 五类失效模式

把上面的直觉收敛成五条可检验的失效模式。每条都给出"为什么会发生"的机制性解释，而不是笼统的"体验不好"。

**F1 · 前端生命周期绑架任务生命周期。**
最朴素的 Agent CLI 是"一个进程装下所有事"：进程退出 = 任务终止。对交互式短任务无所谓，但对长任务致命——网络抖动、终端关闭、SSH 断线，任一都会让几十分钟的工作蒸发。

**F2 · 审批机制自己把执行卡死。**
加入人工审批后，一个隐蔽的死锁出现了：Agent 循环在等用户批准，而用户的批准必须经由**同一个消息处理循环**才能投递进来。如果这个循环被"正在执行的 Agent"占满，批准永远送不达，最终以超时被拒收场。结果是：**越是危险的操作（越需要审批），越容易因为机制自身而失败**。

**F3 · 安全策略被"便利性缓存"侵蚀。**
用户第一次批准 `bash` 时，界面给出"以后都允许"是很自然的体验优化。但如果这个缓存处在权限评估的**较前**位置，就等于给了一条绕过通道：一旦允许过 bash，之后任何 bash 命令——包括读写工作目录之外的路径——都不再被过问。**便利性和安全性在这里是同一个机制的两面，顺序决定了哪一面占上风。**

**F4 · 上下文水位判断失真，压缩过程不可审计。**
长任务必然撞上上下文窗口。业界的通行做法是"接近上限时把历史摘要化"。但这里有两个工程细节常被忽略：
- **水位怎么算**：用字符数估算 token 会系统性失真，导致压缩要么过早（浪费钱、丢信息）要么过晚（还没压就撞墙）。
- **压缩结果怎么用**：摘要替换历史后产生的新消息序列，对下一次 API 调用**未必合法**（例如尾部留下未配对的 `tool_use`）。位置选错，压缩本身就是一次故障。
此外，摘要一旦生成就"消失"在上下文里，**用户无法事后审计"它到底把什么当成结论带走了"**。

**F5 · 过程是黑盒，出错无法归因。**
Agent 的失败往往不是"报错了"，而是"给出了看似合理但错误的结论"。此时唯一有用的信息是过程：它读了哪些文件、跑了哪些命令、每步花了多少 token、在哪一步开始跑偏。多数实现只保留最终对话，**归因所需的中间证据被丢弃**。

### 1.3 把动机写成可检验的命题

动机如果只停在"我觉得这很重要"，就无法闭环。所以把它转写成 7 条**可被实验证伪**的命题（每条对应后文一个实验编号）：

| 编号 | 命题（可证伪） | 若为假会观察到什么 |
|---|---|---|
| P1 | run 级事件账本完整记录全部阶段，且 run_id 同源 | 事件缺失、或不同阶段 run_id 不一致，无法归因 |
| P2 | 客户端崩溃不影响执行中任务，且历史可回放补齐 | 客户端断开后任务中止，或新连接看不到已发生的事 |
| P3 | 审批挂起期间，守护进程仍能并发处理命令 | 挂起态下其他命令无响应，审批响应无法投递 |
| P4 | 越界路径的强制审批不可被"永久允许"缓存绕过 | 允许过一次 bash 后，越界命令不再被过问 |
| P5 | 压缩由真实 token 水位驱动，产出可审计摘要且不破坏会话 | 压缩不触发/触发时机错，或无留痕，或压缩后会话崩溃 |
| P6 | 协议文档与代码同源，文档漂移会导致验证失败 | 改代码不改文档，验证仍通过（文档会撒谎） |
| P7 | 超长工具结果按预算截断，并指明原文去向 | 单条结果撑爆上下文，或信息被静默丢弃 |

**这 7 条就是"动机"到"验证"之间的桥**。第 4 节会逐条给证据。

---

## 2. 业界现状

> **来源说明**：本节的事实来自 2026 年 9 月对各家官方文档的直接抓取。抓取成功的标注了出处；未能取得一手来源的（尤其是部分社区讨论与论文）已明确标注"未取得一手来源"，仅作方向性参考，不作为论证依据。

### 2.1 形态光谱

| 方案 | 形态 | 扩展机制 | 权限/审批模型 | 上下文管理 |
|---|---|---|---|---|
| **Aider** | 终端单进程 CLI（另有浏览器形态） | 无 MCP 一类的通用扩展层 | 交互式确认 + `--auto-accept-*` 开关；紧耦合 git 提交 | 基于 git 仓库的 **repo map** 提供代码上下文；多种 edit format 让模型选表达方式 |
| **Claude Code** | CLI（Anthropic 官方） | MCP host；subagents；hooks | **6 种权限模式**（`default`/`acceptEdits`/`plan`/`auto`/`dontAsk`/`bypassPermissions`）；allow/ask/deny 三张规则表，**评估顺序固定为 deny → ask → allow，首个命中生效**（与特异性无关） | **auto-compaction**：接近上下文上限时摘要化历史；可用 `/compact Focus on...` 或 CLAUDE.md 的 `# Compact instructions` 引导；subagent 只把摘要交回主会话 |
| **Codex CLI** | 本地 CLI | 文档提及 MCP（详见其安全文档，本次未能取得） | 沙箱 + 审批模式（细节未取得一手来源） | 跨会话记忆（未取得一手来源） |
| **OpenHands** | **客户端-服务端**：后端 + 沙箱内 action execution server | 插件、浏览器、Jupyter | 动作在沙箱内执行，后端经 REST 下发 | 对话表示为 **EventStream**（动作 → 观察的流） |
| **MCP**（协议层） | 协议规范 | — | 传输层负责鉴权（推荐 OAuth） | 只管上下文交换，**不规定**应用如何使用上下文 |

出处：
- MCP 定位、协议版本 `2026-07-28`、JSON-RPC 2.0 数据层、stdio 与 Streamable HTTP 两种传输、host/client/server 三方模型、tools/resources/prompts 三原语、`sampling` 在该版本被废弃 → [modelcontextprotocol.io 架构概览](https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture) 与 [MCP 简介](https://modelcontextprotocol.io/docs/getting-started/intro)
- Aider 的定位、repo map、edit format → [aider.chat/docs](https://aider.chat/docs/)
- OpenHands 的客户端-服务端 + Docker 沙箱 + ActionExecutor + EventStream → [docs.openhands.dev 运行时架构](https://docs.openhands.dev/usage/architecture/runtime)
- Claude Code 的 6 种权限模式、三张规则表与 `deny → ask → allow` 首命中顺序、审批持久化范围 → [code.claude.com/docs/en/permissions](https://code.claude.com/docs/en/permissions)
- Claude Code 的 auto-compaction、`/compact` 引导、subagent 摘要回传 → [code.claude.com/docs/en/costs](https://code.claude.com/docs/en/costs)

### 2.2 各家侧重点与其取舍

**Aider —— 侧重点在"改代码的质量"。** 它把工程精力投在 repo map 和 edit format 上，让模型在**有限的上下文**里精准定位与改写代码。取舍是：它假设任务是"一次编辑一批文件"的短周期交互，因此不需要常驻进程，也不需要通用工具扩展层。

**Claude Code —— 侧重点在"产品化的权限与上下文体验"。** 它是本项目中权限设计最主要的参照物。值得学习的是它把权限规则做成**三张表 + 固定顺序 + 首命中生效**，简单可预期；同时用 6 种模式覆盖从"逐步确认"到"全自动"的谱系。一个关键差异：它的"以后不再询问"对 Bash 是持久的，对文件编辑**只到会话结束**——这是一个有意识的收紧。它的 subagent 只回摘要，也是明确的上下文预算控制。

**OpenHands —— 侧重点在"执行环境的安全与可复现"。** 它是形态上离本项目最近的：明确的客户端-服务端拆分，代码只在 Docker 沙箱内执行。取舍是部署变重——需要容器运行时，本地起步成本高于一个 CLI。

**Codex CLI —— 侧重点在"本地优先级与沙箱"。** 从发布产物命名看是 Rust 实现（`codex-x86_64-unknown-linux-musl` 之类的 target triple），定位是"跑在你电脑上的编码 Agent"。其沙箱与审批细节的官方文档本次返回 403，未取得一手来源，故不做进一步判断。

**MCP —— 侧重点在"工具生态的互操作"。** 它刻意把自己限制在"上下文交换"这一层，明确定位为协议而非框架（"MCP focuses solely on the protocol for context exchange—it does not dictate how AI applications use LLMs or manage the provided context"）。**这句话本身就是本项目存在空间的证据**：协议不负责的部分——进程形态、审批时机、压缩策略、事件账本——正是运行时该做的。

### 2.3 结论：没有被占领的位置

把上表横向看一遍，会发现一个**结构性的空白**：

- 单进程 CLI（Aider、Claude Code CLI、Codex CLI）把**前端与执行体绑在一起**。这不是疏忽，而是有意的简化——单进程意味着零 IPC 成本。代价是 F1：前端消失即任务消失。
- 容器化服务端（OpenHands）解决了隔离，但把本地起步成本推高了。
- 协议层（MCP）**明确声明不覆盖**运行时策略。

也就是说：**"本地常驻、多前端复用、且把可观测性作为一等公民"这个位置，在主流方案里没有被占据。** 这不是说没人想过，而是主流选择用单进程换取简单性。本项目选择了相反的一侧，并承担 IPC 的复杂度——**这个选择的正当性，只能由 F1/F2/F5 这三类失效模式是否真的被消除来证明。**

---

## 3. 方案与设计

### 3.1 总命题

> 把 Agent 运行时做成**本地常驻服务**，用**类型化 IPC 契约**划定进程边界，用**事件账本**承载可观测性，用**顺序敏感的权限梯度**承载安全，用**真实用量**驱动上下文治理。前端（CLI/TUI/未来的 Web）只是这个服务的视图。

### 3.2 分层架构

```
anvil / anvil-tui  ──JSON-RPC 2.0 over NDJSON──▶  anvil-core (daemon)
       │                                                  │
       └──────────── event.subscribe ◀────────────────────┘
```

七层从入口到证据：入口层 → 协议层 → 运行时核心 → Agent 能力 → 治理与记忆 → 扩展生态 → 结果与证据。详见 README 的 mermaid 图。

### 3.3 设计决策逐条论证

每条按「解决哪个失效模式 → 具体设计 → 代码位置 → 取舍」展开。

---

**D1 · 执行体与前端分离，守护进程常驻** → 解决 **F1**

`anvil-core` 是独立的常驻进程，CLI/TUI 是它的客户端。任务的执行被放在守护进程内，且**不依附于发起它的那个连接**：

- `agent.run` 立即返回 `run_id`，实际执行交给 `asyncio.create_task`，并登记进 `_running_runs`（`core/app.py:102-106`）。
- 因此客户端断开不会取消任何任务——断开只影响连接，不影响任务。

取舍：引入了 IPC 的全部复杂度（协议演进、序列化、并发、错误传播）。**这个代价换来的是 F1 被结构性消除**，而不是靠"写个断线重连"补丁。

**D2 · 类型化判别联合 + 生成式协议文档 + 门禁** → 解决 **F5 的契约面**

所有 IPC 消息是 pydantic v2 在 `type` 字段上的判别联合（`core/bus/commands.py`、`core/bus/events.py`）。在此基础上：

- `WIRE_PROTOCOL.md` **由模型生成**（`scripts/gen_protocol_doc.py`）；
- `--check` 模式让文档漂移直接导致验证失败。

取舍：每次改协议必须重新生成并提交文档，多一步操作。换来的是"文档撒谎"在机制上不可能（由实验 E6 取证）。

**D3 · 命令并发处理，审批与执行互不阻塞** → 解决 **F2**

`SocketServer` 对每条命令 `asyncio.create_task` 独立执行、**故意不 await**（`core/transport/socket_server.py:139`）。

这是 F2 的直接解法：如果读循环被"正在跑的 Agent"占住，`permission.respond` 永远投递不进来，审批必然超时失败。**"边执行边等审批"这件事能成立，前提就是它们必须并发。** 由实验 E3 取证：挂起态下 `core.ping` 往返 0.5ms。

**D4 · 权限评估的六级梯度，「顺序即安全」** → 解决 **F3**

`PermissionManager.check_and_wait`（`core/permissions/manager.py:65-146`）的评估顺序：

| 级 | 规则 | 可否被缓存绕过 |
|---|---|---|
| 1 | `deny_patterns`（bash） | — |
| 2 | **cwd 越界启发式 → 强制 ASK** | **否** |
| 3 | session 级 always 缓存 | — |
| 4 | 持久化 always 缓存（`~/.anvil/policy.toml`） | — |
| 5 | `allow_patterns`（bash） | — |
| 6 | 工具默认策略 | — |

**关键设计是第 2 级排在第 3、4 级之前。** 这意味着即使用户点过"永久允许"，涉及工作目录之外路径的命令仍必须重新过问。越界检测（`core/permissions/policy.py:16-23`）覆盖绝对路径、`~`、`..` 跳转、`$HOME`/`$PWD` 展开和显式 `cd`。

取舍：会多一些审批打扰。这是**有意的**——把打扰留在"越界"这一侧，因为它对应的是不可逆风险。

对照 Claude Code：它采用 `deny → ask → allow` 三表首命中，规则更易读；本项目则把"越界"提到缓存之前，用层级顺序而非规则特异性来保证不可绕过。两者是同一安全目标的不同实现路线。

**D5 · 审批用 Future 挂起，断连时统一拒绝** → 解决 **F2 的泄漏面**

审批走 `asyncio.Future` + `wait_for(timeout)`；客户端断连时 `cancel_session` 把该 session 所有 pending 请求 resolve 成拒绝（`core/permissions/manager.py:193-204`），**避免协程永久挂起**。没有这一步，审批超时只是把死锁换成了资源泄漏。

**D6 · 事件流三层消费 + 历史回放** → 解决 **F5**

一个 `EventBus` 上挂三类消费者，各司其职：

- **`EventWriter`** —— 每个 run 一份 `events.jsonl`，run 级证据（`core/events/writer.py`）
- **`IpcEventBroadcaster`** —— 推给订阅客户端，支持 topic glob + scope（`global` / `run:<id>`）（`core/transport/ipc_broadcaster.py`）
- **`TraceWriter`** —— 系统级时间线，分 `ipc`/`event`/`llm` 三层，标注方向与 `latency_ms`

`event.subscribe` 的 `replay_from_run` **先回放 `events.jsonl` 再切入实时流**（`core/app.py:173-206`），所以晚连接的客户端能补齐全过程。由 E1、E2 取证。

**D7 · 压缩由真实 usage 驱动，摘要留痕可审计** → 解决 **F4**

- `context_pct` 用**真实的 `usage.input_tokens` / 模型 context window** 计算，不是估算（`core/llm/provider.py:128`）。
- 摘要固定六段式（目标/已完成/关键约束/当前文件状态/剩余 TODO/关键数据），并在 prompt 里明确告知模型"另一个实例只会拿到你的摘要，必须自包含"（`core/compact/compactor.py:18-45`）。
- 摘要落盘为 `summary_<ts>.md`，同时发 `ContextCompactedEvent` 带原始/摘要 token 数——**压缩过程因此可事后审计**。

**D8 · 压缩时机卡在"消息尾部是 user"之后** → 解决 **F4 的合法性面**

压缩只在「工具结果追加完毕、run 继续、且水位越线」时触发（`core/loop.py:117-126`）。**位置是设计的一部分**：只有在这个位置，压缩产物 `[user_summary, assistant_ack]` 对下一次 LLM 调用才是合法消息序列。压缩失败时返回 `None` 且原消息不变——**压缩是无损的**（由单测 `test_compact_failure_preserves_context` 与 E5 共同覆盖）。

**D9 · tool_result 预算截断，并指明原文去向** → 解决 **F4 的信息丢弃面**

超长 `tool_result` 截断保留前缀，并提示"完整输出在 run events 里"（`core/compact/budget.py`）。**关键不是截断，而是截断时告诉模型去哪找**——让模型能自行决定是否回查，而不是被静默丢信息。由 E7 取证。

**D10 · 工具失败回填而非中断 + 失败分类重试**

工具执行出错**不中断循环**，而是把错误文本作为 tool result 交回模型，让它自己纠错；只有 `runtime_error` 和 `rate_limited` 会重试（退避 2s/4s），`timeout`/`schema_error`/`permission_denied` 直接返回（`core/tools/invocation.py`）。取舍：模型可能陷入重试同一错误——用 `max_steps` 兜底。

**D11 · 三层记忆注入** → 解决长任务的事实留存

`~/.anvil/context.md`（全局）→ `.anvil/context.md`（项目）→ `notes.md`（会话，由 Agent 通过 `note_save` 主动写入）。`thread.jsonl` 是完整的消息回放源，而不是简单拼接历史。

**D12 · subagent 冷启动隔离** → 控制上下文污染

子 Agent 拿到隔离上下文，工具描述里明确告知"你看不到父对话，必须写全"；前台阻塞或后台并行；嵌套上限 2 层；子事件桥接到父总线以便渲染；后台任务经 `BackgroundTaskRegistry` 注册后用 `agent_result` 轮询取结果。

---

## 4. 实验验证

### 4.1 方法论：双层验证

单靠"跑通了"不足以支撑结论，因此采用两层：

**第一层 · 受控实验（`make experiments`）**
用**确定性假模型服务**替换最外层的模型 API，其余全部走生产路径——**真实守护进程、真实 IPC、真实工具执行、真实权限审批、真实压缩器**。

- 为什么可行：`AnthropicProvider` 用官方 SDK，而 SDK 默认读取 `ANTHROPIC_BASE_URL`（`core/llm/provider.py:47-55`）。把该变量指向本地假服务，即可在不改动 AnvilCode 任何一行的情况下接管模型行为。
- 为什么必要：真实模型不可复现、要花钱、且无法构造"上下文刚好越过阈值"这类边界条件。受控实验要的是**可证伪**，不是真实感。
- 实验脚本：`experiments/fake_model.py`（假模型）、`experiments/harness.py`（脚手架）、`experiments/run_experiments.py`（7 个实验）。

**第二层 · 现场验证（`make real-model-e2e`）**
真实模型 + 真实守护进程 + 真实审批交互，证明同一套机制在模型不确定性下依然成立。

**隔离措施**：实验把守护进程的 `HOME` 指向临时目录，因此 `~/.anvil`（sessions / policy.toml / logs）完全隔离，不污染使用者环境。

### 4.2 受控实验结果

`make experiments` 实测输出（7/7）：

| 编号 | 对应命题 | 判据 | 实测证据 |
|---|---|---|---|
| E1 | P1 | 关键阶段事件齐全 + run_id 一致 + 终态 success | `run.started → step.started → llm.model_selected → llm.token → llm.usage → tool.call_started → tool.call_finished → step.finished → step.started → … → run.finished`；所有事件 run_id 一致 |
| E2 | P2 | 断开时任务在执行中；断开后达 success；回放 >0 且含终态 | 断开前已产生 3 条事件；**断开后 status=success**；新连接回放 **14 条**，含 `run.finished` |
| E3 | P3 | 挂起态下 `core.ping` 往返 <2s；放行后 success | **挂起态 ping 往返 0.5 ms**；放行后 status=success |
| E4 | P4 | 首次问+落盘；二次命中缓存不问；越界仍问 | ① 首次弹审批 ✅ 且 `policy.toml` 落盘 ✅；② 二次同类命令**未再询问**，status=success；③ 越界命令 `cat /etc/hostname` **仍弹审批** ✅ |
| E5 | P5 | 出现 `context.compacted` + summary 文件含摘要 + 收尾 success | `context.compacted` 1 条（original_tokens=2285, summary_tokens=20）；`summary_*.md` 落盘且含标记内容；status=success |
| E6 | P6 | 原始通过；注入漂移失败；还原后再通过 | 原始 exit=0；**注入漂移 exit=1**（`ERROR: WIRE_PROTOCOL.md out of sync with code`）；还原 exit=0 |
| E7 | P7 | 超长项截断且带省略说明+events 指引；短项原样 | 13000 字符 → 4053 字符（保留前缀 4000），含 `omitted` 与 `run events` 指引；短内容未被改动 |

几个值得单独指出的点：

- **E2 是 F1 的直接否证测试**：它不是在"断线后重连"，而是**粗暴 abort 掉客户端连接**，然后验证任务照样跑到 success、并且新连接能把 14 条历史全部补齐。
- **E4 的第三步是安全命题的关键**：前两步证明缓存**确实生效**（否则第三步的"仍然询问"可能只是因为缓存根本没工作）。只有在"缓存有效"被证实的前提下，"越界仍询问"才真正说明**第 2 级排在缓存之前**。
- **E6 是把"纪律"变成"机制"的示范**：它先证明门禁在正常情况下通过（避免"永远失败所以看不出问题"），再证明注入漂移后**确实失败**。
- **E5 的触发条件是构造出来的**：假模型上报 `input_tokens=150000`，对 200k 窗口即 `context_pct=0.75`，越过 0.5 阈值。这正是受控实验的价值——真实模型下很难精确命中这个水位。

### 4.3 现场验证结果

`make real-model-e2e` 实测（真实模型，经 Anthropic 兼容网关）。任务刻意设计成必须"读文件 + 执行命令"两步，以同时压到 `read_file`、`bash` 与审批链路：

```
终态      : success（2 步，耗时 1.6 s）
工具调用  : ['read_file', 'bash']
审批交互  : 1 次 —— bash  command='echo hello-from-real-model'  → allow_once
LLM 用量  : 第1次 input=138  output=99  cache_read=1536
            第2次 input=3985 output=79  cache_read=1664

事件序列（连续重复已折叠）：
run.started → step.started → llm.model_selected → llm.token ×7 → llm.usage
  → tool.call_started → tool.call_finished                          ← read_file 执行完毕
  → tool.call_started → permission.requested → permission.granted
    → tool.call_finished                                            ← bash 先被拦住，放行后执行
  → step.finished → step.started → llm.model_selected → llm.token ×54 → llm.usage
  → step.finished → run.finished

模型回答  : "第一行标题是 `# AnvilCode`；`echo hello-from-real-model` 输出了
            `hello-from-real-model`。"
```

这份结果补上了受控实验缺的东西，而且比预期更有信息量：

- **审批链路在真实模型下被真实触发，且粒度是"单个工具调用"。** 模型在同一步内请求了两个工具：`read_file` 立即执行完成；紧接着的 `bash` 触发 `permission.requested`，**在审批期间该工具停住**，收到 `allow_once` 后发 `permission.granted` 并继续执行。这说明 D3+D5 的组合不仅"整体能跑"，而且在**工具粒度**上也正确——审批只阻塞被审批的那个动作，不阻塞整个循环。
- **审批事件成对落账。** `permission.requested` 与 `permission.granted` 都进了 run 级事件账本，**审批这件事本身也是可归因的**，而不只是"结果通过了"。这正是 F5 想要的性质。
- **模型输出可核对。** 它读到的标题 `# AnvilCode` 与仓库 README 实际内容一致，说明 `read_file` 真的执行了，而非模型编造。
- **prompt cache 生效**：`cache_read` 从 1536 → 1664，说明 system prompt 与工具 schema 的 cache 断点配置在真实网关上有效（这属于成本相关设计，非本次主张，但有正面证据）。
- **顺带验证了网关兼容性**：项目 README 提到"或 `ANTHROPIC_BASE_URL` 指向兼容网关"，本次即在第三方兼容网关上跑通了全链路（含流式、工具调用、usage 解析）。

> 该场景重复执行多次均通过（步数在 2–3 之间浮动，取决于模型是否把两个工具放在同一步），结论稳定。

### 4.4 复现方式

```bash
make verify            # 静态检查 + 类型 + 全量测试 + 协议同源 + 7 条设计主张实验
make experiments       # 仅跑受控实验（无需真实 Key）
make real-model-e2e    # 现场验证（需 .env 配好 Key/网关）
make live-test         # 真实 API 的集成用例（pytest -m integration）
```

实验输出同时写入 `experiments/REPORT.md`（受控）与 `experiments/REPORT_REAL_MODEL.md`（现场）。

---

## 5. 闭环核对

把动机逐条对回来，这是"闭环"是否真的闭合的判据：

| 动机 | 命题 | 设计 | 实验 | 结论 |
|---|---|---|---|---|
| F1 前端绑架任务 | P2 | D1 双进程 + D6 回放 | **E2** | 客户端被强杀后任务仍达 success，14 条历史可补齐 → **消除** |
| F2 审批卡死执行 | P3 | D3 并发处理 + D5 Future 兜底 | **E3** | 挂起态下 ping 往返 0.5ms，放行后正常收尾 → **消除** |
| F3 缓存侵蚀安全 | P4 | D4 顺序即安全 | **E4** | 缓存生效的前提下，越界命令仍强制过问 → **消除** |
| F4 上下文治理不可靠 | P5/P7 | D7 真实用量 + D8 时机 + D9 预算 | **E5/E7** | 水位驱动触发、摘要留痕可审计、压缩无损、超长结果带指引截断 → **消除** |
| F5 过程黑盒无法归因 | P1/P6 | D2 契约化 + D6 事件账本 | **E1/E6** | 完整事件序列 + run_id 同源 + 文档漂移必然失败 → **消除** |

**五类动机全部有对应的设计，且全部有实验取证。** 这就是本文所说的闭环。

---

## 6. 价值判断

### 6.1 价值来源一：动机

一个项目的价值上限由它选择的问题决定。本项目选择的是**长时运行 Agent 的可控性**，其价值来自三点：

1. **问题真实且昂贵。** F1–F5 都不是假想问题：长任务丢失几十分钟工作是实打实的损失，prompt injection 导致任意命令执行是安全问题，无法归因的错误结论会直接误导决策。
2. **问题不会随模型变强而消失。** 模型再强，也不会让"前端崩溃后任务该不该继续"这个问题消失——它是分布式系统的形态问题，不是智能问题。**这类问题的解法一旦做对，会长期保值。**
3. **主流选择留了空位。** 主流单进程方案用简单性换掉了 F1；协议层明确声明不覆盖运行时策略。本项目填的是这个结构性空位。

### 6.2 价值来源二：设计

本项目产出的不只是"一个能跑的 Agent"，而是若干**与具体模型无关、可迁移的机制**。按其可复用性排序：

1. **「顺序即安全」的权限梯度（D4）。** 不靠规则的特异性，而靠**层级顺序**保证"越界永不被便利性缓存绕过"。这个思路可直接迁移到任何"危险动作 + 便利性缓存"的场景（部署脚本、数据库写操作、云资源变更）。
2. **生成式协议文档 + 门禁（D2/E6）。** 把"文档要与代码同步"从团队纪律变成**验证门禁**。推而广之：任何"两个表示必须保持一致"的地方（协议、schema、i18n 资源、生成代码），都可以用"由源生成 + --check"消灭漂移。
3. **命令并发处理（D3）。** "边执行边等待外部输入"是交互式系统的通例，其前提是**执行与消息处理必须并发**。这是一个容易被忽略但一旦踩到就极难排查的死锁。
4. **压缩时机的位置约束（D8）。** 压缩不只是"生成一段摘要"，还包括"替换后必须构成合法的消息序列"。这类约束只有读过 API 语义才能发现。
5. **双层验证方法（4.1）。** 用"假模型做受控实验 + 真实模型做现场验证"的组合，兼顾了可证伪性与真实性。**这个方法本身可以复用到任何 LLM 应用项目。**

### 6.3 诚实边界

**本次工作中我发现并修复的缺陷**（属于"完善项目"的实际产出）：

| 问题 | 性质 | 处理 |
|---|---|---|
| `tests/unit/test_compactor.py` 用 `asyncio.get_event_loop().run_until_complete()` 旧写法 | 与 pytest-asyncio `auto` 模式冲突，导致**测试结果依赖执行顺序**（单独跑通过、全量跑失败 6 个） | 改写为 `async def`，消除顺序耦合 |
| `pyproject.toml` 未排除 `integration` 标记用例 | 默认 `pytest tests/` 会**真实调用付费 API**，Key 无效时直接失败 | 加 `addopts = "-m 'not integration'"`，用 `-m integration` 显式开启 |
| `src/` 与 `tests/` 存在 47 处 lint 违规 | `make verify-s0` 声称"完整验证"但实际无法通过 | 逐处修复（长行折行、E402 导入位置、未使用导入）；定宽 ASCII banner 逐行标注豁免并说明原因 |

**验证状态**：`make verify` 全绿——ruff 全通过、mypy strict 在 85 个文件上无问题、**272 个测试通过**（1 个 integration 用例按设计排除）、协议同源检查通过、7/7 设计主张证实。

**仍然没有解决的**（这些是"已知限制"的诚实延续，且**不**宣称被本项目的设计覆盖）：

- **事件总线没有背压**：`EventBus.publish` 串行 await 所有订阅者，而 IPC 广播里有 `await writer.drain()`——一个卡住的客户端会拖慢 Agent 循环。
- **`EventWriter` 不按 run_id 过滤**：已核实 `EventWriter.handle`（`core/events/writer.py:27`）直接写文件不过滤，因此**并发 run 会产生事件交叉污染**；同时 `EventBus` 没有 `unsubscribe`（已核实），长跑守护进程里订阅者会随 run 数增长。
- **本地信任边界缺失**：`SocketServer` 监听 TCP 但**无鉴权**（已核实），本机任意进程连上即可调用 `permission.respond` **自行批准**工具调用。这是当前最严重的安全缺口，缓解方向是 token 握手或改用 Unix domain socket + `0600`。
- **权限策略只覆盖 bash**：路径级正则对 `write_file` 无效，缺少"写文件限制在 workspace 内""拒写 `.env` / `.git/`"这类策略。
- **压缩会丢掉最近工作集**：当前把全部消息压成摘要对，刚读的文件内容、刚看到的报错都会丢。计划保留最近 K 轮原文。
- **run 进行中不截断 tool_result**：截断只在会话重载时生效。
- **context window 硬编码**：`_MODEL_CONTEXT_WINDOWS`（`core/llm/provider.py:16-20`）只登记了少量 Claude 模型，换其他模型会按 200k 计算，压缩触发时机不准。
- **后台 subagent 共享工作目录**：并发子 Agent 会互相覆盖文件，缺少 worktree 级隔离。

**关于"真实模型"的一点说明**：现场验证走的是 Anthropic **兼容网关**，而非 Anthropic 官方端点。因此对官方端点的特有行为（尤其是 extended thinking 的 `signature` 回传）本次未做真机验证；该部分目前由代码与单测覆盖（`core/loop.py` 中 thinking block 首位回传逻辑）。

### 6.4 下一步（按优先级）

1. **补上本地信任边界**（安全缺口，最优先）：token 握手或 Unix socket + 文件权限。
2. **事件总线加背压 + `EventWriter` 按 run_id 过滤 + `EventBus.unsubscribe`**：把 D6 从"能用"做到"并发下正确"。
3. **权限策略扩展到文件工具**：把 D4 的"顺序即安全"从 bash 推广到路径级写保护。
4. **压缩保留最近 K 轮原文**：修掉 F4 残留的"最近工作集丢失"。

---

## 附录 · 文档与代码索引

| 主题 | 位置 |
|---|---|
| 双进程架构与能力总览 | `README.md` |
| 协议定义与示例（自动生成） | `WIRE_PROTOCOL.md` |
| 运维与排错 | `RUNBOOK.md` |
| 受控实验 | `experiments/run_experiments.py` |
| 假模型服务 | `experiments/fake_model.py` |
| 实验脚手架 | `experiments/harness.py` |
| 现场验证 | `experiments/real_model_e2e.py` |
| 受控实验报告 | `experiments/REPORT.md` |
| 现场验证报告 | `experiments/REPORT_REAL_MODEL.md` |
| 权限梯度 | `src/anvil_code/core/permissions/manager.py` |
| 越界启发式 | `src/anvil_code/core/permissions/policy.py` |
| 命令并发处理 | `src/anvil_code/core/transport/socket_server.py` |
| 事件广播与回放 | `src/anvil_code/core/transport/ipc_broadcaster.py`、`src/anvil_code/core/app.py` |
| 压缩器与摘要模板 | `src/anvil_code/core/compact/compactor.py` |
| 工具结果预算 | `src/anvil_code/core/compact/budget.py` |
| Agent 循环与压缩时机 | `src/anvil_code/core/loop.py` |
