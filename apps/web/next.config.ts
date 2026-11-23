import type { NextConfig } from "next";

// Qoder Sites 参赛版走纯静态产物；本地 Docker 工作台仍保留服务端渲染。
const staticExport = process.env.SITE_STATIC_EXPORT === "1";

const nextConfig: NextConfig = {
  // 导出模式不接受 PPR，因此这两个服务端渲染开关只在非导出时启用。
  ...(staticExport
    ? { output: "export" as const }
    : { cacheComponents: true, partialPrefetching: true }),
  /* config options here */
  // Hide the Next.js dev tools badge; it is development-only chrome that
  // overlaps the primary action button in screenshots and demos.
  devIndicators: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
