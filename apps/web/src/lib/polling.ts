type ReadLocks = {
  request: (
    key: string,
    options: { ifAvailable: true },
    callback: (lock: object | null) => Promise<unknown>,
  ) => Promise<unknown>;
};

type PollingEnvironment = {
  document: Document;
  scheduler: {
    setInterval: (callback: () => void, interval: number) => number;
    clearInterval: (timer: number) => void;
  };
};

const localReads = new Map<string, Promise<unknown>>();

export async function readOnce<T>(
  key: string,
  request: () => Promise<T>,
  locks?: ReadLocks | null,
): Promise<T | null> {
  const manager = locks === undefined
    ? (typeof navigator === "undefined" ? undefined : navigator.locks as unknown as ReadLocks)
    : locks;
  const existing = localReads.get(key);
  if (existing) return await existing as T;
  const pending = manager
    ? manager.request(key, { ifAvailable: true }, async (lock) => (
      lock ? request() : null
    )) as Promise<T | null>
    : request();
  localReads.set(key, pending);
  try {
    return await pending;
  } finally {
    if (localReads.get(key) === pending) localReads.delete(key);
  }
}

export function startVisiblePolling(
  task: () => void | Promise<void>,
  interval: number,
  environment?: PollingEnvironment,
): () => void {
  const document = environment?.document ?? globalThis.document;
  const setInterval = environment?.scheduler.setInterval ?? globalThis.setInterval;
  const clearInterval = environment?.scheduler.clearInterval ?? globalThis.clearInterval;
  if (!document) return () => {};

  let stopped = false;
  let inFlight = false;
  let timer: number | undefined;

  const clear = () => {
    if (timer !== undefined) clearInterval(timer);
    timer = undefined;
  };
  const tick = () => {
    if (stopped || document.hidden || inFlight) return;
    inFlight = true;
    void Promise.resolve().then(task).catch(() => {}).finally(() => {
      inFlight = false;
    });
  };
  const start = () => {
    clear();
    if (stopped || document.hidden) return;
    tick();
    timer = setInterval(tick, interval);
  };
  const onVisibilityChange = () => start();

  document.addEventListener("visibilitychange", onVisibilityChange);
  start();
  return () => {
    stopped = true;
    clear();
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
