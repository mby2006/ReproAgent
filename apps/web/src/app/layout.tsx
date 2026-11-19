import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ReproAgent",
  description: "科研论文复现 Agent：从论文到可追溯的复现报告（第一阶段：LeNet-5 + MNIST）",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-zinc-950 text-zinc-100">
        {children}
      </body>
    </html>
  );
}