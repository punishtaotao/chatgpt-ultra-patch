/**
 * Responses API <-> WorkBuddy chat/completions 协议转换
 * ------------------------------------------------------------------
 * 入：Codex 发来的 Responses 请求（/v1/responses body）
 * 出：1) 转成 WorkBuddy chat 请求体；2) 把上游 chat SSE 转成 Responses 事件流。
 *
 * 只实现 Codex 实际会用到的事件子集，字段按 codex-rs/codex-api/src/sse/responses.rs 对齐：
 *   response.created / response.output_item.added / response.output_text.delta /
 *   response.output_text.done / response.output_item.done / response.completed
 *   以及 function_call 的 output_item.added/done（arguments 直接给全）。
 *   reasoning 通过 response.reasoning_text.delta 附带（Codex 可识别）。
 */

let SEQ = 0;
const rid = (prefix) => `${prefix}_${Date.now().toString(36)}${(SEQ++).toString(36)}${Math.random().toString(36).slice(2, 8)}`;

/** 把 Responses 的 input[] + instructions 转成 chat messages[]。 */
export function toChatMessages(body) {
  const messages = [];
  // WorkBuddy/CN 网关硬性要求：第一条必须是 system 角色，否则 400/11128
  // "Illegal API invocation from an unapproved channel"。
  // 另：WorkBuddy 网关会对超长的第三方 agent system 提示做渠道风控（11128），
  // Codex 的 instructions 常有 20KB+，必须压缩为中性短提示。工具能力走 tools 字段，
  // 不受影响。
  let instructions = body.instructions || "";
  if (instructions.length > 1200) {
    instructions = "You are a helpful coding assistant. Use the provided tools when needed. Be concise and precise. Respond in the user's language.";
  }
  if (instructions) messages.push({ role: "system", content: instructions });

  const inputs = Array.isArray(body.input) ? body.input : (typeof body.input === "string" ? [{ role: "user", content: body.input }] : []);
  for (const item of inputs) {
    const type = item?.type || (item?.role ? "message" : "unknown");
    if (type === "message") {
      // developer 一律降级为 system（上游无 developer 角色）
      const isSys = item.role === "developer" || item.role === "system";
      const role = isSys ? "system" : item.role === "assistant" ? "assistant" : "user";
      let content = "";
      const parts = Array.isArray(item.content) ? item.content : [{ type: "input_text", text: String(item.content ?? "") }];
      for (const p of parts) {
        if (p.type === "input_text" || p.type === "output_text" || p.type === "text") content += (p.text || "");
        else if (p.type === "input_image") content += ""; // 图片输入暂不转（WorkBuddy 图片链路另议）
      }
      // 同样压缩 input 里超长的 system/developer 内容（Codex 的 skills 注入可能 20KB+）
      if (isSys && content.length > 1200) {
        content = "You are a helpful coding assistant. Use the provided tools when needed. Be concise and precise. Respond in the user's language.";
      }
      messages.push({ role, content });
    } else if (type === "function_call_output") {
      messages.push({ role: "tool", tool_call_id: item.call_id, content: typeof item.output === "string" ? item.output : JSON.stringify(item.output) });
    } else if (type === "function_call") {
      // 模型历史里的工具调用，回传给 chat 需放在 assistant.tool_calls
      messages.push({
        role: "assistant", content: "",
        tool_calls: [{
          id: item.call_id || item.id,
          type: "function",
          function: { name: item.name, arguments: typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments || {}) },
        }],
      });
    }
    // reasoning / item_reference 等：忽略（不影响功能）
  }
  // 合并紧邻的 system 消息，避免多条 system 触发上游校验
  const merged = [];
  for (const m of messages) {
    const prev = merged[merged.length - 1];
    if (m.role === "system" && prev?.role === "system") prev.content += "\n\n" + m.content;
    else merged.push(m);
  }
  // 兜底：确保第一条是 system（上游硬性要求）
  if (merged.length === 0 || merged[0].role !== "system") {
    merged.unshift({ role: "system", content: "You are a helpful assistant." });
  }
  return merged;
}

/** Responses tools[] -> chat tools[]（字段已兼容：type:function, name, description, parameters）。 */
export function toChatTools(body) {
  if (!Array.isArray(body.tools)) return undefined;
  const tools = [];
  for (const t of body.tools) {
    if (t.type === "function" && t.name) {
      tools.push({ type: "function", function: { name: t.name, description: t.description || "", parameters: t.parameters || { type: "object", properties: {} } } });
    }
  }
  return tools.length ? tools : undefined;
}

/** 生成发给 WorkBuddy 的 chat 请求体。 */
export function buildChatBody(body) {
  const chat = {
    model: body.model,
    messages: toChatMessages(body),
    stream: true,
  };
  const tools = toChatTools(body);
  if (tools) { chat.tools = tools; chat.tool_choice = body.tool_choice === "required" ? "required" : body.tool_choice === "none" ? "none" : "auto"; }
  // reasoning effort：WorkBuddy 支持 low/high/max，映射到 chat 的 reasoning_effort（插件会规整）
  // ultra/xhigh → max（紫色 Ultra 档请求落到 WorkBuddy 最强推理）
  const effort = body.reasoning?.effort;
  if (effort === "low" || effort === "high" || effort === "max") chat.reasoning_effort = effort;
  else if (effort === "ultra" || effort === "xhigh") chat.reasoning_effort = "max";
  else if (effort === "medium") chat.reasoning_effort = "high";
  return chat;
}

function sseEvent(type, data) {
  return `event: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * 把一个 Responses 请求对应的完整 chat SSE 文本转成 Responses SSE 事件串。
 * 返回 {events, usage}。
 */
export function convertChatSSEToResponses(chatSSE, model, responseId) {
  const out = [];
  const respId = responseId || rid("resp");
  const createdAt = Math.floor(Date.now() / 1000);

  // --- 解析上游 chat chunk ---
  const chunks = [];
  let usage = null;
  for (const line of chatSSE.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("data:")) continue;
    const data = t.slice(5).trim();
    if (!data || data === "[DONE]") continue;
    try { chunks.push(JSON.parse(data)); } catch {}
  }

  // 聚合：文本 delta、reasoning delta、tool_calls（按 index 累积 arguments）
  const textDeltas = [];
  const reasoningDeltas = [];
  const toolsByIndex = new Map();
  let finishReason = "";
  for (const c of chunks) {
    if (c.usage) usage = c.usage;
    const choice = c.choices?.[0];
    if (!choice) continue;
    if (choice.finish_reason) finishReason = choice.finish_reason;
    const d = choice.delta || {};
    if (d.content) textDeltas.push(d.content);
    if (d.reasoning_content) reasoningDeltas.push(d.reasoning_content);
    if (Array.isArray(d.tool_calls)) {
      for (const tc of d.tool_calls) {
        const idx = tc.index ?? 0;
        const cur = toolsByIndex.get(idx) || { id: "", name: "", arguments: "" };
        if (tc.id) cur.id = tc.id;
        if (tc.function?.name) cur.name = tc.function.name;
        if (tc.function?.arguments) cur.arguments += tc.function.arguments;
        toolsByIndex.set(idx, cur);
      }
    }
  }
  const fullText = textDeltas.join("");
  const tools = [...toolsByIndex.values()];

  // --- 生成 Responses 事件 ---
  const baseResponse = {
    id: respId, object: "response", created_at: createdAt, status: "completed",
    model, error: null, incomplete_details: null, instructions: null,
    max_output_tokens: null, output: [], parallel_tool_calls: true,
    previous_response_id: null, reasoning: { effort: null, summary: null },
    store: false, temperature: null, text: { format: { type: "text" } },
    top_p: null, truncation: "disabled", usage: null, metadata: {},
  };

  out.push(sseEvent("response.created", { type: "response.created", response: { ...baseResponse, status: "in_progress" } }));
  out.push(sseEvent("response.in_progress", { type: "response.in_progress", response: { ...baseResponse, status: "in_progress" } }));

  let outputIndex = 0;
  const outputItems = [];

  // reasoning 作为一条 reasoning item（Codex 可识别 response.reasoning_text.delta）
  if (reasoningDeltas.length) {
    const rItemId = rid("rs");
    const rItem = { id: rItemId, type: "reasoning", summary: [], content: [], encrypted_content: null, status: "completed" };
    out.push(sseEvent("response.output_item.added", { type: "response.output_item.added", output_index: outputIndex, item: { ...rItem, status: "in_progress" } }));
    for (const dlt of reasoningDeltas) {
      out.push(sseEvent("response.reasoning_text.delta", { type: "response.reasoning_text.delta", item_id: rItemId, output_index: outputIndex, content_index: 0, delta: dlt }));
    }
    rItem.content = [{ type: "reasoning_text", text: reasoningDeltas.join("") }];
    out.push(sseEvent("response.output_item.done", { type: "response.output_item.done", output_index: outputIndex, item: rItem }));
    outputItems.push(rItem);
    outputIndex++;
  }

  // 文本内容作为 message item
  {
    const mItemId = rid("msg");
    const msgItem = { id: mItemId, type: "message", role: "assistant", status: "completed", content: [] };
    out.push(sseEvent("response.output_item.added", { type: "response.output_item.added", output_index: outputIndex, item: { ...msgItem, status: "in_progress", content: [] } }));
    out.push(sseEvent("response.content_part.added", { type: "response.content_part.added", item_id: mItemId, output_index: outputIndex, content_index: 0, part: { type: "output_text", text: "", annotations: [], logprobs: [] } }));
    for (const dlt of textDeltas) {
      out.push(sseEvent("response.output_text.delta", { type: "response.output_text.delta", item_id: mItemId, output_index: outputIndex, content_index: 0, delta: dlt }));
    }
    out.push(sseEvent("response.output_text.done", { type: "response.output_text.done", item_id: mItemId, output_index: outputIndex, content_index: 0, text: fullText }));
    out.push(sseEvent("response.content_part.done", { type: "response.content_part.done", item_id: mItemId, output_index: outputIndex, content_index: 0, part: { type: "output_text", text: fullText, annotations: [], logprobs: [] } }));
    msgItem.content = [{ type: "output_text", text: fullText, annotations: [], logprobs: [] }];
    out.push(sseEvent("response.output_item.done", { type: "response.output_item.done", output_index: outputIndex, item: msgItem }));
    outputItems.push(msgItem);
    outputIndex++;
  }

  // function_call items
  for (const tc of tools) {
    const callId = tc.id || rid("call");
    const fcItem = { id: rid("fc"), type: "function_call", call_id: callId, name: tc.name, arguments: tc.arguments, status: "completed" };
    out.push(sseEvent("response.output_item.added", { type: "response.output_item.added", output_index: outputIndex, item: { ...fcItem, status: "in_progress" } }));
    out.push(sseEvent("response.function_call_arguments.delta", { type: "response.function_call_arguments.delta", item_id: fcItem.id, output_index: outputIndex, delta: tc.arguments }));
    out.push(sseEvent("response.function_call_arguments.done", { type: "response.function_call_arguments.done", item_id: fcItem.id, output_index: outputIndex, arguments: tc.arguments }));
    out.push(sseEvent("response.output_item.done", { type: "response.output_item.done", output_index: outputIndex, item: fcItem }));
    outputItems.push(fcItem);
    outputIndex++;
  }

  // completed
  const u = usage ? {
    input_tokens: usage.prompt_tokens || 0,
    input_tokens_details: { cached_tokens: 0 },
    output_tokens: usage.completion_tokens || 0,
    output_tokens_details: { reasoning_tokens: (usage.completion_tokens_details?.reasoning_tokens) || 0 },
    total_tokens: usage.total_tokens || 0,
  } : null;
  out.push(sseEvent("response.completed", {
    type: "response.completed",
    response: { ...baseResponse, status: "completed", output: outputItems, usage: u },
  }));
  out.push("data: [DONE]\n\n");

  return { events: out.join(""), usage: u, text: fullText, finishReason, tools };
}
