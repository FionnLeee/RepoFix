# RepoPilot

面向代码任务的 Agent 工作台，基于固定版本的 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 扩展。

技术栈：Next.js / TypeScript 工作台、NestJS / Fastify 控制端、Prisma / PostgreSQL、RabbitMQ 和 Python Worker。支持固定 commit 的小型 Python 仓库、多文件补丁与独立验收。

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
uv run python scripts/prepare_baselines.py
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

## 仓库任务与基线

网页选择基线任务，或填写公开 GitHub URL、完整 40 位 commit、子目录、问题说明、允许修改的文件和独立 unittest 验收代码。自定义仓库使用真实模型；内置基线支持预设动作与真实模型两种模式。

执行过程：下载／读取固定源码快照 → RabbitMQ 分发 → Worker 在 Docker 沙箱运行上游 Agent → 检查修改范围 → 生成多文件 Git patch → 在原始副本上重新应用 → 使用同一镜像分别验收原始版本与候选版本。成功要求原始版本有断言失败、无测试加载错误，候选版本通过同一组非跳过测试且补丁非空。

当前接收 UTF-8 普通文本，单文件不超过 100 KB、最多 200 文件、总量不超过 4 MB；不支持二进制、符号链接、submodule 或文件权限变更。执行环境为断网 Python 标准库环境，暂不支持任意依赖安装。开发命令与验收代码都只在沙箱中执行。

本机私有仓库可先注册精确版本，不上传源代码到新 GitHub 仓库：

```bash
uv run python scripts/register_repository.py --repo /absolute/path/to/repo --id my-project --commit <40-character-commit>
```

注册器只读取指定 commit 的已跟踪文件，不执行仓库代码，不包含工作区未提交改动。页面仓库来源填 `registered:my-project`，commit 填注册时版本。注册产物位于本地 `runtime/repositories/`，只读挂载给 Worker；应仅注册适合交给模型处理的源码仓库。

`prepare_baselines.py` 将三个自建任务构建为可重复生成的 Git 快照：订单优惠与运费、分页边界与切片、配置解析与默认值。每个任务需修复两个模块，含开发测试和五项独立验收。定义与预设修复位于 `benchmarks/tasks.json`，运行时仅将缺陷源码和开发测试放入模型沙箱。它们用于工程回归和小样本对照，不是 SWE-bench 数据集或泛化成绩。

```bash
# 预设修复验证全部基线与两种上下文模式，不调用模型
uv run python scripts/evaluate_baselines.py
# 真实模型配对实验：每任务每模式各运行一次，失败保留在分母
uv run python scripts/evaluate_baselines.py --live
# 可选重复实验；会增加模型调用消耗
uv run python scripts/evaluate_baselines.py --live --repeats 3
# 不调用模型，使用归档源码、测试、镜像 ID 和 patch 重新验收
docker compose exec -T worker python -m repopilot.replay <run-id>
# 从完整轨迹恢复旧报告漏记的用量，另存报告，不调用模型或修改原报告
uv run python scripts/evaluate_baselines.py --recover-report runtime/validation/<report>.json
```

运行工件保存源码快照、patch、测试摘要、源码／测试／补丁哈希、Git commit、容器 image ID、模型参数和完整轨迹。摘要在页面显示；完整报告位于本地 `runtime/validation/` 与 `runtime/artifacts/`，不提交。复现验收需保留这些工件和对应镜像；模型输出本身不保证逐次相同。

## 上下文对照与边界

`full` 保留完整会话历史。`compact` 在历史超过 3,500 字符且有足够旧消息时，用不超过约 1,200 字符的历史摘录替换较早消息，保留系统规则、原始任务及最近四条消息。完整原始轨迹独立保存，token 汇总使用完整记录，页面显示压缩事件。

这是有损的抽取式压缩 v1，不是语义摘要或长期记忆；字符阈值不是精确 token 预算。小任务可能不触发，或压缩后反而需要更多调用，必须结合实测成功率和 token 判断。自建任务数量小，单次配对不能证明统计显著提升。

2026-09-16 首轮实测：三个任务的两模式预设运行共 6/6 通过；沿用 `deepseek-v4-flash-0731` 的真实单次配对，full 为 1/3、compact 为 2/3。失败包括两次连续模型输出格式错误和一次漏修折扣范围校验。只有一次真实运行触发压缩（5,001 → 3,985 字符）。两次格式失败的用量原先被失败上报遗漏，现已从完整轨迹恢复：full 输入／输出合计 7,163／1,770 token，compact 为 9,070／4,900 token，六次运行均有完整记录。本组样本没有显示 token 节省，成功率差异也不能归因于压缩。另一个自定义仓库请求成功修改两个文件，真实候选补丁已通过无模型重放验收。

成功与失败运行均汇总轨迹中的用量，包括格式错误回复。`usage_status` 区分完整、部分与不可用；部分记录只是已知 token 的小计，不代表整次运行消耗。基线报告同时列出完整覆盖的运行数。格式重试提示包含正确的命令块示例，连续三次格式错误仍会停止。

当前只面向单用户本机运行。用户登录、权限审批、项目记忆、独立 Reviewer、执行恢复、S3 工件存储、OTel 和正式 SWE-bench 尚未完成；Qdrant/Redis 仅有扩展启动配置。任务失联标记中断，不自动恢复；API 的 broker 重连与孤儿沙箱清理仍待完善。固定测试验收不保证任意对抗代码无法干扰测试进程。

## 停止

```bash
docker compose down
```

默认保留数据库与工件，不使用 `down -v` 删除数据。应用只监听本机端口，尚未实现公开服务所需的用户认证。
