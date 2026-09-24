"""AnvilCode 设计主张的实验验证。

每个实验针对一条**可证伪的架构主张**，用真实守护进程 + 真实 IPC + 确定性假模型取证。
运行：`uv run python experiments/run_experiments.py`

设计原则：
1. 不 mock 被测代码 —— 只替换最外层的模型服务，其余走生产路径。
2. 每个实验给出可核对的证据（事件序列、耗时、文件内容），而非只给结论。
3. 结论用「主张 → 判据 → 证据」三段式呈现，判据在实验前就写死在代码里。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO_ROOT, IpcClient, Rig  # noqa: E402


@dataclass
class Result:
    key: str
    claim: str
    criterion: str
    passed: bool
    evidence: str
    details: dict[str, Any] = field(default_factory=dict)


# ── 实验 1：run 级事件流是完整、同源、可归因的证据链 ────────────────────────────
# 主张：一次 run 的全部阶段都落进同一份 events.jsonl，且 run_id 全局一致，
#       因此任何一次执行都能被事后逐阶段归因（而不是只有最终输出）。
# 判据：事件类型序列包含全部关键阶段，且所有事件的 run_id 相同。
async def exp_event_ledger() -> Result:
    rig = Rig().start()
    try:
        rig.fake.set_script(
            tool_name="read_file", tool_input={"path": "README.md"},
            final_text="The project is AnvilCode.", input_tokens=500,
        )
        client = await IpcClient().connect(rig.port)
        await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})
        run_id = (await client.call("agent.run", {"goal": "report the project name"}))["result"][
            "run_id"
        ]
        await client.wait_event("run.finished", timeout=25)
        await asyncio.sleep(0.3)

        events = rig.daemon.run_events(run_id)
        types = [e["type"] for e in events]
        required = [
            "run.started", "step.started", "llm.model_selected", "llm.usage",
            "tool.call_started", "tool.call_finished", "step.finished", "run.finished",
        ]
        missing = [t for t in required if t not in types]
        same_run = all(e.get("run_id") == run_id for e in events)
        finished = events[-1] if events else {}
        passed = not missing and same_run and finished.get("status") == "success"

        await client.close()
        return Result(
            key="E1",
            claim="run 级事件流是完整可归因的证据链",
            criterion="关键阶段事件齐全 + 所有事件 run_id 一致 + 终态 success",
            passed=passed,
            evidence=(
                f"序列={' → '.join(types)}；run_id 一致={same_run}；"
                f"status={finished.get('status')}"
            ),
            details={"event_types": types, "missing": missing},
        )
    finally:
        rig.stop()


# ── 实验 2：双进程隔离 —— 客户端消失不中断任务，且事后能补齐事件 ──────────────
# 主张：执行体与前端解耦后，前端崩溃/断网不会带走正在跑的任务；新客户端可用
#       replay_from_run 回放历史补齐，从而"晚连接也能看到全过程"。
# 判据：强杀客户端连接后 run 仍跑到 success；新连接回放到 >0 条历史且含 run.finished。
async def exp_durability_replay() -> Result:
    rig = Rig().start()
    try:
        # delay_s 撑开"运行中"窗口，确保客户端是在任务执行期间被强杀的
        rig.fake.set_script(
            tool_name="read_file", tool_input={"path": "README.md"},
            final_text="survived.", input_tokens=500, delay_s=1.0,
        )
        client = await IpcClient().connect(rig.port)
        await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})
        run_id = (await client.call("agent.run", {"goal": "long task"}))["result"]["run_id"]

        await asyncio.sleep(0.6)          # 确认任务已在执行中
        mid_events = rig.daemon.run_events(run_id)
        client.abort()                    # 粗暴断开，不发送任何关闭语义

        # 轮询磁盘上的事件账本，确认 run 在客户端消失后仍在推进并完成
        finished: dict[str, Any] = {}
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            evs = rig.daemon.run_events(run_id)
            if evs and evs[-1]["type"] == "run.finished":
                finished = evs[-1]
                break
            await asyncio.sleep(0.2)

        # 新客户端接入，回放该 run 的历史
        late = await IpcClient().connect(rig.port)
        sub = await late.call("event.subscribe", {
            "topics": ["*"], "scope": "global", "replay_from_run": run_id,
        })
        replayed = sub["result"]["replayed_count"]
        await asyncio.sleep(0.4)
        replayed_types = [e["type"] for e in late.events]

        passed = (
            len(mid_events) > 0                 # 断开前确实在跑
            and finished.get("status") == "success"   # 断开后跑完了
            and replayed > 0                    # 回放有数据
            and "run.finished" in replayed_types      # 补到了终态
        )
        await late.close()
        return Result(
            key="E2",
            claim="客户端崩溃不影响执行，且历史事件可回放补齐",
            criterion="客户端断开时任务在执行中；断开后 run 达 success；新连接回放条数>0 且含终态",
            passed=passed,
            evidence=(
                f"断开前已产生 {len(mid_events)} 条事件；断开后 status={finished.get('status')}；"
                f"回放 {replayed} 条，含 run.finished={'run.finished' in replayed_types}"
            ),
            details={"mid_events": len(mid_events), "replayed_types": replayed_types},
        )
    finally:
        rig.stop()


# ── 实验 3：审批挂起不阻塞命令处理（并发读循环）────────────────────────────────
# 主张：run 阻塞在"等用户审批"时，守护进程仍能处理其他命令（含审批响应本身）。
#       这是审批机制能成立的前提——若读循环被 run 占住，审批响应永远投递不进来。
# 判据：等待审批期间发起 core.ping 能在 2s 内返回；随后审批放行并跑到 success。
async def exp_approval_concurrency() -> Result:
    rig = Rig(config_toml="[permission]\ntimeout_s = 8.0\n").start()
    try:
        rig.fake.set_script(
            tool_name="bash", tool_input={"command": "echo experiment"},
            final_text="command finished.", input_tokens=500,
        )
        client = await IpcClient().connect(rig.port)
        await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})
        await client.call("agent.run", {"goal": "echo something"})

        asked = await client.wait_event("permission.requested", timeout=15)

        # 关键测点：run 正处于挂起态，此时同一条连接上再发一个命令
        t0 = time.monotonic()
        pong = await client.call("core.ping", {"client": "probe"}, timeout=5.0)
        ping_ms = (time.monotonic() - t0) * 1000

        await client.call("permission.respond", {
            "tool_use_id": asked["tool_use_id"], "decision": "allow_once",
        })
        fin = await client.wait_event("run.finished", timeout=20)

        passed = (
            asked is not None
            and pong.get("result", {}).get("uptime_ms") is not None
            and ping_ms < 2000
            and (fin or {}).get("status") == "success"
        )
        await client.close()
        return Result(
            key="E3",
            claim="审批挂起期间守护进程仍并发处理命令（含审批响应本身）",
            criterion="挂起态下 core.ping 往返 <2s；审批放行后 run 达 success",
            passed=passed,
            evidence=(
                f"挂起态 ping 往返 {ping_ms:.1f} ms；审批通过后 status={(fin or {}).get('status')}"
            ),
            details={"ping_ms": round(ping_ms, 1)},
        )
    finally:
        rig.stop()


# ── 实验 4：越界路径强制审批不可被"永久允许"缓存绕过 ─────────────────────────
# 主张：权限评估的层级顺序（deny → 越界强制 ASK → 缓存 → 默认）本身是安全设计，
#       即使用户点过"永久允许"，涉及 cwd 之外路径的命令仍必须重新过问。
# 判据：① 首次 bash → 弹审批；② 再次同类命令 → 命中缓存不再问；
#       ③ 换成越界命令 → 依然弹出审批。
async def exp_policy_tiers() -> Result:
    rig = Rig(config_toml="[permission]\ntimeout_s = 8.0\n").start()
    try:
        client = await IpcClient().connect(rig.port)
        await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})

        # ① 首次 in-cwd 命令：应触发审批，回答"永久允许"
        rig.fake.set_script(tool_name="bash", tool_input={"command": "echo first"},
                            final_text="ok1", input_tokens=500)
        await client.call("agent.run", {"goal": "run in-cwd"})
        asked1 = await client.wait_event("permission.requested", timeout=15)
        await client.call("permission.respond", {
            "tool_use_id": asked1["tool_use_id"], "decision": "always_allow",
        })
        await client.wait_event("run.finished", timeout=20)
        await asyncio.sleep(0.4)
        policy_file = rig.home / ".anvil" / "policy.toml"
        policy_text = policy_file.read_text(encoding="utf-8") if policy_file.exists() else ""
        cached = 'bash = "allow"' in policy_text

        # ② 再次同类命令：应命中缓存，不再弹审批
        client.events.clear()
        rig.fake.set_script(tool_name="bash", tool_input={"command": "echo second"},
                            final_text="ok2", input_tokens=500)
        r2 = (await client.call("agent.run", {"goal": "run in-cwd again"}))["result"]["run_id"]
        await client.wait_event("run.finished", timeout=20)
        await asyncio.sleep(0.4)
        e2 = rig.daemon.run_events(r2)
        asked_again = any(e["type"] == "permission.requested" for e in e2)
        r2_status = e2[-1].get("status") if e2 else None

        # ③ 越界命令：缓存不得生效，必须重新审批
        client.events.clear()
        rig.fake.set_script(tool_name="bash", tool_input={"command": "cat /etc/hostname"},
                            final_text="ok3", input_tokens=500)
        await client.call("agent.run", {"goal": "read outside cwd"})
        asked3 = await client.wait_event("permission.requested", timeout=15)
        if asked3 is not None:
            await client.call("permission.respond", {
                "tool_use_id": asked3["tool_use_id"], "decision": "deny_once",
            })
        await client.wait_event("run.finished", timeout=20)

        passed = (
            asked1 is not None and cached            # ① 首次问 + 策略落盘
            and not asked_again and r2_status == "success"  # ② 缓存生效
            and asked3 is not None                   # ③ 越界仍强制问
        )
        await client.close()
        return Result(
            key="E4",
            claim="越界路径强制审批的位置先于缓存，故不可被「永久允许」绕过",
            criterion="首次问+落盘；二次同类命中缓存不问；越界命令仍问",
            passed=passed,
            evidence=(
                f"① 首次弹审批={asked1 is not None}，policy.toml 落盘={cached}；"
                f"② 二次命中缓存（未再询问={not asked_again}, status={r2_status}）；"
                f"③ 越界命令仍弹审批={asked3 is not None}"
            ),
            details={"policy_text": policy_text.strip()},
        )
    finally:
        rig.stop()


# ── 实验 5：上下文水位触发自动压缩，且压缩留痕可审计 ─────────────────────────
# 主张：压缩由**真实的 usage.input_tokens / context_window** 驱动（不是估算），
#       触发后产出固定六段式交接摘要并留痕，且压缩本身失败不会破坏上下文。
# 判据：input_tokens 越过阈值 → 出现 context.compacted 事件 + summary_*.md 落盘，
#       且 run 仍能正常收尾。
async def exp_compaction() -> Result:
    rig = Rig(config_toml="[compaction]\nauto_threshold = 0.5\n").start()
    try:
        marker = "## 1. Original Goal\nEXP-SUMMARY-MARKER"
        # 200k 上下文窗口上报 150k → context_pct=0.75 ≥ 0.5，必然触发
        rig.fake.set_script(
            tool_name="read_file", tool_input={"path": "README.md"},
            final_text="continued after compaction.", input_tokens=150_000,
            summary_text=marker,
        )
        client = await IpcClient().connect(rig.port)
        await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})
        run_id = (await client.call("agent.run", {"goal": "big task"}))["result"]["run_id"]
        await client.wait_event("run.finished", timeout=25)
        await asyncio.sleep(0.4)

        events = rig.daemon.run_events(run_id)
        compacted = [e for e in events if e["type"] == "context.compacted"]
        summaries = list((rig.home / ".anvil" / "sessions").glob("*/summary_*.md"))
        content = summaries[0].read_text(encoding="utf-8") if summaries else ""
        status = events[-1].get("status") if events else None

        passed = (
            len(compacted) >= 1
            and len(summaries) >= 1
            and "EXP-SUMMARY-MARKER" in content
            and status == "success"
        )
        ev = compacted[0] if compacted else {}
        await client.close()
        return Result(
            key="E5",
            claim="压缩由真实 token 水位驱动，产出可审计的六段式摘要且不破坏会话",
            criterion="出现 context.compacted + summary 文件含摘要内容 + run 收尾 success",
            passed=passed,
            evidence=(
                f"context.compacted 事件 {len(compacted)} 条"
                f"（original_tokens={ev.get('original_tokens')}, "
                f"summary_tokens={ev.get('summary_tokens')}）；"
                f"summary 文件 {len(summaries)} 个且含标记内容；status={status}"
            ),
        )
    finally:
        rig.stop()


# ── 实验 6：协议文档同源检查是可证伪的（文档漂移会真的失败）──────────────────
# 主张："文档不会撒谎"不是靠纪律，而是靠机制：文档由模型生成，且有 --check 门禁，
#       任何未被同步的协议变更都会让验证失败。
# 判据：未改动时 --check 通过；人为篡改文档后 --check 必须失败；还原后恢复通过。
def exp_protocol_drift() -> Result:
    doc = REPO_ROOT / "WIRE_PROTOCOL.md"
    original = doc.read_text(encoding="utf-8")

    def run_check() -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, "scripts/gen_protocol_doc.py", "--check"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    try:
        clean_code, clean_msg = run_check()
        doc.write_text(original + "\n<!-- injected drift -->\n", encoding="utf-8")
        drift_code, drift_msg = run_check()
    finally:
        doc.write_text(original, encoding="utf-8")
        restored_code, _ = run_check()

    passed = clean_code == 0 and drift_code != 0 and restored_code == 0
    return Result(
        key="E6",
        claim="协议文档同源检查可证伪：文档漂移会真的导致验证失败",
        criterion="原始状态通过；注入漂移后失败；还原后再次通过",
        passed=passed,
        evidence=(
            f"原始 exit={clean_code}；注入漂移 exit={drift_code}"
            f"（{drift_msg.splitlines()[0] if drift_msg else ''}）；还原 exit={restored_code}"
        ),
    )


# ── 实验 7：超长工具结果被截断且指明原文去向 ─────────────────────────────────
# 主张：单条超长 tool_result 不会无限占用上下文，截断保留前缀并告知完整输出去哪找，
#       使模型能自行决定是否回查，而不是被静默丢信息。
# 判据：超过 limit 的内容被截为前缀 + 省略说明，且说明里包含 events 指引；短内容不动。
def exp_tool_result_budget() -> Result:
    from anvil_code.core.compact.budget import (
        TOOL_RESULT_KEEP,
        TOOL_RESULT_LIMIT,
        truncate_tool_results,
    )

    huge = "A" * (TOOL_RESULT_LIMIT + 5000)
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": huge},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "short"},
        ]},
    ]
    out = truncate_tool_results(messages)
    long_block = out[0]["content"][0]["content"]
    short_block = out[1]["content"][0]["content"]

    passed = (
        len(long_block) < len(huge)
        and long_block.startswith("A" * TOOL_RESULT_KEEP)
        and "omitted" in long_block
        and "run events" in long_block
        and short_block == "short"
    )
    return Result(
        key="E7",
        claim="超长工具结果按预算截断，并保留前缀与原文检索指引",
        criterion="超长项被截断且带省略说明+events 指引；未超限项原样保留",
        passed=passed,
        evidence=(
            f"原始 {len(huge)} 字符 → 截断后 {len(long_block)} 字符"
            f"（保留前缀 {TOOL_RESULT_KEEP}）；短内容未被改动={short_block == 'short'}"
        ),
    )


# 顺序执行所有实验，打印结论表并返回是否存在失败项
def main() -> int:
    results: list[Result] = []

    async def run_async() -> None:
        results.append(await exp_event_ledger())
        results.append(await exp_durability_replay())
        results.append(await exp_approval_concurrency())
        results.append(await exp_policy_tiers())
        results.append(await exp_compaction())

    asyncio.run(run_async())
    results.append(exp_protocol_drift())
    results.append(exp_tool_result_budget())

    print("\n" + "=" * 100)
    print("AnvilCode 设计主张验证报告")
    print("=" * 100)
    width = shutil.get_terminal_size((100, 24)).columns
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"\n[{mark}] {r.key}  {r.claim}")
        print(f"       判据：{r.criterion}")
        print(f"       证据：{r.evidence}")

    ok = sum(1 for r in results if r.passed)
    print("\n" + "-" * min(width, 100))
    print(f"合计：{ok}/{len(results)} 条主张被实验证实")

    report = REPO_ROOT / "experiments" / "REPORT.md"
    lines = [
        "# 设计主张验证报告", "",
        "由 `uv run python experiments/run_experiments.py` 自动生成。",
        "实验驱动真实守护进程与真实 IPC，仅将模型服务替换为确定性假模型。", "",
        f"**合计 {ok}/{len(results)} 条主张被证实。**", "",
        "| 编号 | 主张 | 判据 | 结果 | 证据 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ev = r.evidence.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.key} | {r.claim} | {r.criterion} | "
            f"{'✅ 通过' if r.passed else '❌ 失败'} | {ev} |"
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {report}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
