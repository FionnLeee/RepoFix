# 上游与个人贡献边界

- 来源：https://github.com/SWE-agent/mini-swe-agent
- 上游版本：2.4.6
- 固定提交：04d809ceab9df28f9adaed044884180159172930
- 许可证：MIT，完整文件保留在 vendor/mini-swe-agent/LICENSE.md。
- 引入方式：Git submodule，克隆本项目时使用 `git submodule update --init --recursive`。

首轮直接复用 DefaultAgent 的循环、步数/时间边界、轨迹格式、LitellmTextbasedModel 的模型接入、DeterministicModel 的预设动作测试能力。RepoPilot 不把这些内容称为个人原创。

个人新增：Next.js 工作台、NestJS/Fastify/Prisma 任务与事件、Outbox 分发、跨语言认领与步骤提交协议、Worker 心跳和取消、Docker SDK 沙箱适配器、日志限长、干净环境验收与补丁交付。上游代码未直接修改。

确定性演示固定了修复动作，仅证明系统链路；真实模型运行才用于观察自主选择工具的行为。单个自建样例不属于 SWE-bench 成绩。
