# RepoPilot

面向代码任务的 Agent 工作台，基于固定版本的 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 扩展。

技术栈：Next.js / TypeScript 工作台、NestJS / Fastify 控制端、Prisma / PostgreSQL、RabbitMQ 和 Python Worker。当前为固定样例的本地可运行版本。

复用 mini-swe-agent 2.4.6（MIT）的 Agent 循环、模型接入与轨迹格式，以 submodule 固定提交 `04d809ceab9df28f9adaed044884180159172930`，保留上游许可证。RepoPilot 在外部新增任务管理、跨语言 Worker 协议、Docker 沙箱适配、独立验收和网页工作台。

## 代码归档与回档

私有仓库：[FionnLeee/RepoPilot](https://github.com/FionnLeee/RepoPilot)，默认分支 `main`。克隆时使用 `git clone --recurse-submodules https://github.com/FionnLeee/RepoPilot.git` 获取固定版本的上游依赖。

每轮改进通过适用验证后，提交相关文件并执行 `git push origin main`。通过 `git log --oneline` 查找历史版本；需要撤销某次修改时，执行 `git revert <commit>`，验证后推送，保留完整历史。

Git 归档包含源码、配置模板和本 README。学习笔记、设计方案、复盘和验证记录仅保存在本地，不提交到 GitHub。本机 `.env`、数据库数据及 `runtime/` 工件不在归档中，恢复部署时需另行配置。

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
