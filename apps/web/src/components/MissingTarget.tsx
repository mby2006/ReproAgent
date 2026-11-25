import SiteLink from "@/components/SiteLink";

export default function MissingTarget() {
  return (
    <div className="p-6 font-mono text-sm text-fg-3">
      未指定目标编号 · <SiteLink className="text-action" href="/papers">回到论文库</SiteLink>
    </div>
  );
}
