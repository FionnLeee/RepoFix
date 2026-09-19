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

## M1 Checkpoint

Worker 在任务开始、工具结果完整返回和执行结束的安全边界保存 checkpoint：工作区文本文件（含新增和删除）、当前与完整会话、压缩记录、模型／工具调用计数、已用时间和已知费用。记录绑定任务、执行代次、原始源码摘要、沙箱镜像 ID 和 Agent／模型配置摘要，写入后经 Worker 认证、租约和连续序号检查登记到控制端。页面显示保存事件，`GET /runs/<run-id>/checkpoints` 返回登记记录。

```bash
# 将最新已登记 checkpoint 还原到一次性新沙箱，核对内容后销毁，不调用模型
docker compose exec -T worker python -m repopilot.checkpoint <run-id> --generation 1
# 也可用 --checkpoint-id <id> 校验指定历史快照
```

运行工件位于 `runtime/artifacts/<run-id>/checkpoints/g<generation>/`。Python 的 `TracedAgent.restore_checkpoint(reference)` 可在同一有效执行代次、相同配置和镜像的新沙箱中，从最新安全边界继续；需先用该 checkpoint 的 files 初始化沙箱，再以原始 run/source 调用 `enable_checkpoints`。它保留已消耗预算，不重复已完成工具动作。恢复等待时间不计入已消耗执行时间；费用价格未知时仍不能据此宣称有真实美元预算控制。

终态或过期快照、校验失败、配置／代次不匹配、有后台进程的工作区，以及模型／工具调用结果未确认的状态均不允许作为续跑点。checkpoint 仅覆盖上述文本工作区，不保存进程、环境变量或 `/tmp`。CLI 只校验还原；失联任务的自动重排队、跨代次恢复与审批恢复见下方 M3 章节，页面仍不提供“从此检查点恢复”按钮（恢复由协调器与审批决定触发）。恢复功能通过确定性故障测试验证，不代表真实模型效果评测。

## M2 上下文管理：索引、预算、规则与记忆

上下文策略选择 `managed` 时，Worker 在每次模型调用前重新组装请求，而不是直接发送累计历史。请求由四部分构成：系统规则与原始任务（逐字保留）、适用的项目规则文件（根目录及被涉及目录的 `AGENTS.md`，逐字保留，超出 16 KB 拒绝启动）、当前状态与证据（工作区摘要、已修改路径、有效记忆、检索到的代码片段、压缩后的结构化摘要），以及最近的会话历史。完整原始轨迹和每次调用的实际请求（`runtime/artifacts/<run-id>/context/call-N.json`）都独立保存。

启动前需下载固定版本的本地 embedding 模型，不使用付费 API：

```bash
uv run python scripts/prepare_embeddings.py
docker compose up -d --build --scale worker=2
```

`qdrant/bge-small-en-v1.5-onnx-q`（revision `52398278…`）通过 manifest 校验后只读挂载给 Worker；Qdrant 随默认 Compose 启动，仅监听本机端口。

- **代码索引与版本**：源码按 AST 函数／类边界和行数切片，向量写入 Qdrant，payload 绑定项目、commit、索引 ID、embedding 版本和文件哈希。控制端记录每个索引头的待完成与已发布构建；只有 Worker 声明的全部点都已写入且清单一致时才发布，不完整、过期或代次不匹配的构建返回 409。检索只查询已发布索引，命中还必须与当前工作区的片段哈希一致才会被采纳，被修改或删除的文件片段立即失效；执行期间的修改进入本任务独立的覆盖层索引。Qdrant 或 embedding 不可用时退回当前文件的字面检索并记录 `INDEX_FALLBACK`，所有权错误则中止任务。
- **预算与压缩**：按 UTF-8 字节作为 token 上界估算（`CONTEXT_WINDOW_TOKENS` 默认 16,384，另预留输出 1,600、协议 512、安全 1,024）。超过输入预算 65% 或在页面点击“请求下一步骤压缩”时，生成确定性的结构化摘要（目标、约束、最近决策、已改文件、最近一次测试摘录、未解决项、证据引用），只保留最近的完整动作组，不调用模型生成摘要。仍然超限时依次丢弃证据片段、更早历史和记忆；任务、规则和最近一组动作不可丢弃，超限则报错而不是静默截断。工具输出完整归档，模型可用 `repopilot_read_log <id> <offset>` 回读。事件 `CONTEXT_ASSEMBLED` 记录估算值与来源，`CONTEXT_USAGE` 记录 provider 实际输入 token 的差值。
- **项目规则**：`AGENTS.md` 作用域按目录嵌套，子目录规则只在其范围内覆盖父级；提示中明确规则低于平台策略和用户任务。
- **显式记忆**：仅通过页面或 `POST /projects/<id>/memories` 显式保存，必须引用来源任务和事件；带乐观版本号，可编辑、停用、启用、删除、导出与导入 Markdown。记忆绑定保存时的 commit，commit 变化后标记“待复核”，需用新任务重新确认；创建任务时可关闭记忆读取。Worker 每次调用都从控制端重新读取有效记忆，控制端不可用时不使用缓存；提示中声明记忆和检索结果是证据而非权限。
- **Checkpoint**：managed 模式的压缩状态与上下文配置纳入 checkpoint 绑定，恢复后沿用同一摘要状态。

managed 工具输出绑定执行后的工作区哈希；后续文件变化时，旧版本输出在请求和最近测试摘要中替换为失效提示，要求重读或重新运行测试。原始内容保留在轨迹中，checkpoint 保留版本绑定。旧版轨迹中没有哈希的输出无法追溯判断。本轮修复后 Linux 测试共 29 项通过，最新上下文专项 8 项通过；部署冒烟确认修改后每次请求排除了 2 条过期输出。

2026-09-16／17 功能验证：Linux Worker 镜像中 `pytest` 28 项通过（含发布索引隔离、修改／删除覆盖层、规则作用域与预算、长历史压缩与记忆撤销、控制端不可用不用缓存、日志回读、managed checkpoint 恢复）；`scripts/context_check.py` 在真实 Qdrant 与本地 embedding 上验证跨项目隔离、记忆停用／删除／版本冲突、commit 变化待复核、手动压缩持久化、不完整或过期构建不可发布；`scripts/context_smoke.py` 完成一次确定性 managed 任务并只读恢复其 checkpoint。这些是功能与故障检查，不是效果评测。managed 模式尚未做真实模型配对实验，不能宣称它提高成功率或节省 token；字节估算偏保守，实际 token 通常更少。

## M3 可靠调度与审批

任务执行不再依赖单条未确认消息：Worker 认领后先向控制端取恢复计划，再决定是全新执行、从检查点恢复，还是等待审批。演示任务与真实任务使用同一套协议。

- **失联自动重排队**：Worker 心跳续租，租约过期时控制端检查该任务已登记的最新检查点，存在可恢复点且未超过 3 次恢复上限就重新入队，否则停在中断。被顶掉的旧 Worker 收到 409 后只停止本次尝试，不写任何状态。
- **跨代次恢复**：重新认领会得到新的执行代次，新代次用检查点里的工作区重建沙箱，校验任务、来源、镜像、Agent 配置与工作区摘要后从同一安全边界继续，已消耗的模型调用、工具调用与计时一并继承。被中断步骤在沙箱内的副作用随容器丢弃，原尝试轨迹另存为 `runtime/artifacts/<run-id>/trajectory-before-recovery-g<N>.json`。
- **版本绑定审批**：动作审批策略为 `auto`（默认，写入允许范围之外的位置时需批准）或 `strict`（所有文件写入都需批准）。命中时 Worker 在安全边界保存检查点并登记审批，任务进入“等待审批”；批准只对该动作与当时的工作区版本有效，恢复时再次核对动作哈希与工作区版本，拒绝则把决定作为观察交回模型。页面提供批准／拒绝按钮。
- **共享模型并发配额**：Redis 令牌槽限制所有 Worker 合计的并发模型调用数（`MODEL_MAX_CONCURRENCY`，默认 2），槽位带 TTL，Worker 崩溃不会永久占用；Redis 不可用时记录 `QUOTA_FALLBACK` 并继续，不伪装成受限。
- **孤儿沙箱回收**：Worker 周期扫描带 `repopilot.managed=sandbox` 标签的容器，只清理不属于活跃运行且超过 90 秒宽限的容器；控制端不可达时不做任何删除。
- **broker 断线重连**：控制端发布 Outbox 失败时重建连接，未发布的行保留到下个周期重发。

确定性验证（无生成模型调用）：`scripts/approval_smoke.py` 在 `strict` 策略下暂停、批准、跨代次恢复并完成验收；`scripts/recovery_check.py` 注入租约过期，验证重排队、从登记检查点恢复、旧尝试不留状态与无主沙箱被回收；`scripts/quota_check.py` 在真实 Redis 上验证峰值并发等于上限、等待者串行与槽位回收。Linux Worker 镜像 48 项 pytest、协议检查 16 项、ruff、构建与页面检查通过。这些是功能与故障检查，不是效果评测。

```bash
# 严格审批路径：暂停、批准、跨代次恢复、独立验收（确定性任务）
uv run python scripts/approval_smoke.py
# 故障注入：租约过期后自动重排队并从检查点恢复；同时检查孤儿沙箱回收
uv run python scripts/recovery_check.py
# 真实 Redis 上的共享模型并发配额
docker compose exec -T worker sh -c 'python scripts/quota_check.py'
```

## M4 独立评审与工作台

交付前的评审是一个独立角色，不是第二个执行者：它拿到的只有任务、允许修改的路径、候选 diff、变更后的源码，以及评审前重新跑一遍的开发测试输出；不继承执行者的对话历史，没有工具，也不能写任何文件。

- **结构化意见**：输出为 `file / line / severity / finding / trigger / evidence / suggestion`，每条意见必须指向候选版本里真实存在的文件与行，指不到的意见被丢弃并计数；整段解析不出来记为该次评审失败，而不是“没有问题”。
- **有限修订**：每个候选版本评审一次，出现阻断项时把意见交回执行者修订，默认上限两轮（每次运行可配 0–5）；候选版本一变，上一份评审立即标为过期。
- **判定权仍在验收**：修订额度用尽仍有阻断项时记录为未解决，独立验收照常判定；评审本身失败也继续，不会因为评审把运行卡死。评审调用计入本次运行的模型用量（与执行者共用预算）。
- **工作台**：候选补丁页签用只读的 Monaco 侧栏对比固定基准与候选版本，评审意见可以跳到它命名的行；评审历史按轮次显示，并标出哪一份已被修订取代。
- **可观测**：控制端为每条投递生成 W3C `traceparent` 放进队列消息，Worker 续接成一次运行的 trace（attempt 之下挂着模型调用、工具执行、评审与两次验收），导出到 Compose 内的 Jaeger。

```bash
# 评审闭环：评审发生在验收前、一轮有限修订、旧评审过期、复评干净（确定性任务）
uv run python scripts/review_smoke.py
# 一次运行的 trace：控制端 → 消息 → Worker 的传播与步骤 span
uv run python scripts/trace_check.py
# 故障矩阵：把已有故障注入按顺序跑一遍，输出注入/期望/观测（约 6 分钟）
python scripts/fault_matrix.py
```

SWE-bench 的接线（实例 → 任务、运行 → 官方预测文件、官方报告 → 本地记录、联网/磁盘预检）在 `scripts/swebench.py`。2026-09-18 已用本机缓存的实例镜像、真实模型补丁和官方 harness 完成两个单实例评测：`astropy__astropy-12907` 与 `sympy__sympy-20590` 均为 `resolved=1`（2/2）。前者补丁与 gold 的代码 hunk 一致；后者同样加入 `__slots__ = ()`，gold 另有说明性注释。结果、预测和官方报告只保存在本地 `runtime/validation/`。**口径限制（写进任何引用这些数字的地方）**：任务翻译把 gold 补丁触及的文件列表当作 `allowedPaths`，等于给执行者一个"改这个文件"的提示，因此这些数字**与公开榜单不可比**；样本还经过"本机镜像与 gold 可评测性"筛选，不能外推为 SWE-bench 总体成绩或 reviewer 的效果。`preflight` 仍会如实报告其他实例的镜像、磁盘或依赖缺口。评审质量本身没有做效果对照：演示任务用确定性桩驱动闭环，真实评审走模型路径。

## 上下文对照与边界

`full` 保留完整会话历史。`compact` 在历史超过 3,500 字符且有足够旧消息时，用不超过约 1,200 字符的历史摘录替换较早消息，保留系统规则、原始任务及最近四条消息。完整原始轨迹独立保存，token 汇总使用完整记录，页面显示压缩事件。

这是有损的抽取式压缩 v1，不是语义摘要或长期记忆；字符阈值不是精确 token 预算。小任务可能不触发，或压缩后反而需要更多调用，必须结合实测成功率和 token 判断。自建任务数量小，单次配对不能证明统计显著提升。

2026-09-16 首轮实测：三个任务的两模式预设运行共 6/6 通过；沿用 `deepseek-v4-flash-0731` 的真实单次配对，full 为 1/3、compact 为 2/3。失败包括两次连续模型输出格式错误和一次漏修折扣范围校验。只有一次真实运行触发压缩（5,001 → 3,985 字符）。两次格式失败的用量原先被失败上报遗漏，现已从完整轨迹恢复：full 输入／输出合计 7,163／1,770 token，compact 为 9,070／4,900 token，六次运行均有完整记录。本组样本没有显示 token 节省，成功率差异也不能归因于压缩。另一个自定义仓库请求成功修改两个文件，真实候选补丁已通过无模型重放验收。

成功与失败运行均汇总轨迹中的用量，包括格式错误回复。`usage_status` 区分完整、部分与不可用；部分记录只是已知 token 的小计，不代表整次运行消耗。基线报告同时列出完整覆盖的运行数。格式重试提示包含正确的命令块示例，连续三次格式错误仍会停止。

当前只面向单用户本机运行。用户登录、S3 工件存储与大规模 SWE-bench 评测尚未完成；独立 Reviewer 与 OTel trace 已实现，但评审质量没有做效果对照（演示用确定性桩驱动闭环），trace 也只在配置了 OTLP 端点时开启。任务失联会按已登记检查点自动重排队（最多 3 次），超过上限或没有可用检查点时停在中断；审批策略是确定性写命令解析，不是完备的能力模型，沙箱、允许路径校验与独立验收仍是实际边界。Redis 用于共享模型并发配额，仅在 Compose 内部可达。没有用户体系，审批接口与任务接口一样只对本机开放。固定测试验收不保证任意对抗代码无法干扰测试进程。SWE-bench 已在两个经筛选的本机缓存实例上通过官方 harness 得到 2/2 resolved，但尚未进行可代表总体的固定子集或全量评测；`scripts/swebench.py preflight` 会如实报出其他实例所需的包、缓存镜像与磁盘余量。live 运行使用配置里的模型凭据（额度按模型计：带日期的快照 id 可能先耗尽，换成同模型的滚动别名即可继续）。

## 停止

```bash
docker compose down
```

默认保留数据库与工件，不使用 `down -v` 删除数据。应用只监听本机端口，尚未实现公开服务所需的用户认证。
