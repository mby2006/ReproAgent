import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ReproAgent · Evidence Lab",
  description:
    "科研论文复现 Agent：从论文到可追溯的复现报告。证据落地、隔离执行、逐节点 Trace。",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full bg-ink-950 font-sans text-fg">{children}</body>
    </html>
  );
}