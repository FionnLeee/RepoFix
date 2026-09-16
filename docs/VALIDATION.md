# 第一轮部署与验证记录

日期：2026-09-16。本机 Windows + Docker Desktop，独立 Compose 项目 `repopilot`。TicketPilot 使用原有端口，未替换或停止其服务。

## 打开结果

- 工作台：http://localhost:3100
- 真实模型成功任务：http://localhost:3100/?run=1f4a64f7-4750-4418-bff8-50dd495678cb
- API 健康：http://localhost:3101/health

源码根目录即本文上一级。设计确认记录见 `DESIGN.md`，部署步骤见根目录 README。

## 实际验证

| 检查 | 结果与证据 |
| --- | --- |
| 上游来源 | GitHub mini-swe-agent 2.4.6，commit 04d809ceab9df28f9adaed044884180159172930，MIT，submodule 固定 |
| NestJS/Prisma 构建 | Prisma Client 生成、TypeScript 编译通过；初始 migration 在独立 PostgreSQL 执行成功 |
| Next.js 构建 | 生产构建与 TypeScript 检查通过 |
| Python | Ruff 通过；3 个 pytest 测试通过，含真实 Docker 隔离、非 root、只读根目录、无凭据/socket、候选 symlink 拒绝与清理 |
| 确定性全链路 | 任务 8b55afd4-c9a8-4257-bf85-2b30c7ba7ec4 成功，见 evidence/demo-smoke.json |
| 真实模型全链路 | 任务 1f4a64f7-4750-4418-bff8-50dd495678cb 成功，见 evidence/live-smoke.json |
| 并发幂等 | 6 个同时创建请求得到同一 run，只有一次有效认领；两种模式均验证 |
| 输入与认证 | 未授权 Worker 认领返回 401，额外未知字段返回 400 |
| 协议故障 | 排队取消、重复认领、旧代次、步骤去重、SSE 续传、45 秒租约失联标记通过，见 evidence/protocol.json |
| 两个 Worker | 两条并发演示分别由 861ef917e1fa 与 99e3ae1f0117 执行，均成功，见 evidence/parallel.json |
| 浏览器交互 | 标题/加载正常，桌面页面无水平溢出，控制台无 warn/error；轨迹、diff、验收标签正常切换 |

浏览器检查按用户路由偏好由 gpt-5.6-luna/xhigh 执行，未改变全局模型设置。浏览器只启动一次确定性演示，真实模型任务由独立脚本启动。

## 真实模型单次运行

- 沿用 TicketPilot 配置，模型：deepseek-v4-flash-0731，经 OpenAI-compatible 接口调用。
- 模型自行选择读代码、测试、编辑和提交动作，未使用演示 FIXED 源码作为模型输入。
- 6 次模型调用；provider 返回 input 2,721 / output 695 token。
- 冒烟脚本总耗时 22.52 秒，包含并发幂等检查、查询轮询与验收；不是纯模型延迟或性能分位数。
- 缺陷前源码无法通过固定测试；候选源码在干净容器通过 6 项独立验收。
- 补丁修复百分比折扣与范围校验，详见结果文件。费用未配置价格表，记为未知。

这是一个自建样例的冒烟，不是 SWE-bench 成绩、完整成功率或生产 SLA。

## 本轮遇到并修复的问题

1. Docker archive 写接口在只读 rootfs 下拒绝初始化，即使目标是 tmpfs。改为容器内非 root 写入声明范围的初始文件。
2. Docker archive 读接口无法取得该 tmpfs 中的候选文件。改为容器内隔离 Python 读取固定路径，使用 O_NOFOLLOW、regular-file 和大小检查，然后以 base64 回传。
3. 新增真实沙箱测试发现测试工具对 DockerClient 的上下文管理假设错误，改用显式 close，复测通过。

数据库保留了前两次演示失败，便于对照完整事件；未删除失败任务美化结果。第一次归档初始化失败的临时容器已按精确 ID 清理。

## 尚未交付

完整范围见 STATUS.md。当前为首轮 M0/M1 部分：压缩、记忆、审批、自动恢复、Reviewer、Qdrant 业务索引、S3 工件存储、OTel 和 SWE-bench 后续接入。已部署容器、固定示例成功和最终设计完成是三个不同状态。
