import HealthPanel from "@/components/HealthPanel";
import Shell from "@/components/Shell";

export default function HealthPage() {
  return (
    <Shell>
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
        <div className="rise mb-6">
          <p className="label mb-1 text-brand">Diagnostics</p>
          <h1 className="display text-2xl font-bold text-fg">服务健康状态</h1>
          <p className="mt-1 text-sm text-fg-3">
            五个服务的实时探活与信任域边界。数据来自 API 的 /health 与 /health/dependencies。
          </p>
        </div>
        <div className="rise rise-1">
          <HealthPanel />
        </div>
      </main>
    </Shell>
  );
}