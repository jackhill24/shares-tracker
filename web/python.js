// The page's side of py-worker.js: call(cmd, ...args) → Promise of the
// parsed JSON reply. `ready` settles once Python has loaded, or rejects if
// it couldn't (after which every call rejects too).

// A phone can stop the worker (usually for running out of memory) without
// any error reaching the page, so a call that hasn't answered by its limit
// is taken as stalled. Each is many times what a computer needs (requirements
// well under 1 s, build about 5 s). Loading has its own hint instead.
const TIME_LIMITS = { demo: 60000, requirements: 60000, parse_targets: 60000, build: 240000 };

export class StalledError extends Error {}

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
      if (data.crash) return this.fail(new Error(data.crash));
      const p = this.pending.get(data.id);
      if (!p) return;
      this.pending.delete(data.id);
      clearTimeout(p.timer);
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
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  call(cmd, ...args) {
    if (this.failure) return Promise.reject(this.failure);
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const limit = TIME_LIMITS[cmd];
      // A stalled worker can't be trusted with another call: stop it, and
      // fail this and every later call.
      const timer = limit && setTimeout(() => {
        this.worker.terminate();
        this.fail(new StalledError(`Python didn't answer within ${limit / 1000} s (${cmd})`));
      }, limit);
      this.pending.set(id, { resolve, reject, timer });
      this.worker.postMessage({ id, cmd, args });
    });
  }
}
