"""现场验证：真实模型 + 真实守护进程 + 真实审批交互，走完整 IPC 链路。

与 `run_experiments.py` 的分工：
- run_experiments.py 是**受控实验**（假模型），用来隔离机制、可复现地证明设计主张；
- 本脚本是**现场验证**（真实模型），用来证明同一套机制在真实模型的不确定性下依然成立。

运行：`uv run python experiments/real_model_e2e.py`
需要仓库 .env 中配置好 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL（可指向兼容网关）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import REPO_ROOT, IpcClient, Rig, read_dotenv  # noqa: E402

# 目标设计成必须"读文件 + 执行命令"两步，才能同时压到 read_file、bash 和审批链路
GOAL = (
    "请按顺序完成两件事："
    "1) 用 read_file 工具读取 README.md 的第一行标题；"
    "2) 用 bash 工具执行 `echo hello-from-real-model`；"
    "然后用一句话总结你做了什么。"
)


# 把连续重复的事件类型折叠成 "type ×N"，避免 token 流事件淹没真正有信息量的阶段事件
def compress_types(types: list[str]) -> str:
    out: list[str] = []
    for t in types:
        if out and out[-1][0] == t:
            out[-1][1] += 1
        else:
            out.append([t, 1])
    return " → ".join(f"{t} ×{n}" if n > 1 else t for t, n in out)


# 执行真实模型端到端验证，返回退出码
def main() -> int:
    dotenv = read_dotenv()
    if not dotenv.get("ANTHROPIC_API_KEY"):
        print("SKIP: .env 中缺少 ANTHROPIC_API_KEY，跳出现场验证。")
        return 0

    print(f"网关：{dotenv.get('ANTHROPIC_BASE_URL', '(官方默认)')}")
    # 隔离 HOME，避免污染用户真实的 ~/.anvil；max_steps 收紧以控制真实调用成本
    gateway = dotenv.get("ANTHROPIC_BASE_URL", "(官方默认)")
    rig = Rig(config_toml="[agent]\nmax_steps = 8\n[permission]\ntimeout_s = 120.0\n",
              use_fake_model=False).start()
    try:
        return asyncio.run(_run(rig, gateway))
    finally:
        rig.stop()


# 驱动一次真实 run：自动应答审批，收集事件，最后核对证据并打印
async def _run(rig: Rig, gateway: str) -> int:
    client = await IpcClient().connect(rig.port)
    await client.call("event.subscribe", {"topics": ["*"], "scope": "global"})

    approvals: list[dict[str, Any]] = []
    t0 = time.monotonic()
    run_id = (await client.call("agent.run", {"goal": GOAL}, timeout=30))["result"]["run_id"]
    print(f"run_id = {run_id}\n")

    # 一边等终局一边应答审批：只要出现权限请求就自动放行一次，并记录交互
    finished: dict[str, Any] = {}
    deadline = time.monotonic() + 240
    handled: set[str] = set()
    while time.monotonic() < deadline:
        for ev in list(client.events):
            if ev.get("type") == "permission.requested" and ev["tool_use_id"] not in handled:
                handled.add(ev["tool_use_id"])
                approvals.append(ev)
                print(f"  [审批请求] {ev['tool_name']}  {ev.get('param_preview')}")
                await client.call("permission.respond", {
                    "tool_use_id": ev["tool_use_id"], "decision": "allow_once",
                })
            if ev.get("type") == "tool.call_started":
                print(f"  [工具开始] {ev['tool_name']}")
            if ev.get("type") == "run.finished":
                finished = ev
                break
        if finished:
            break
        await asyncio.sleep(0.2)

    elapsed = time.monotonic() - t0
    events = rig.daemon.run_events(run_id)
    types = [e["type"] for e in events]
    tool_names = [e["tool_name"] for e in events if e["type"] == "tool.call_started"]
    usage = [e for e in events if e["type"] == "llm.usage"]
    final_text = ""

    # 从 session thread 里取出模型最终回答（run.finished 不带正文）
    for thread in (rig.home / ".anvil" / "sessions").glob("*/thread.jsonl"):
        for line in thread.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                import json as _json
                msg = _json.loads(line)
            except Exception:
                continue
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, str):
                    final_text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            final_text = block.get("text", "")

    print("\n" + "=" * 96)
    print("真实模型现场验证报告")
    print("=" * 96)
    print(
        f"终态            : {finished.get('status')}"
        f"（原因={finished.get('reason')}，步数={finished.get('steps')}）"
    )
    print(f"耗时            : {elapsed:.1f} s")
    print(f"事件序列        : {compress_types(types)}")
    print(f"工具调用        : {tool_names}")
    print(f"审批交互        : {len(approvals)} 次，"
          f"{[(a['tool_name'], a.get('param_preview')) for a in approvals]}")
    for i, u in enumerate(usage, 1):
        print(f"第 {i} 次 LLM 用量 : input={u['input_tokens']} output={u['output_tokens']} "
              f"cache_read={u['cache_read_input_tokens']} context_pct={u['context_pct']:.4f}")
    print(f"模型最终回答    : {final_text.strip()[:400]}")

    checks = {
        "run 终态为 success": finished.get("status") == "success",
        "发生至少一次工具调用": len(tool_names) >= 1,
        "事件序列含 run.started 与 run.finished": (
            "run.started" in types and "run.finished" in types
        ),
        "记录了 LLM token 用量": len(usage) >= 1,
    }
    print("-" * 96)
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    ok_all = all(checks.values())
    print(f"\n现场验证结论：{'通过' if ok_all else '未通过'}"
          f"（审批链路{'已被真实触发' if approvals else '未被本次模型触发'}）")

    report = REPO_ROOT / "experiments" / "REPORT_REAL_MODEL.md"
    report.write_text(
        "# 真实模型现场验证报告\n\n"
        f"- 网关：`{gateway}`\n"
        "- 方式：真实 anvil-core 守护进程 + 真实 IPC + 真实模型，实测一次完整 run\n"
        f"- 目标：{GOAL}\n"
        f"- 终态：`{finished.get('status')}`（步数 {finished.get('steps')}）\n"
        f"- 耗时：{elapsed:.1f} s\n"
        f"- 事件序列（连续重复已折叠）：`{compress_types(types)}`\n"
        f"- 工具调用：`{tool_names}`\n"
        f"- 审批相关事件：requested {types.count('permission.requested')} 次，"
        f"granted {types.count('permission.granted')} 次\n"
        f"- 审批交互：{len(approvals)} 次 "
        f"`{[(a['tool_name'], a.get('param_preview')) for a in approvals]}`\n"
        f"- LLM 用量：`{[{'in': u['input_tokens'], 'out': u['output_tokens']} for u in usage]}`\n"
        f"- 模型最终回答：{final_text.strip()[:400]}\n\n"
        "| 检查项 | 结果 |\n|---|---|\n"
        + "\n".join(f"| {n} | {'✅' if v else '❌'} |" for n, v in checks.items())
        + "\n",
        encoding="utf-8",
    )
    print(f"报告已写入 {report}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
