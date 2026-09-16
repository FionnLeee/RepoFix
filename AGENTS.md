# RepoPilot project rules

- 项目 2 最终设计：docs/DESIGN.md。以面试为导向，每项贡献必须有运行证据。
- vendor/mini-swe-agent 为固定 Git submodule，不直接修改上游代码。
- TypeScript 控制端拥有业务状态；Python Worker 使用内部认证接口提交状态。
- 保留模型与确定性演示的区别，演示结果不得宣称真实模型效果。
- .env、凭据、容器运行工件不得提交。模型凭据不得进入执行沙箱。
- 新增检查应验证实际行为或故障边界。提交前运行构建、Python 检查和部署冒烟。
- 未完成能力写入 docs/STATUS.md，不在 UI 显示为已实现。

## 版本归档

- GitHub 私有仓库：https://github.com/FionnLeee/RepoPilot，默认分支 main，远程 origin。
- 后续完成一轮代码修改并通过适用验证后，提交本轮相关文件并推送到 origin；交付时报告 commit 和验证结果。用户明确要求暂不提交时遵循当次要求。
- 提交前检查 diff 和暂存文件，排除凭据与运行工件；不把无关改动混入提交。
- 回档优先使用 git revert 生成可追踪的撤销提交；不擅自强推、重写远程历史或删除数据库。
