// Venera source runtime — Node side of the bridge.
//
// Venera sources are plain JS classes extending `ComicSource`; the JS library talks to its host
// through a global `sendMessage({method, ...})`.
//
// The library calls `sendMessage` **synchronously** for DOM, encoding, cookies and storage
// (`querySelectorAll(...).map(...)`, `el.text`, `Convert.md5(...)`), so those are implemented
// here in JS. Only network access is asynchronous and goes to Python (which owns the proxy
// configuration and the cookie jar), so the two processes never need synchronous IPC.
//
// Binary values travel as {"__b64": "..."}.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import readline from "node:readline";
import { parseHTML } from "linkedom";

const STATE_DIR = process.argv[2] || ".";
const STORE = path.join(STATE_DIR, "storage.json");

let store = {};
try {
  store = JSON.parse(fs.readFileSync(STORE, "utf8"));
} catch {
  store = {};
}
function saveStore() {
  try {
    fs.writeFileSync(STORE, JSON.stringify(store));
  } catch {}
}

// ---------------------------------------------------------------- DOM registry
const docs = new Map(); // key -> document
const els = new Map(); // id -> element
let elSeq = 1;

function reg(el) {
  if (!el) return null;
  const id = elSeq++;
  els.set(id, el);
  return id;
}

function html(p) {
  const fn = p.function;
  const key = p.key | 0;
  if (fn === "parse") {
    const { document } = parseHTML(p.data || "<html></html>");
    docs.set(key, document);
    return null;
  }
  if (fn === "dispose") {
    docs.delete(key);
    return null;
  }
  if (fn === "querySelector") return reg((docs.get(key) || {}).querySelector?.(p.query));
  if (fn === "querySelectorAll") {
    const d = docs.get(key);
    return d ? [...d.querySelectorAll(p.query)].map(reg) : [];
  }
  if (fn === "getElementById") return reg((docs.get(key) || {}).getElementById?.(p.id));
  if (fn === "dom_querySelector") {
    const el = els.get(key);
    return reg(el ? el.querySelector(p.query) : null);
  }
  if (fn === "dom_querySelectorAll") {
    const el = els.get(key);
    return el ? [...el.querySelectorAll(p.query)].map(reg) : [];
  }

  const el = els.get(key);
  if (!el) return null;
  switch (fn) {
    case "getText": return el.textContent;
    case "getAttributes": {
      const o = {};
      for (const a of el.attributes || []) o[a.name] = a.value;
      return o;
    }
    case "getChildren": return [...el.children].map(reg);
    case "getClassNames": return [...(el.classList || [])];
    case "getId": return el.id || null;
    case "getInnerHTML": return el.innerHTML;
    case "getLocalName": return el.localName;
    case "getParent": return reg(el.parentElement);
    case "getPreviousSibling": return reg(el.previousElementSibling);
    case "getNextSibling": return reg(el.nextElementSibling);
    case "getNodes": return [...el.childNodes].map(reg);
    case "node_text": return el.textContent;
    case "node_type": return el.nodeType === 3 ? "text" : "element";
    case "node_toElement": return el.nodeType === 1 ? key : reg(el.parentElement);
    default: return null;
  }
}

// ---------------------------------------------------------------- convert
function toBuf(v) {
  if (v && typeof v === "object" && typeof v.__b64 === "string") {
    return Buffer.from(v.__b64, "base64");
  }
  if (v instanceof ArrayBuffer) return Buffer.from(new Uint8Array(v));
  if (ArrayBuffer.isView(v)) return Buffer.from(v.buffer, v.byteOffset, v.byteLength);
  if (typeof v === "string") return Buffer.from(v, "utf8");
  return Buffer.alloc(0);
}
const b64 = (b) => ({ __b64: Buffer.from(b).toString("base64") });

function convert(p) {
  const t = p.type;
  const enc = p.isEncode !== false;
  const raw = toBuf(p.value);
  try {
    if (t === "utf8") return enc ? b64(raw) : raw.toString("utf8");
    if (t === "gbk") {
      if (!enc) return new TextDecoder("gbk").decode(raw);
      return b64(raw); // gbk encoding is rare; utf8 fallback keeps callers from crashing
    }
    if (t === "base64") {
      if (enc) return raw.toString("base64");
      const s = typeof p.value === "string" ? p.value : raw.toString("utf8");
      return b64(Buffer.from(s, "base64"));
    }
    if (t === "md5" || t === "sha1" || t === "sha256" || t === "sha512") {
      const h = crypto.createHash(t).update(raw).digest();
      return enc ? b64(h) : h.toString("hex");
    }
    if (t === "hmac") {
      const h = crypto.createHmac(p.hash || "sha256", toBuf(p.key)).update(raw).digest();
      return p.isString ? h.toString("hex") : b64(h);
    }
    if (t && t.startsWith("aes")) return aes(t, p, raw, enc);
    return null;
  } catch (e) {
    return null;
  }
}

function aes(mode, p, raw, enc) {
  const key = toBuf(p.key);
  const iv = p.iv ? toBuf(p.iv) : null;
  const algo = mode === "aes-ecb" ? "aes-128-ecb" : mode === "aes-cbc" ? "aes-128-cbc"
    : mode === "aes-cfb" ? "aes-128-cfb" : "aes-128-ofb";
  const keyBuf = key.length === 16 || key.length === 24 || key.length === 32 ? key : key.subarray(0, 16);
  const name = `aes-${keyBuf.length * 8}-${algo.split("-")[2]}`;
  const cipher = enc
    ? crypto.createCipheriv(name, keyBuf, iv)
    : crypto.createDecipheriv(name, keyBuf, iv);
  if (enc) cipher.setAutoPadding(true);
  const outBuf = Buffer.concat([cipher.update(raw), cipher.final()]);
  return b64(outBuf);
}

// ---------------------------------------------------------------- cookies
const jar = new Map(); // domain -> Map(name -> value)

function domainOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function cookie(p) {
  const host = domainOf(p.url || "");
  if (p.function === "set") {
    const m = jar.get(host) || new Map();
    for (const c of p.cookies || []) {
      if (c && c.name) m.set(c.name, c.value == null ? "" : String(c.value));
    }
    jar.set(host, m);
    return null;
  }
  if (p.function === "get") {
    const m = jar.get(host) || new Map();
    return [...m.entries()].map(([name, value]) => ({ name, value, domain: host, path: "/" }));
  }
  if (p.function === "delete") {
    jar.delete(host);
    return null;
  }
  return null;
}

function cookieHeader(url) {
  const host = domainOf(url);
  const parts = [];
  for (const [dom, m] of jar) {
    if (host === dom || host.endsWith("." + dom) || dom.endsWith(host)) {
      for (const [k, v] of m) parts.push(`${k}=${v}`);
    }
  }
  return parts.join("; ");
}

// ---------------------------------------------------------------- local dispatch
function local(p) {
  const m = p.method;
  switch (m) {
    case "html": return html(p);
    case "convert": return convert(p);
    case "cookie": return cookie(p);
    case "random":
      return p.type === "double"
        ? Math.random() * ((p.max ?? 1) - (p.min ?? 0)) + (p.min ?? 0)
        : Math.floor(Math.random() * ((p.max ?? 100) - (p.min ?? 0) + 1)) + (p.min ?? 0);
    case "uuid": return crypto.randomUUID();
    case "log": return null;
    case "getPlatform": return "windows";
    case "getLocale": return "zh_CN";
    case "isLogged": return true;
    case "compute": return null;
    case "getClipboard": return "";
    case "setClipboard": return null;
    case "UI": case "image": return null;
    case "load_data": {
      const k = `${p.key}:${p.data_key}`;
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
    }
    case "save_data": {
      store[`${p.key}:${p.data_key}`] = p.data;
      saveStore();
      return null;
    }
    case "delete_data": {
      delete store[`${p.key}:${p.data_key}`];
      saveStore();
      return null;
    }
    case "load_setting": {
      // sources declare defaults in `settings = {key: {..., default}}`
      const inst = sources.get(p.key);
      const spec = inst && inst.settings && inst.settings[p.setting_key];
      if (spec && spec.default !== undefined) return spec.default;
      return p.default ?? null;
    }
    default: return null;
  }
}

// ---------------------------------------------------------------- async network
let seq = 1;
const pending = new Map();

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function net(p) {
  const rid = seq++;
  const headers = { ...(p.headers || {}) };
  const extra = cookieHeader(p.url || "");
  if (extra) headers["Cookie"] = headers["Cookie"] ? headers["Cookie"] + "; " + extra : extra;
  return new Promise((resolve) => {
    pending.set(rid, resolve);
    out({ t: "rpc", rid, payload: { ...p, headers } });
  });
}

globalThis.sendMessage = function (payload) {
  const m = payload && payload.method;
  if (m === "http") return net(payload);
  if (m === "delay") return Promise.resolve(local(payload));
  return local(payload);
};

// ---------------------------------------------------------------- library / sources
function evalGlobal(code, filename) {
  const names = [...code.matchAll(/^(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)/gm)]
    .map((x) => x[1]);
  const tail = names.length ? ";" + names.map((n) => `globalThis[${JSON.stringify(n)}]=${n};`).join("") : "";
  // runInThisContext is unavailable without vm in ESM-only builds; Function is enough here
  // because we append explicit exports for every top-level name.
  new Function(code + tail)();
}

const sources = new Map();

function loadSource(sid, file) {
  if (!globalThis.ComicSource) throw new Error("library not loaded");
  const before = new Set(Object.keys(globalThis));
  evalGlobal(fs.readFileSync(file, "utf8"), file);
  const added = Object.keys(globalThis).filter((k) => !before.has(k));
  let cls = null;
  for (const k of added) {
    const v = globalThis[k];
    if (typeof v === "function" && v.prototype instanceof globalThis.ComicSource) {
      cls = v;
      break;
    }
  }
  if (!cls) throw new Error("no ComicSource subclass in " + file);
  const inst = new cls();
  sources.set(sid, inst);
  return { name: inst.name || sid, key: inst.key || sid, version: inst.version || "" };
}

function atPath(obj, p) {
  return p.split(".").reduce((a, k) => (a == null ? null : a[k]), obj);
}

async function handle(msg) {
  const fail = (e) => out({ t: "err", cid: msg.cid, error: String((e && e.stack) || e).slice(0, 900) });
  if (msg.cmd === "rpc_result") {
    const fn = pending.get(msg.rid);
    pending.delete(msg.rid);
    if (fn) fn(msg.value);
    return;
  }
  if (msg.cmd === "ping") {
    out({ t: "ok", cid: msg.cid, value: "pong" });
    return;
  }
  if (msg.cmd === "load_init") {
    try {
      evalGlobal(fs.readFileSync(msg.path, "utf8"), msg.path);
      out({ t: "ok", cid: msg.cid, value: !!globalThis.ComicSource });
    } catch (e) { fail(e); }
    return;
  }
  if (msg.cmd === "load_source") {
    try { out({ t: "ok", cid: msg.cid, value: loadSource(msg.sid, msg.path) }); }
    catch (e) { fail(e); }
    return;
  }
  if (msg.cmd === "call") {
    try {
      const inst = sources.get(msg.sid);
      if (!inst) throw new Error("source not loaded: " + msg.sid);
      const fn = atPath(inst, msg.path);
      if (typeof fn !== "function") throw new Error("not a function: " + msg.path);
      const res = await fn.apply(inst, msg.args || []);
      out({ t: "ok", cid: msg.cid, value: res === undefined ? null : res });
    } catch (e) { fail(e); }
    return;
  }
  if (msg.cmd === "eval") {
    try {
      const fn = new Function("return (async () => { " + msg.code + " })()");
      const value = await fn();
      out({ t: "ok", cid: msg.cid, value: value === undefined ? null : value });
    } catch (e) { fail(e); }
    return;
  }
  if (msg.cmd === "shutdown") {
    process.exit(0);
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  handle(msg);
});
out({ t: "ready", pid: process.pid });
