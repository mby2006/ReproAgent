import Link from "next/link";

export default function MissingTarget() {
  return (
    <div className="p-6 font-mono text-sm text-fg-3">
      未指定目标编号 · <Link className="text-action" href="/papers">回到论文库</Link>
    </div>
  );
}
