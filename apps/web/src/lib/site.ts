// 参赛静态站不带后端：构建期隐藏工作台入口。
// 后端上线后用 NEXT_PUBLIC_SITE_MODE=live 重新构建即可恢复。
export const isStaticSite = process.env.NEXT_PUBLIC_SITE_MODE === "static";
