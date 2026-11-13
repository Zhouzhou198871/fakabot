const ORIGIN = "https://dahaoshang.com";
const PUBLIC_HOST = "auth.lynli.me";
const SESSION_PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqQC4wdKepZunvRI8Ogj2\nqsTqtRaJOpO2fytiQi1tB1mQhz1SQ/Gr0pWlzfMNw3mO9qk4fBRygpOlVTijRNil\n5YOz1Ke7q5893bTtFaBWABjPP9+y+HzBe/jDvRNeJIyzqpPLkILXM9wf+hrca1GI\n/LT9dLNUfKe0zcxAPinHFdUGMD7ra+Dk9BJ/NwrJ4pt4UWUX2l59LT730NMsKskJ\ntmJMaBLpwTCJZYGuPEjiBRX9tGv1BZ1gusBBLSrpxMScINSHK6Jv7d7cjg7JYUDL\n6+BUJbaYP+IqAcRPtT397/hjbRBBoralHP7lpDSoABgFHTj23wz1zBPCPy2iFlxm\n5QIDAQAB\n-----END PUBLIC KEY-----";

const SPA_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, minimum-scale=1.0, user-scalable=no">
<meta name="referrer" content="strict-origin-when-cross-origin">
<title>大号商 - 专业账号购买与账号出售平台</title>
<link rel="icon" href="/logo.png" type="image/x-icon">
<link rel="stylesheet" href="/assets/index-6pebbnt6.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="/assets/index-koqdjogt.js"></script>
</body>
</html>`;

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
});

function pemBytes(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s/g, "");
  return Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
}
function base64(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function encryptSessionToken(token) {
  const rsaKey = await crypto.subtle.importKey(
    "spki", pemBytes(SESSION_PUBLIC_KEY),
    { name: "RSA-OAEP", hash: "SHA-256" }, false, ["encrypt"],
  );
  const aesBytes = crypto.getRandomValues(new Uint8Array(32));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const aesKey = await crypto.subtle.importKey("raw", aesBytes, { name: "AES-GCM" }, false, ["encrypt"]);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv }, aesKey, new TextEncoder().encode(token),
  );
  const wrappedKey = await crypto.subtle.encrypt({ name: "RSA-OAEP" }, rsaKey, aesBytes);
  return {
    v: 1,
    alg: "RSA-OAEP-256+A256GCM",
    iv: base64(iv),
    ciphertext: base64(new Uint8Array(ciphertext)),
    wrapped_key: base64(new Uint8Array(wrappedKey)),
  };
}
function tokenExpiry(token) {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const claims = JSON.parse(atob(part + "=".repeat((4 - part.length % 4) % 4)));
    if (claims.exp) return new Date(claims.exp * 1000).toISOString();
  } catch {}
  return new Date(Date.now() + 86400000).toISOString();
}
async function persistSession(token, env) {
  const expiresAt = tokenExpiry(token);
  const encryptedState = JSON.stringify(await encryptSessionToken(token));
  await env.DB.prepare(
    "INSERT INTO supplier_sessions (id, provider, status, expires_at, encrypted_state, updated_at) VALUES (?, ?, ?, ?, ?, ?) " +
    "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider, status=excluded.status, expires_at=excluded.expires_at, encrypted_state=excluded.encrypted_state, updated_at=excluded.updated_at",
  ).bind("dahaoshang-primary", "dahaoshang", "authorized", expiresAt, encryptedState, new Date().toISOString()).run();
}
async function probe(env) {
  const checkedAt = new Date().toISOString();
  let result;
  try {
    const response = await fetch(ORIGIN + "/", {
      headers: { "user-agent": "Mozilla/5.0", accept: "text/html" },
      redirect: "follow",
    });
    const html = await response.text();
    result = {
      ok: response.ok, status: response.status, final_url: response.url,
      content_type: response.headers.get("content-type"),
      bytes: new TextEncoder().encode(html).length, checked_at: checkedAt,
    };
  } catch (error) {
    result = { ok: false, error: String(error).slice(0, 200), checked_at: checkedAt };
  }
  await env.DB.prepare(
    "INSERT INTO supplier_probe_runs (checked_at, status_code, ok, result_json) VALUES (?, ?, ?, ?)",
  ).bind(checkedAt, result.status || null, result.ok ? 1 : 0, JSON.stringify(result)).run();
  return result;
}

export default {
  async fetch(request, env) {
    const incoming = new URL(request.url);

    const isSpaRoute = request.method === "GET" &&
      !incoming.pathname.startsWith("/api/") &&
      !incoming.pathname.startsWith("/assets/") &&
      !incoming.pathname.startsWith("/_internal_") &&
      incoming.pathname !== "/_proxy_health" &&
      !/\\.[a-zA-Z0-9]{1,8}$/.test(incoming.pathname);
    if (isSpaRoute) {
      return new Response(SPA_HTML, { headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
      }});
    }
    if (incoming.pathname === "/_proxy_health") {
      return new Response("auth proxy online", { headers: { "cache-control": "no-store" } });
    }
    if (incoming.pathname === "/_internal_session_status") {
      const row = await env.DB.prepare(
        "SELECT status, expires_at, updated_at FROM supplier_sessions WHERE id = ?",
      ).bind("dahaoshang-primary").first();
      const usable = row?.status === "authorized" &&
        (!row.expires_at || Date.parse(row.expires_at.replace(" ", "T") + "Z") > Date.now());
      return json({
        provider: "dahaoshang", status: row?.status || "not_connected",
        expires_at: row?.expires_at || null, usable: Boolean(usable),
      });
    }
    if (incoming.pathname === "/_internal/session") {
      const row = await env.DB.prepare(
        "SELECT status, expires_at, encrypted_state FROM supplier_sessions WHERE id = ?",
      ).bind("dahaoshang-primary").first();
      if (!row || row.status !== "authorized" ||
          (row.expires_at && Date.parse(row.expires_at.replace(" ", "T") + "Z") <= Date.now())) {
        return json({ ok: false, error: "supplier_session_unavailable" }, 409);
      }
      try {
        return json({ ok: true, encrypted_state: JSON.parse(row.encrypted_state), expires_at: row.expires_at });
      } catch {
        return json({ ok: false, error: "session_data_invalid" }, 503);
      }
    }
    if (incoming.pathname === "/_internal_probe") return json(await probe(env));

    // All supplier API calls use the IPv4-only relay, not Worker-to-origin fetch.
    const useRelay = incoming.pathname.startsWith("/api/");
    const target = new URL(useRelay ? "https://relay.lynli.me" : ORIGIN);
    target.pathname = useRelay ? "/_supplier" + incoming.pathname : incoming.pathname;
    target.search = incoming.search;
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.set("origin", ORIGIN);
    headers.set("referer", ORIGIN + "/");
    headers.delete("cf-connecting-ip");

    const upstream = await fetch(new Request(target.toString(), {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
      redirect: "manual",
    }));

    if (incoming.pathname === "/api/user/loginjwt" && request.method === "POST" && upstream.ok) {
      try {
        const payload = await upstream.clone().json();
        const token = payload?.Data?.access_token;
        if (payload?.Code === 200 && typeof token === "string" && token.length > 20) {
          await persistSession(token, env);
        }
      } catch (error) {
        console.error("supplier session persistence failed", String(error).slice(0, 100));
      }
    }
    if (incoming.pathname === "/api/user/logout" && request.method === "POST") {
      await env.DB.prepare(
        "UPDATE supplier_sessions SET status = 'revoked', encrypted_state = NULL, updated_at = ? WHERE id = ?",
      ).bind(new Date().toISOString(), "dahaoshang-primary").run();
    }

    const out = new Headers(upstream.headers);
    const location = out.get("location");
    if (location) {
      try {
        const next = new URL(location, ORIGIN);
        if (next.hostname === "dahaoshang.com" || next.hostname.endsWith(".dahaoshang.com")) {
          out.set("location", "https://" + PUBLIC_HOST + next.pathname + next.search + next.hash);
        }
      } catch {}
    }
    const cookies = upstream.headers.getSetCookie ? upstream.headers.getSetCookie() : [];
    if (cookies.length) {
      out.delete("set-cookie");
      for (let cookie of cookies) {
        cookie = cookie.split(";").filter((part) => !part.trim().toLowerCase().startsWith("domain=")).join(";");
        out.append("set-cookie", cookie);
      }
    }
    out.delete("content-security-policy");
    out.delete("content-security-policy-report-only");
    out.delete("x-frame-options");
    out.set("cache-control", "no-store");
    const type = (out.get("content-type") || "").toLowerCase();
    if (type.includes("text/html") || type.includes("javascript") || type.includes("json")) {
      let body = await upstream.text();
      body = body.split("https://dahaoshang.com").join("https://" + PUBLIC_HOST)
        .split("http://dahaoshang.com").join("https://" + PUBLIC_HOST)
        .split("//dahaoshang.com").join("//" + PUBLIC_HOST)
        .split("dahaoshang.com").join(PUBLIC_HOST);
      return new Response(body, { status: upstream.status, headers: out });
    }
    return new Response(upstream.body, { status: upstream.status, headers: out });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(probe(env));
  },
};