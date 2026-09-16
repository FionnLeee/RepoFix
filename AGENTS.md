# RepoPilot project rules

- 项目 2 最终设计：docs/DESIGN.md。以面试为导向，每项贡献必须有运行证据。
- vendor/mini-swe-agent 为固定 Git submodule，不直接修改上游代码。
- TypeScript 控制端拥有业务状态；Python Worker 使用内部认证接口提交状态。
- 保留模型与确定性演示的区别，演示结果不得宣称真实模型效果。
- .env、凭据、容器运行工件不得提交。模型凭据不得进入执行沙箱。
- 新增检查应验证实际行为或故障边界。提交前运行构建、Python 检查和部署冒烟。
- 未完成能力写入 docs/STATUS.md，不在 UI 显示为已实现。
