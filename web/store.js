// Everything this site keeps lives in the visitor's own browser:
//
//   IndexedDB "shares-tracker"
//     kv      trades {csv, name, savedAt}, settings, targets.demo, targets.mine
//     prices  saved closes per symbol (see prices.js)
//   localStorage
//     sharesTracker.*.demo / sharesTracker.*.mine  the dashboard's own saved
//                                                  settings (goal planner…)
//     holdings-map-view                            the holdings map's view
//
// clearAll removes exactly these. GitHub Pages serves every project of one
// account from the same origin, so it can't wipe the whole origin instead.
//
// If the browser refuses storage (some private modes), everything is kept in
// memory for this tab only and `persistent` is false.

const DB_NAME = "shares-tracker";
const DB_VERSION = 1;
const LOCAL_PREFIX = "sharesTracker.";
const LOCAL_KEYS = ["holdings-map-view"];

let dbPromise = null;
let memory = null;  // Map per store, when IndexedDB isn't available
// After clearAll, reads answer "nothing" without opening (and so recreating)
// the database, until something is saved again.
let cleared = false;
export let persistent = true;

function openDb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        req.result.createObjectStore("kv");
        req.result.createObjectStore("prices", { keyPath: "symbol" });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error("storage blocked by another tab"));
    }).catch((e) => {
      console.warn("IndexedDB unavailable, keeping data in memory:", e);
      persistent = false;
      memory = { kv: new Map(), prices: new Map() };
      return null;
    });
  }
  return dbPromise;
}

function request(db, storeName, mode, fn) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const req = fn(tx.objectStore(storeName));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("storage transaction aborted"));
  });
}

async function get(storeName, key) {
  if (cleared) return null;
  const db = await openDb();
  const value = db ? await request(db, storeName, "readonly", (s) => s.get(key)) : memory[storeName].get(key);
  return value ?? null;
}

// `key` is undefined for "prices", whose entries carry their own (symbol).
async function put(storeName, value, key) {
  cleared = false;
  const db = await openDb();
  if (!db) memory[storeName].set(key ?? value.symbol, value);
  else await request(db, storeName, "readwrite", (s) => s.put(value, key));
}

async function remove(storeName, key) {
  const db = await openDb();
  if (!db) memory[storeName].delete(key);
  else await request(db, storeName, "readwrite", (s) => s.delete(key));
}

export const kv = {
  get: (key) => get("kv", key),
  put: (key, value) => put("kv", value, key),
  delete: (key) => remove("kv", key),
};
export const prices = {
  get: (symbol) => get("prices", symbol),
  put: (entry) => put("prices", entry),
};

function localStore() {
  try { return window.localStorage; } catch { return null; }
}

/** The dashboard's localStorage entries for one dashboard ("mine"/"demo"). */
export function localEntries(storageKey) {
  const ls = localStore();
  const out = {};
  if (!ls) return out;
  for (let i = 0; i < ls.length; i++) {
    const k = ls.key(i);
    if (k.startsWith(LOCAL_PREFIX) && k.endsWith(`.${storageKey}`)) out[k] = ls.getItem(k);
  }
  return out;
}

/** Restore entries from an export; only the dashboard's own keys are taken. */
export function restoreLocalEntries(entries, storageKey) {
  const ls = localStore();
  if (!ls || !entries) return;
  for (const [k, v] of Object.entries(entries)) {
    if (k.startsWith(LOCAL_PREFIX) && k.endsWith(`.${storageKey}`) && typeof v === "string") {
      try { ls.setItem(k, v); } catch { /* full or blocked */ }
    }
  }
}

/** Remove everything this site stored. */
export async function clearAll() {
  const ls = localStore();
  if (ls) {
    const doomed = [];
    for (let i = 0; i < ls.length; i++) {
      const k = ls.key(i);
      if (k.startsWith(LOCAL_PREFIX) || LOCAL_KEYS.includes(k)) doomed.push(k);
    }
    doomed.forEach((k) => ls.removeItem(k));
  }
  const db = await openDb();
  dbPromise = null;
  cleared = true;
  if (memory) { memory = null; return; }
  db?.close();
  await new Promise((resolve, reject) => {
    const req = indexedDB.deleteDatabase(DB_NAME);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
    req.onblocked = () => resolve();  // finishes once other tabs close it
  });
}
