# RepoPilot

面向代码任务的 Agent 工作台，基于固定版本的 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 扩展。

最终方案见 [docs/DESIGN.md](docs/DESIGN.md)，实际进度与边界见 [docs/STATUS.md](docs/STATUS.md)，复用范围见 [docs/UPSTREAM.md](docs/UPSTREAM.md)。

## 本地启动

需要 Git、Docker Desktop/Linux Docker、Node.js 22 与 uv。

```bash
git submodule update --init --recursive
uv sync --frozen
uv run python scripts/configure.py
docker pull python:3.12-slim
docker compose up -d --build --scale worker=2
```

页面：http://localhost:3100 。API：http://localhost:3101/health 。配置文件 `.env` 不提交。

授权复用 TicketPilot 的模型时：

```bash
uv run python scripts/configure.py --ticketpilot-env /absolute/path/to/ticketpilot/.env
docker compose up -d --force-recreate api worker
```

该脚本仅映射已知 openai-compatible 配置，保留独立的数据库密码和 Worker 令牌。不会打印凭据。

## 验证

```bash
npx pnpm@10.17.1 install --frozen-lockfile
npx pnpm@10.17.1 build
uv run ruff check services scripts
uv run pytest
uv run python scripts/smoke.py
```

默认冒烟仅使用确定性模型，另用 `--live` 启动一次有界真实模型任务。结果保存在 `runtime/validation/`，可从页面查看每次运行。

## 第一轮做什么

固定 Python 示例 → 事务创建任务 → RabbitMQ 分发 → Python Worker 认领 → 上游 Agent loop → 独立 Docker 沙箱 → 干净环境验证 → patch、测试和轨迹回到页面。

第一轮仅修复示例的 pricing.py，不支持任意仓库。演示的预设修复用于验证链路，不能作为模型修复成功率。正式 SWE-bench 接入属于后续里程碑。

## 停止

```bash
docker compose down
```

默认保留数据库与工件，不使用 `down -v` 删除数据。应用只监听本机端口，尚未实现公开服务所需的用户认证。
