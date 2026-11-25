import type { NextConfig } from "next";

const siteMode = process.env.NEXT_PUBLIC_SITE_MODE ?? "live";
if (siteMode !== "live" && siteMode !== "static") {
  throw new Error("NEXT_PUBLIC_SITE_MODE must be either live or static");
}

const nextConfig: NextConfig = {
  ...(siteMode === "static"
    ? { output: "export" as const }
    : { cacheComponents: true, partialPrefetching: true }),
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
