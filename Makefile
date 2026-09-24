.PHONY: lint test integration-test docs experiments real-model-e2e verify verify-s0

lint:
	uv run ruff check src tests scripts experiments
	uv run mypy src

test:
	uv run pytest tests/unit -v

integration-test:
	uv run pytest tests/integration -v

# 需要真实 API Key（integration 标记默认被 pytest addopts 排除）
live-test:
	uv run pytest -m integration -v

docs:
	uv run python scripts/gen_protocol_doc.py

# 受控实验：假模型驱动真实守护进程，验证各条设计主张
experiments:
	uv run python experiments/run_experiments.py

# 现场验证：真实模型 + 真实守护进程 + 真实审批交互
real-model-e2e:
	uv run python experiments/real_model_e2e.py

# 完整验证门：静态检查 + 类型 + 全量测试（不含真实 API 用例）+ 协议同源 + 设计主张实验
verify:
	uv sync --frozen
	uv run ruff check src tests scripts experiments
	uv run mypy src
	uv run pytest tests/ -v
	uv run python scripts/gen_protocol_doc.py --check
	uv run python experiments/run_experiments.py

# 兼容旧名，等价于 verify
verify-s0: verify
