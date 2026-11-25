// 参赛静态站不带后端：构建期隐藏工作台入口。
// 后端上线后用 NEXT_PUBLIC_SITE_MODE=live 重新构建即可恢复。
export const isStaticSite = process.env.NEXT_PUBLIC_SITE_MODE === "static";

export function siteHref(href: string): string {
  if (!isStaticSite || !href.startsWith("/") || href.startsWith("//")) return href;
  const suffixIndex = href.search(/[?#]/);
  const path = suffixIndex === -1 ? href : href.slice(0, suffixIndex);
  if (path === "/" || /\.[a-z0-9]+$/i.test(path)) return href;
  return `${path}.html${suffixIndex === -1 ? "" : href.slice(suffixIndex)}`;
}

// 演示服务器上没有 SMTP（重置邮件发不出去），也只有一份训练并发额度，
// 所以自助注册与忘记密码在演示构建里整体关掉，账号由服务端管理接口预建。
export const selfServiceAuth = process.env.NEXT_PUBLIC_SELF_SERVICE_AUTH !== "0";

// 演示账号写进产物：公开站点上的入门凭据，不承担保密职责。
export const demoLogin =
  process.env.NEXT_PUBLIC_DEMO_EMAIL && process.env.NEXT_PUBLIC_DEMO_PASSWORD
    ? { email: process.env.NEXT_PUBLIC_DEMO_EMAIL, password: process.env.NEXT_PUBLIC_DEMO_PASSWORD }
    : null;
