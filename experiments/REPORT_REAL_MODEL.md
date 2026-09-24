# 真实模型现场验证报告

- 网关：`https://api.deepseek.com/anthropic`
- 方式：真实 anvil-core 守护进程 + 真实 IPC + 真实模型，实测一次完整 run
- 目标：请按顺序完成两件事：1) 用 read_file 工具读取 README.md 的第一行标题；2) 用 bash 工具执行 `echo hello-from-real-model`；然后用一句话总结你做了什么。
- 终态：`success`（步数 2）
- 耗时：2.2 s
- 事件序列（连续重复已折叠）：`run.started → step.started → llm.model_selected → llm.token ×7 → llm.usage → tool.call_started → tool.call_finished → tool.call_started → permission.requested → permission.granted → tool.call_finished → step.finished → step.started → llm.model_selected → llm.token ×54 → llm.usage → step.finished → run.finished`
- 工具调用：`['read_file', 'bash']`
- 审批相关事件：requested 1 次，granted 1 次
- 审批交互：1 次 `[('bash', "command='echo hello-from-real-model'")]`
- LLM 用量：`[{'in': 138, 'out': 172}, {'in': 3930, 'out': 64}]`
- 模型最终回答：两件事都已完成：我用 `read_file` 读取了 README.md，其第一行标题是 **`# AnvilCode`**，并用 `bash` 执行 `echo hello-from-real-model`，输出为 `hello-from-real-model`。

| 检查项 | 结果 |
|---|---|
| run 终态为 success | ✅ |
| 发生至少一次工具调用 | ✅ |
| 事件序列含 run.started 与 run.finished | ✅ |
| 记录了 LLM token 用量 | ✅ |
