# 第一轮交付边界

已完成本地部署与第一轮验证，证据与结果见 `VALIDATION.md`。

已实现代码：

- Next.js 工作台：创建固定样例任务、列表、SSE、事件、patch 下载、独立验收和取消。
- NestJS/Fastify + Prisma/PostgreSQL：类型化输入、幂等创建、Outbox、条件认领、Worker 令牌、运行代次、租约、事件去重。
- RabbitMQ + aio-pika：跨语言任务消息和两个可扩展 Worker。
- 复用 mini-swe-agent 的实际执行循环；确定性演示与真实模型分开标记。
- Docker 沙箱无网络、非 root、只读根目录、CPU/内存/PID 限制；Agent 无模型凭据或 Docker socket。
- 仅交付 pricing.py 候选源码，在新的容器运行固定独立测试。

当前限制：

- 仅提供固定 pricing 示例，不支持上传任意仓库或自动提 PR。
- 取消在工具边界生效，正在执行的命令最长受 25 秒工具期限限制；模型请求取消需等待其期限。
- 首版租约过期标记 INTERRUPTED，不自动重放或恢复执行；队列消息在任务期间保持未确认并由 prefetch=1 限制每个 Worker。
- 无效消息直接拒绝；认领接口故障延后再入队，避免暂时断连直接丢失尚未认领的任务。尚未实现完整死信/人工重投控制台。
- Outbox 发布器为单 API 进程，broker 断连后需要重启 API。不是高可用集群。
- 工件暂存本地持久目录；S3/MinIO、Qdrant 检索、共享 Redis 配额、上下文压缩、项目记忆、审批、Reviewer、OTel 和 SWE-bench 尚未实现。
- Qdrant/Redis 仅提供 extended Compose profile，启动容器不等于完成业务集成。
- 首版 UI diff 使用文本展示，Monaco 后续接入。
- UI/API 只绑定 127.0.0.1，属于单用户本地演示；用户登录、项目级访问控制尚未实现，不适合公开部署。
- Worker 是持有 Docker 管理权限的可信进程；模型命令仅在另建沙箱中执行。当前未将沙箱管理器独立拆成服务。
- 真实模型费用未配置价格表，不能把未知费用写成零；步数 12、单次输出 1600 token、单次调用 45 秒、重试最多两次。总时间限制在步骤边界检查，不是硬实时预算。
- 正常流程会回收容器；Worker 被强杀时可能留下容器，未实现自动孤儿清理，部署排障需检查标签 `repopilot.managed=sandbox`。

这些限制属于第一轮里程碑，不改变已确认的最终设计。
