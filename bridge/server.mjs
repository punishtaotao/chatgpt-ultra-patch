/**
 * Codex <-> WorkBuddy dual-account bridge (CN + International)
 * ------------------------------------------------------------------
 * One HTTP port exposes models & credits from two WorkBuddy accounts:
 *   - CN (www.workbuddy.cn):    model ids as-is, e.g. hy4flash-free
 *   - International (www.workbuddy.ai): model ids prefixed "ai-", e.g. ai-gpt-6.2-sol
 * Requests are routed by model-id prefix; the prefix is stripped upstream.
 * Point one Codex provider at this port and both account model lists merge
 * into the desktop model picker.
 *
 * Endpoints:
 *   GET  /healthz             health check (sign-in state of both accounts)
 *   GET  /v1/models            merged model list (OpenAI format)
 *   POST /v1/responses        Codex main channel (Responses SSE)
 *   POST /v1/chat/completions compatibility channel (chat SSE)
 *
 * Security: binds 127.0.0.1 only; upstream tokens stay in memory.
 * Port:     env WB_BRIDGE_PORT, default 15790.
 *
 * Dependencies:
 *   - node >= 18
 *   - the `dsh-workbuddy-connect` plugin module (path via env WB_CONNECT_MODULE).
 *     NOTE: the plugin minifies its exports, so the alias names below
 *     (h.m / h.S / h.x / h.O / h.I / h.P) correspond to one specific plugin
 *     build.  If your build differs, resolve them from
 *     `Object.keys(module)` / source inspection and adjust the aliases.
 *
 * Run: node server.mjs   (or start_bridge.pyw for a windowless start on Windows)
 */
import { createServer } from "node:http";
import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { buildChatBody, convertChatSSEToResponses } from "./responses.mjs";

// --- configuration via environment -------------------------------------------
const WB_MODULE = process.env.WB_CONNECT_MODULE || "";      // required
const CN_ELECTRON = process.env.WORKBUDDY_ELECTRON_BIN || "";      // optional
const AI_ELECTRON = process.env.WORKBUDDY_AI_ELECTRON_BIN || "";   // optional
const PORT = Number(process.env.WB_BRIDGE_PORT || 15790);
const HOST = "127.0.0.1";
const BRIDGE_TOKEN = process.env.WB_BRIDGE_TOKEN || ""; // empty = no auth (loopback only)
const AI_PREFIX = "ai-"; // international model id prefix, used for routing

if (!WB_MODULE) {
  console.error("env WB_CONNECT_MODULE is required: absolute file:// path to the dsh-workbuddy-connect module");
  process.exit(64);
}
if (CN_ELECTRON) process.env.WORKBUDDY_ELECTRON_BIN = CN_ELECTRON;
if (AI_ELECTRON) process.env.WORKBUDDY_AI_ELECTRON_BIN = AI_ELECTRON;

const h = await import(WB_MODULE);
// one credential store per account; the HTTP client is shared (region is
// derived from the credential domain automatically)
const stores = {
  cn: new h.m({ variant: h.S }),   // h.S = CN_VARIANT
  ai: new h.m({ variant: h.x }),   // h.x = AI_VARIANT (international)
};

// ------------------------------------------------------------------
// International gateway channel check (11128 "unofficial client"): the
// official desktop app sends a set of session/trace headers that the plugin
// client omits.  Inject them globally, but only for international chat
// requests (credentials/region handling stays with the plugin).
// ------------------------------------------------------------------
const origFetch = globalThis.fetch;
const CHAT_PATH = "/v2/chat/completions";
globalThis.fetch = async (url, opts) => {
  try {
    const u = typeof url === "string" ? url : (url?.url || "");
    if (u.includes(CHAT_PATH) && u.includes("workbuddy.ai") && opts?.headers) {
      const m = new Headers(opts.headers);
      m.set("X-Private-Data", "true");
      m.set("X-Conversation-ID", randomUUID());
      m.set("X-Conversation-Request-ID", randomUUID());
      m.set("X-Conversation-Message-ID", randomUUID());
      m.set("X-Request-ID", randomUUID());
      m.set("X-Agent-Intent", "craft");
      m.set("X-Agent-Purpose", "conversation");
      m.set("x-codebuddy-request", "1");
      const t = randomBytes(16).toString("hex"), s = randomBytes(8).toString("hex"), p = randomBytes(8).toString("hex");
      m.set("traceparent", `00-${t}-${s}-01`);
      m.set("b3", `${t}-${s}-1-${p}`);
      m.set("X-B3-TraceId", t); m.set("X-B3-ParentSpanId", p); m.set("X-B3-SpanId", s); m.set("X-B3-Sampled", "1");
      m.set("X-Trace-ID", t);
      opts = { ...opts, headers: m };
    }
  } catch (e) { log("header inject warn:", String(e?.message || e).slice(0, 120)); }
  return origFetch(url, opts);
};

const client = new h.O();

const log = (...a) => process.stdout.write(`[wb-bridge ${new Date().toISOString()}] ${a.join(" ")}\n`);
const regionOfModel = (model) => String(model || "").startsWith(AI_PREFIX) ? "ai" : "cn";
const wireModel = (model) => String(model || "").startsWith(AI_PREFIX) ? String(model).slice(AI_PREFIX.length) : model;

// ------------------------------------------------------------------
// credit display: cache per account 60s, mapped onto Codex rate-limit headers
// ------------------------------------------------------------------
const _creditsCache = { cn: { at: 0, value: null }, ai: { at: 0, value: null } };
async function getCredits(region) {
  const cache = _creditsCache[region];
  const now = Date.now();
  if (cache.value && now - cache.at < 60_000) return cache.value;
  try {
    const cred = await stores[region].resolve();
    const info = await client.fetchCredits(cred, new AbortController().signal);
    cache.at = now; cache.value = info;
  } catch (e) {
    log(`credits fetch failed (${region}):`, String(e?.message || e).slice(0, 120));
  }
  return cache.value;
}
function rateLimitHeaders(region) {
  const c = _creditsCache[region]?.value;
  const out = {};
  if (!c || typeof c.total !== "number") return out;
  out["x-codex-credits-has-credits"] = c.total > 0 ? "true" : "false";
  out["x-codex-credits-unlimited"] = "false";
  out["x-codex-credits-balance"] = String(c.total);
  return out;
}

const REASON_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"].map((reasoningEffort) => ({ reasoningEffort }));
function json(res, status, body, region = "cn") {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(payload), ...rateLimitHeaders(region) });
  res.end(payload);
}
function err(res, status, code, message, region = "cn") {
  json(res, status, { error: { message, type: code, code } }, region);
}
function authOk(req) {
  if (!BRIDGE_TOKEN) return true;
  const m = /^Bearer\s+(.+)$/i.exec((req.headers.authorization || "").trim());
  if (!m) return false;
  const a = Buffer.from(m[1]), b = Buffer.from(BRIDGE_TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0;
    req.on("data", (c) => { size += c.length; if (size > 64 * 1024 * 1024) { reject(new Error("body too large")); req.destroy(); } chunks.push(c); });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

// model list cache: 5 min per account
const _modelsCache = { cn: { at: 0, value: null }, ai: { at: 0, value: null } };
async function modelsOf(region) {
  const cache = _modelsCache[region];
  if (cache.value && Date.now() - cache.at < 300_000) return cache.value;
  const cred = await stores[region].resolve();
  const models = await client.fetchModels(cred, new AbortController().signal);
  cache.at = Date.now(); cache.value = models;
  return models;
}

const server = createServer(async (req, res) => {
  let region = "cn";
  try {
    const url = (req.url || "/").split("?")[0];
    if (req.method === "GET" && url === "/healthz") {
      const states = {};
      for (const key of ["cn", "ai"]) {
        try { states[key] = (await stores[key].status()).state; } catch (e) { states[key] = "error:" + String(e?.message || e).slice(0, 80); }
      }
      return json(res, 200, { ok: true, states }, "cn");
    }
    if (!authOk(req)) return err(res, 401, "unauthorized", "missing or invalid bearer");

    if (req.method === "GET" && (url === "/v1/models" || url === "/v1/models/")) {
      const data = [];
      const failures = {};
      for (const key of ["cn", "ai"]) {
        try {
          const models = await modelsOf(key);
          for (const m of models) {
            const id = key === "ai" ? AI_PREFIX + m.id : m.id;
            data.push({
              id, object: "model", created: 0,
              owned_by: key === "ai" ? "workbuddy-ai" : "workbuddy-cn",
              context_window: m.contextWindow ?? m.context ?? undefined,
              billing: m.billing?.credits ?? undefined,
              // make the desktop model picker render the Ultra (purple) effort level
              supportedReasoningEfforts: REASON_EFFORTS,
              defaultReasoningEffort: "high",
            });
          }
        } catch (e) {
          failures[key] = String(e?.message || e).slice(0, 200);
          log(`models fetch failed (${key}):`, failures[key]);
        }
      }
      if (!data.length) return err(res, 502, "upstream_error", JSON.stringify(failures));
      return json(res, 200, { object: "list", data, failures }, "cn");
    }

    if (req.method === "POST" && (url === "/v1/responses" || url === "/v1/responses/")) {
      let raw;
      try { raw = await readBody(req); } catch (e) { return err(res, 400, "bad_request", String(e)); }
      let body;
      try { body = JSON.parse(raw); } catch { return err(res, 400, "bad_request", "invalid JSON"); }
      region = regionOfModel(body.model);
      const upstreamModel = wireModel(body.model);

      let cred;
      try { cred = await stores[region].resolve(); }
      catch (e) { return err(res, 401, "not_signed_in", `${region}: ${String(e?.message || e).slice(0, 300)}`, region); }
      await getCredits(region); // failure does not affect the main flow

      // Responses request -> WorkBuddy chat request (strip international prefix)
      const chatBody = buildChatBody({ ...body, model: upstreamModel });
      let prepared;
      try {
        const src = JSON.stringify(chatBody);
        // international uses the plugin's region-specific body prep; CN uses the generic one
        prepared = region === "ai" ? h.I(src) : h.P(src);
      } catch (e) { return err(res, 400, "bad_request", "prepare body failed: " + String(e?.message || e).slice(0, 200), region); }

      const controller = new AbortController();
      req.on("close", () => controller.abort());
      let result;
      try { result = await client.chatStream(cred, prepared, controller.signal); }
      catch (e) { return err(res, 502, "upstream_error", `${region}: ${String(e?.message || e).slice(0, 300)}`, region); }
      if (!result.ok) {
        const status = ({ hard_credit: 402, soft_rate: 429, session_dead: 401, not_found: 502, server: 502, client: 400 })[result.kind] || 502;
        return err(res, status, result.kind, `workbuddy(${region}) upstream ${result.kind}: ${String(result.message).slice(0, 300)}`, region);
      }

      const upstreamText = await result.response.text();
      // echo back the prefixed model id so Codex does not think the model was swapped
      const conv = convertChatSSEToResponses(upstreamText, body.model);

      if (body.stream === false) {
        const completedEvt = conv.events.split("\n").find((l) => l.includes('"type": "response.completed"'));
        let responseObj = null;
        try { responseObj = JSON.parse(completedEvt.replace(/^data: /, "").trim()).response; } catch {}
        return json(res, 200, responseObj || { id: "resp_err", object: "response", status: "completed", output: [], usage: null }, region);
      }

      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache", Connection: "keep-alive", "X-Accel-Buffering": "no",
        ...rateLimitHeaders(region),
      });
      res.write(conv.events);
      res.end();
      return;
    }

    if (req.method === "POST" && (url === "/v1/chat/completions" || url === "/v1/chat/completions/")) {
      let raw;
      try { raw = await readBody(req); } catch (e) { return err(res, 400, "bad_request", String(e)); }
      let body;
      try { body = JSON.parse(raw); } catch { return err(res, 400, "bad_request", "invalid JSON"); }
      region = regionOfModel(body.model);
      const upstreamModel = wireModel(body.model);

      let cred;
      try { cred = await stores[region].resolve(); }
      catch (e) { return err(res, 401, "not_signed_in", `${region}: ${String(e?.message || e).slice(0, 300)}`, region); }

      let prepared;
      try {
        const src = JSON.stringify({ ...body, model: upstreamModel });
        prepared = region === "ai" ? h.I(src) : h.P(src);
      } catch (e) { return err(res, 400, "bad_request", "prepare body failed: " + String(e?.message || e).slice(0, 200), region); }

      const controller = new AbortController();
      req.on("close", () => controller.abort());
      let result;
      try { result = await client.chatStream(cred, prepared, controller.signal); }
      catch (e) { return err(res, 502, "upstream_error", `${region}: ${String(e?.message || e).slice(0, 300)}`, region); }
      if (!result.ok) {
        const status = ({ hard_credit: 402, soft_rate: 429, session_dead: 401, not_found: 502, server: 502, client: 400 })[result.kind] || 502;
        return err(res, status, result.kind, `workbuddy(${region}) upstream ${result.kind}: ${String(result.message).slice(0, 300)}`, region);
      }
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache", Connection: "keep-alive", "X-Accel-Buffering": "no",
        ...rateLimitHeaders(region),
      });
      const { Readable } = await import("node:stream");
      const stream = Readable.fromWeb(result.response.body);
      stream.on("error", () => { if (res.writable) res.end("data: [DONE]\n\n"); });
      stream.pipe(res);
      return;
    }

    return err(res, 404, "not_found", `no such route: ${req.method} ${url}`);
  } catch (e) {
    log("FATAL", String(e?.stack || e).slice(0, 500));
    if (!res.headersSent) err(res, 500, "internal", String(e?.message || e).slice(0, 300), region);
    else res.end();
  }
});

server.listen(PORT, HOST, () => {
  log(`listening http://${HOST}:${PORT}/v1  (CN + WorkBuddy AI dual accounts)`);
  for (const key of ["cn", "ai"]) {
    stores[key].status().then((s) => log(`${key} credential state:`, s.state)).catch(() => log(`${key} credential state: unknown`));
  }
});

function shutdown() {
  try { server.close(); } catch {}
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
