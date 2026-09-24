"""实验脚手架：拉起真实的假模型服务 + 真实 anvil-core 守护进程，并提供 IPC 客户端。

设计取舍：实验**不 mock** AnvilCode 的任何代码，只替换最外层的模型服务。
因此被测对象（双进程架构、IPC 协议、权限审批、事件流、压缩器）全部走生产路径，
被控对象（模型行为）完全确定。这样得到的结论既可复现，又不会被 mock 掩盖。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPERIMENTS_DIR = Path(__file__).resolve().parent


# 申请一个空闲 TCP 端口（随即释放，交给被测进程绑定）
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# 解析仓库根目录 .env，返回其中非空的行式键值对（不引入 python-dotenv 依赖）
def read_dotenv() -> dict[str, str]:
    out: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        if val:
            out[key.strip()] = val
    return out


# 阻塞等待端口可连接，超时抛异常
def wait_port(port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"port {port} not accepting connections within {timeout_s}s")


class FakeModel:
    """假模型服务的子进程包装；支持运行期 /__script 切换脚本。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.port = 0

    def start(self) -> FakeModel:
        self.proc = subprocess.Popen(
            [sys.executable, str(EXPERIMENTS_DIR / "fake_model.py")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT),
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline().strip()
        if not line.startswith("PORT="):
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"fake model failed to start: {line!r} {err}")
        self.port = int(line.removeprefix("PORT="))
        wait_port(self.port)
        return self

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # 热更新模型脚本，返回前确保生效（同步请求，避免与下一次 run 竞争）
    def set_script(self, **fields: Any) -> None:
        import urllib.request

        req = urllib.request.Request(
            f"{self.base_url}/__script",
            data=json.dumps(fields).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class Daemon:
    """真实 anvil-core 子进程；HOME 指向临时目录以隔离 sessions / policy.toml。

    use_fake_model=True 时把模型指向本地假模型服务；
    False 时改用仓库 .env 里的真实网关配置（ANTHROPIC_BASE_URL / API_KEY）。
    """

    def __init__(
        self, fake: FakeModel | None, home: Path,
        config_toml: str = "", *, use_fake_model: bool = True,
    ) -> None:
        self.fake = fake
        self.home = home
        self.config_toml = config_toml
        self.use_fake_model = use_fake_model
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = free_port()

    def start(self) -> Daemon:
        home = self.home
        home.mkdir(parents=True, exist_ok=True)
        if self.config_toml:
            cfg = home / ".anvil" / "config.toml"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(self.config_toml, encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["ANVIL_PORT"] = str(self.port)
        env["ANVIL_LOG_FILE"] = ""
        env["ANVIL_LOG_LEVEL"] = "WARNING"
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env.pop("ANVIL_CONFIG", None)

        if self.use_fake_model:
            assert self.fake is not None
            env["ANTHROPIC_API_KEY"] = "experiment-dummy-key"
            env["ANTHROPIC_BASE_URL"] = self.fake.base_url
        else:
            # 只从 .env 取模型网关相关键：端口/HOME/日志必须由实验控制，
            # 否则 .env 里的 ANVIL_PORT 会把守护进程顶到我分配的空闲端口之外
            for k, v in read_dotenv().items():
                if k.startswith("ANTHROPIC_") or k.startswith("ANVIL_LLM_"):
                    env[k] = v
            # 再次确认实验关键变量，防止后续改动引入覆盖
            env["HOME"] = str(home)
            env["ANVIL_PORT"] = str(self.port)
            env["ANVIL_LOG_FILE"] = ""
            env["ANVIL_CONFIG"] = str(home / ".anvil" / "config.toml")

        self.proc = subprocess.Popen(
            [sys.executable, "-m", "anvil_code.core"],
            env=env, cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        wait_port(self.port)
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    # 读取该 HOME 下某个 run 的 events.jsonl 事件列表
    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        for path in (self.home / ".anvil" / "sessions").glob(f"*/runs/{run_id}/events.jsonl"):
            out = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return out
        return []

    # 返回本次实验中所有 run_id（按目录名，new_run_id 递增，可用作排序）
    def all_run_ids(self) -> list[str]:
        ids = {p.name for p in (self.home / ".anvil" / "sessions").glob("*/runs/*")}
        return sorted(ids)


class IpcClient:
    """NDJSON JSON-RPC 客户端；可后台持续收集服务端推来的事件。"""

    def __init__(self) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self._pump: asyncio.Task[None] | None = None

    async def connect(self, port: int) -> IpcClient:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self._pump = asyncio.create_task(self._read_loop())
        return self

    # 持续读取一行一帧：有 id 的是响应，无 id 且 kind=event 的是推送事件
    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    return
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in msg and msg["id"] in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
                elif msg.get("kind") == "event":
                    self.events.append(msg["event"])
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            return

    # 发送 JSON-RPC 请求并等待对应响应
    async def call(
        self, method: str, params: dict[str, Any], timeout: float = 30.0,
    ) -> dict[str, Any]:
        assert self.writer is not None
        self._next_id += 1
        rid = f"c-{self._next_id}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.writer.write(json.dumps(payload).encode() + b"\n")
        await self.writer.drain()
        return await asyncio.wait_for(fut, timeout=timeout)

    # 带超时地等待某个类型的事件出现，返回该事件（或 None）
    async def wait_event(self, event_type: str, timeout: float = 20.0) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ev in self.events:
                if ev.get("type") == event_type:
                    return ev
            await asyncio.sleep(0.05)
        return None

    # 关闭连接（不带 drain，模拟客户端突然消失）
    def abort(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        if self.writer is not None:
            self.writer.transport.abort()

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass


# 便捷脚手架：临时 HOME + 假模型 + 守护进程；显式 start/stop，也可用 with 语法
class Rig:
    def __init__(self, config_toml: str = "", *, use_fake_model: bool = True) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="anvil-exp-")
        self.home = Path(self.tmp.name)
        self.fake: FakeModel | None = FakeModel() if use_fake_model else None
        self.daemon: Daemon | None = None
        self.config_toml = config_toml
        self.use_fake_model = use_fake_model

    # 启动假模型（如需要）与守护进程，并等待守护进程端口就绪
    def start(self) -> Rig:
        if self.fake is not None:
            self.fake.start()
        self.daemon = Daemon(
            self.fake, self.home, self.config_toml, use_fake_model=self.use_fake_model,
        ).start()
        return self

    # 依次停止守护进程、假模型并清理临时 HOME
    def stop(self) -> None:
        if self.daemon is not None:
            self.daemon.stop()
        if self.fake is not None:
            self.fake.stop()
        self.tmp.cleanup()

    def __enter__(self) -> Rig:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self.daemon is not None
        return self.daemon.port
