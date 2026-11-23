import Link from "next/link";
import { IconPapers, LogoMark } from "./icons";
import { isStaticSite } from "@/lib/site";

export default function PublicHeader({ example = false }: { example?: boolean }) {
  return <header className="public-header">
    <a className="skip-link" href="#content">跳到正文</a>
    <div className="public-bar">
      <Link href="/" className="wordmark" aria-label="ReproAgent 首页"><LogoMark />ReproAgent</Link>
      <nav aria-label="主导航" className="public-nav">
        <Link href="/" aria-current={example ? undefined : "page"}>首页</Link>
        <Link href="/examples/lenet-mnist" aria-current={example ? "page" : undefined}>公开实验</Link>
      </nav>
      {!isStaticSite && <Link className="btn btn-ghost public-workspace" href="/papers"><IconPapers />工作台</Link>}
    </div>
  </header>;
}
