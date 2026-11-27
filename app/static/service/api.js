const ROOT = "/api/v1";
let token = "";

export function setSession(value) { token = value; }

export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

export async function request(path, { method = "GET", body, idempotencyKey, signal } = {}) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  if (body !== undefined && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(ROOT + path, { method, headers, body, signal, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = typeof error.detail === "string" ? error.detail : "请求未完成，请检查输入后重试。";
    throw new ApiError(response.status, detail);
  }
  return response;
}

export async function json(path, options) {
  const response = await request(path, options);
  return response.status === 204 ? null : response.json();
}

export async function readEvents(response, receive) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      let end;
      while ((end = pending.indexOf("\n\n")) !== -1) {
        const lines = pending.slice(0, end).split("\n");
        pending = pending.slice(end + 2);
        const payload = lines.filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
        if (payload) receive(JSON.parse(payload));
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
}
