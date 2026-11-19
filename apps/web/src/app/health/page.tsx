import HealthPanel from "@/components/HealthPanel";

export default function HealthPage() {
  return (
    <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-12">
      <h1 className="mb-6 text-2xl font-bold">服务健康状态</h1>
      <HealthPanel />
    </main>
  );
}