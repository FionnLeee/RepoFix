# 首轮工作台设计

用户：希望检查代码任务是否正确完成的开发者。页面主要任务：启动受控样例，追踪执行，检查 patch 与独立验收。

颜色：纸面灰 #F4F6FA、白 #FFFFFF、墨蓝 #24314A、钴蓝 #315ED8、边界灰 #E2E7EF、结果绿 #337758。

字体：标题使用 Segoe UI Variable Display / Microsoft YaHei，正文 Segoe UI / Microsoft YaHei，运行 ID 与代码使用 Cascadia Code / Consolas。使用系统字体避免字体下载依赖。

布局：左侧项目与任务导航，中间执行轨迹，右侧本次运行事实。独特元素是贯穿实际事件的编号轨迹；编号等于事件顺序，帮助定位执行证据。候选 diff 与测试报告处于同一任务内的独立标签页。

克制使用状态色。空状态引导启动样例，连接失败显示实际错误；未实现功能不提供假按钮。小屏折叠导航与侧栏，保留键盘焦点、按钮状态和减少动画偏好。

Button 使用 Radix Slot、class-variance-authority 和 Tailwind 的 shadcn 组件模式，首版 diff 为文本渲染；Monaco 将在大文件交互阶段接入。
