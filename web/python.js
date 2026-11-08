// The page's side of py-worker.js: call(cmd, ...args) → Promise of the
// parsed JSON reply. `ready` settles once Python has loaded, or rejects if
// it couldn't (after which every call rejects too).

export class Python {
  constructor(onStatus) {
    this.worker = new Worker(new URL("./py-worker.js", import.meta.url), { type: "module" });
    this.pending = new Map();
    this.nextId = 1;
    this.status = "Starting";
    this.failure = null;
    this.worker.onmessage = ({ data }) => {
      if (data.status) {
        this.status = data.status;
        if (data.status.startsWith("failed")) this.fail(new Error(data.status.slice(8)));
        onStatus?.(data.status);
        return;
      }
      const p = this.pending.get(data.id);
      if (!p) return;
      this.pending.delete(data.id);
      if (data.error) p.reject(new Error(data.error));
      else p.resolve(data.result);
    };
    this.worker.onerror = (e) => {
      this.status = `failed: ${e.message || "Python couldn't start"}`;
      this.fail(new Error(this.status.slice(8)));
      onStatus?.(this.status);
    };
    this.ready = this.call("init");
  }

  fail(err) {
    this.failure ??= err;
    for (const p of this.pending.values()) p.reject(err);
    this.pending.clear();
  }

  call(cmd, ...args) {
    if (this.failure) return Promise.reject(this.failure);
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, cmd, args });
    });
  }
}
