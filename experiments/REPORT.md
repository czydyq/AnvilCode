# 设计主张验证报告

由 `uv run python experiments/run_experiments.py` 自动生成。
实验驱动真实守护进程与真实 IPC，仅将模型服务替换为确定性假模型。

**合计 7/7 条主张被证实。**

| 编号 | 主张 | 判据 | 结果 | 证据 |
|---|---|---|---|---|
| E1 | run 级事件流是完整可归因的证据链 | 关键阶段事件齐全 + 所有事件 run_id 一致 + 终态 success | ✅ 通过 | 序列=run.started → step.started → llm.model_selected → llm.token → llm.usage → tool.call_started → tool.call_finished → step.finished → step.started → llm.model_selected → llm.token → llm.usage → step.finished → run.finished；run_id 一致=True；status=success |
| E2 | 客户端崩溃不影响执行，且历史事件可回放补齐 | 客户端断开时任务在执行中；断开后 run 达 success；新连接回放条数>0 且含终态 | ✅ 通过 | 断开前已产生 3 条事件；断开后 status=success；回放 14 条，含 run.finished=True |
| E3 | 审批挂起期间守护进程仍并发处理命令（含审批响应本身） | 挂起态下 core.ping 往返 <2s；审批放行后 run 达 success | ✅ 通过 | 挂起态 ping 往返 1.0 ms；审批通过后 status=success |
| E4 | 越界路径强制审批的位置先于缓存，故不可被「永久允许」绕过 | 首次问+落盘；二次同类命中缓存不问；越界命令仍问 | ✅ 通过 | ① 首次弹审批=True，policy.toml 落盘=True；② 二次命中缓存（未再询问=True, status=success）；③ 越界命令仍弹审批=True |
| E5 | 压缩由真实 token 水位驱动，产出可审计的六段式摘要且不破坏会话 | 出现 context.compacted + summary 文件含摘要内容 + run 收尾 success | ✅ 通过 | context.compacted 事件 1 条（original_tokens=2405, summary_tokens=20）；summary 文件 1 个且含标记内容；status=success |
| E6 | 协议文档同源检查可证伪：文档漂移会真的导致验证失败 | 原始状态通过；注入漂移后失败；还原后再次通过 | ✅ 通过 | 原始 exit=0；注入漂移 exit=1（ERROR: /home/chenziyu04916/projects/AnvilCode/WIRE_PROTOCOL.md out of sync with code — run: make docs）；还原 exit=0 |
| E7 | 超长工具结果按预算截断，并保留前缀与原文检索指引 | 超长项被截断且带省略说明+events 指引；未超限项原样保留 | ✅ 通过 | 原始 13000 字符 → 截断后 4053 字符（保留前缀 4000）；短内容未被改动=True |
