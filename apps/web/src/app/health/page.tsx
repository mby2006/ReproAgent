import HealthPanel from "@/components/HealthPanel";
import Shell from "@/components/Shell";

export default function HealthPage() {
  return (
    <Shell>
      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
        <div className="rise mb-6">
          <h1 className="display text-2xl font-bold text-fg">服务健康状态</h1>
          <p className="mt-1 text-sm text-fg-3">
            查看各项服务的连接状态。点击刷新可重新检测。
          </p>
        </div>
        <div className="rise rise-1">
          <HealthPanel />
        </div>
      </main>
    </Shell>
  );
}
