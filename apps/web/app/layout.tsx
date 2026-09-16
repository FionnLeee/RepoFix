import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "RepoPilot · 代码任务工作台",
  description: "检查执行轨迹、补丁与独立验收结果。",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
