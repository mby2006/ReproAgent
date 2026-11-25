import SiteLink from "@/components/SiteLink";
import { IconPapers, LogoMark } from "./icons";
import { isStaticSite } from "@/lib/site";

export default function PublicHeader({ example = false }: { example?: boolean }) {
  return <header className="public-header">
    <a className="skip-link" href="#content">跳到正文</a>
    <div className="public-bar">
      <SiteLink href="/" className="wordmark" aria-label="ReproAgent 首页"><LogoMark />ReproAgent</SiteLink>
      <nav aria-label="主导航" className="public-nav">
        <SiteLink href="/" aria-current={example ? undefined : "page"}>首页</SiteLink>
        <SiteLink href="/examples/lenet-mnist" aria-current={example ? "page" : undefined}>公开实验</SiteLink>
      </nav>
      {!isStaticSite && <SiteLink className="btn btn-ghost public-workspace" href="/papers"><IconPapers />工作台</SiteLink>}
    </div>
  </header>;
}
