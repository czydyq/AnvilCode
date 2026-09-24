"""可编程的假 Anthropic Messages API 服务，用于确定性地驱动 AnvilCode 做实验。

存在理由：真实模型不可复现、要花钱、且需要有效 Key。本服务实现 `/v1/messages` 的
SSE 流式协议，行为由一份 JSON 脚本控制，因此「模型在第几步说什么」完全可控，
而 AnvilCode 侧走的是**未改动的真实代码路径**（真实 daemon、真实 IPC、真实工具执行）。

配置经环境变量 `FAKE_MODEL_SCRIPT` 传入（JSON 字符串或文件路径），字段：

    tool_name       第一轮要调用的工具名；缺省则直接给最终回答
    tool_input      该工具的参数对象
    final_text      看到 tool_result 之后返回的最终文本
    summary_text    收到「无 tools」的压缩请求时返回的摘要文本
    input_tokens    message_start 中上报的 input_tokens（用于触发压缩阈值）
    output_tokens   message_delta 中上报的 output_tokens
    thinking        非空则先发一个 thinking block（测 thinking 回传）
    fail_first_n    前 N 次请求以 500 失败（测流重试）
    delay_s         每次请求前的延迟，用于制造「运行中」窗口
    max_tool_turns  最多发多少次 tool_use，超出后直接给最终回答（防死循环）
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_SCRIPT: dict[str, Any] = {
    "tool_name": None,
    "tool_input": {},
    "final_text": "Done.",
    "summary_text": "## 1. Original Goal\nFake summary.",
    "input_tokens": 100,
    "output_tokens": 20,
    "thinking": "",
    "fail_first_n": 0,
    "delay_s": 0.0,
    "max_tool_turns": 1,
}


# 读取并解析实验脚本配置，缺省字段用 DEFAULT_SCRIPT 补齐
def load_script() -> dict[str, Any]:
    raw = os.environ.get("FAKE_MODEL_SCRIPT", "")
    cfg = dict(DEFAULT_SCRIPT)
    if raw:
        if os.path.exists(raw):
            raw = open(raw, encoding="utf-8").read()
        cfg.update(json.loads(raw))
    return cfg


# 组装 SSE 帧：data 行必须是单行 JSON，事件类型同时写在 event: 与 data.type 里
def sse(event_type: str, payload: dict[str, Any]) -> bytes:
    payload = {"type": event_type, **payload}
    return (
        f"event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode()


# 判断消息列表里是否已经出现过 tool_result（即工具已被执行过一轮）
def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return True
    return False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script: dict[str, Any] = DEFAULT_SCRIPT
    # 进程内累计的请求序号，供 fail_first_n 使用
    calls = 0
    # 已发出的 tool_use 轮数，供 max_tool_turns 使用（压缩后模型可能重复调工具）
    tool_turns = 0
    lock = threading.Lock()

    # 记录并抑制默认的 stderr 访问日志，保持实验输出干净
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # 处理 POST /v1/messages：按脚本决定回 tool_use 还是 end_turn，并以 SSE 吐出
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")

        # 控制端点：实验中途切换脚本（换工具、换参数、改 input_tokens），无需重启服务
        if self.path.rstrip("/") == "/__script":
            with _Handler.lock:
                _Handler.script = {**DEFAULT_SCRIPT, **body}
                _Handler.calls = 0
                _Handler.tool_turns = 0
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            payload = json.dumps({"ok": True})
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        with _Handler.lock:
            _Handler.calls += 1
            call_index = _Handler.calls

        cfg = _Handler.script
        if call_index <= int(cfg.get("fail_first_n", 0)):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            payload = json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": "injected"}}
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())
            return

        delay = float(cfg.get("delay_s", 0.0))
        if delay:
            time.sleep(delay)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        for chunk in self._build_stream(body, cfg):
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # 依脚本与当前对话状态生成完整的 SSE 事件序列
    def _build_stream(self, body: dict[str, Any], cfg: dict[str, Any]) -> list[bytes]:
        model = body.get("model", "claude-sonnet-4-6")
        messages = body.get("messages", [])
        tools = body.get("tools") or []
        in_tok = int(cfg.get("input_tokens", 100))
        out_tok = int(cfg.get("output_tokens", 20))

        out: list[bytes] = [
            sse("message_start", {
                "message": {
                    "id": "msg_fake", "type": "message", "role": "assistant",
                    "model": model, "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": in_tok, "output_tokens": 0},
                }
            })
        ]

        def block(index: int, content_block: dict[str, Any]) -> None:
            out.append(sse("content_block_start", {"index": index, "content_block": content_block}))

        def stop(index: int) -> None:
            out.append(sse("content_block_stop", {"index": index}))

        idx = 0
        thinking = cfg.get("thinking") or ""

        # 压缩请求没有 tools，按摘要场景回文本
        if not tools:
            text = str(cfg.get("summary_text", "summary"))
            block(idx, {"type": "text", "text": ""})
            out.append(sse("content_block_delta", {
                "index": idx, "delta": {"type": "text_delta", "text": text}
            }))
            stop(idx)
            return out + self._finish("end_turn", out_tok)

        if thinking:
            block(idx, {"type": "thinking", "thinking": "", "signature": ""})
            out.append(sse("content_block_delta", {
                "index": idx, "delta": {"type": "thinking_delta", "thinking": thinking}
            }))
            out.append(sse("content_block_delta", {
                "index": idx, "delta": {"type": "signature_delta", "signature": "fakesig"}
            }))
            stop(idx)
            idx += 1

        tool_name = cfg.get("tool_name")
        can_tool = (
            tool_name
            and not _has_tool_result(messages)
            and _Handler.tool_turns < int(cfg.get("max_tool_turns", 1))
        )
        if can_tool:
            with _Handler.lock:
                _Handler.tool_turns += 1
            # 第一轮：先流一点解释文本，再发 tool_use（贴近真实模型行为）
            block(idx, {"type": "text", "text": ""})
            out.append(sse("content_block_delta", {
                "index": idx, "delta": {"type": "text_delta", "text": f"Calling {tool_name}."}
            }))
            stop(idx)
            idx += 1

            block(idx, {"type": "tool_use", "id": "toolu_fake_1", "name": tool_name, "input": {}})
            partial = json.dumps(cfg.get("tool_input", {}), ensure_ascii=False)
            out.append(sse("content_block_delta", {
                "index": idx, "delta": {"type": "input_json_delta", "partial_json": partial}
            }))
            stop(idx)
            return out + self._finish("tool_use", out_tok)

        # 终局：返回最终文本
        text = str(cfg.get("final_text", "Done."))
        block(idx, {"type": "text", "text": ""})
        out.append(sse("content_block_delta", {
            "index": idx, "delta": {"type": "text_delta", "text": text}
        }))
        stop(idx)
        return out + self._finish("end_turn", out_tok)

    # 生成 message_delta + message_stop 收尾事件
    @staticmethod
    def _finish(stop_reason: str, out_tok: int) -> list[bytes]:
        return [
            sse("message_delta", {
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": out_tok},
            }),
            sse("message_stop", {}),
        ]


# 启动假模型服务；端口设 0 时由系统分配，实际端口写到 stdout 第一行供父进程读取
def main() -> None:
    host = os.environ.get("FAKE_MODEL_HOST", "127.0.0.1")
    port = int(os.environ.get("FAKE_MODEL_PORT", "0") or "0")
    _Handler.script = load_script()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"PORT={httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
