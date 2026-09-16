# 项目 2 最终设计方案

用户于 2026-09-16 确认。项目名称：RepoPilot。目标：AI Agent 开发实习面试主项目。

最终功能设计为同日《RepoPilot_Coding_Agent完整规划_v3.md》；技术架构以《RepoPilot_技术架构升级_v4.md》覆盖。两份原文已归档在本目录 `design/` 中。

确定的架构：Next.js / TypeScript 工作台，NestJS / Fastify / Prisma 控制端，Python asyncio / Pydantic / aio-pika Agent Worker，PostgreSQL、Qdrant、RabbitMQ、Redis、S3 兼容工件存储、Docker 与 OpenTelemetry。

确定的功能主线：自主工具循环、分层上下文与自动压缩、项目规则与记忆、沙箱与版本绑定审批、用户纠偏与执行恢复、独立 Reviewer、独立补丁验收、SWE-bench 官方 harness 接入。

开源底座：[SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，以 Git submodule 固定提交。保留上游原貌，在外部实现适配与扩展。

第一轮范围为 M0/M1 部分：可运行全栈、两个 Worker、确定性样例及真实模型试跑、隔离执行和独立验收。不要求一次完成 7–10 周的全部设计。

后续技术选择若改变已确认的服务职责，需要在本文件记录原因与影响，不自动退回第一项目技术栈。
