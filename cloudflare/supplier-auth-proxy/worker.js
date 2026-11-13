const ORIGIN = "https://dahaoshang.com";
const PUBLIC_HOST = "auth.lynli.me";
const SESSION_PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqQC4wdKepZunvRI8Ogj2\nqsTqtRaJOpO2fytiQi1tB1mQhz1SQ/Gr0pWlzfMNw3mO9qk4fBRygpOlVTijRNil\n5YOz1Ke7q5893bTtFaBWABjPP9+y+HzBe/jDvRNeJIyzqpPLkILXM9wf+hrca1GI\n/LT9dLNUfKe0zcxAPinHFdUGMD7ra+Dk9BJ/NwrJ4pt4UWUX2l59LT730NMsKskJ\ntmJMaBLpwTCJZYGuPEjiBRX9tGv1BZ1gusBBLSrpxMScINSHK6Jv7d7cjg7JYUDL\n6+BUJbaYP+IqAcRPtT397/hjbRBBoralHP7lpDSoABgFHTj23wz1zBPCPy2iFlxm\n5QIDAQAB\n-----END PUBLIC KEY-----";

const LOGIN_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>大号商网页登录授权</title>
<meta name="referrer" content="no-referrer">
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6fa;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#182230;padding:20px}
main{width:min(100%,420px);background:#fff;border:1px solid #e5e9f0;border-radius:18px;padding:28px;box-shadow:0 12px 36px #17203312}
h1{font-size:22px;margin:0 0 8px}p{color:#667085;line-height:1.6;font-size:14px}
label{display:block;font-size:14px;font-weight:600;margin:18px 0 7px}input{width:100%;height:48px;border:1px solid #d0d5dd;border-radius:9px;padding:0 13px;font-size:16px}
button{width:100%;height:48px;margin-top:22px;border:0;border-radius:9px;background:#2458d3;color:white;font-size:16px;font-weight:650}
button:disabled{opacity:.6}#msg{min-height:42px;margin-top:16px;font-size:14px;white-space:pre-wrap}.ok{color:#067647}.bad{color:#b42318}
small{display:block;color:#667085;margin-top:18px;line-height:1.6}
</style></head>
<body><main>
<h1>大号商账号授权</h1>
<p>在此登录你的大号商账号。登录成功后，系统只会把加密后的登录会话保存到 Cloudflare，用于后续自动采购；不会把明文密码保存到机器人服务器。</p>
<form id="login">
<label for="account">账号 / 邮箱</label><input id="account" name="account" autocomplete="username" required minlength="3" maxlength="100">
<label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required minlength="6" maxlength="200">
<button id="submit" type="submit">登录并授权</button>
</form><div id="msg" role="status" aria-live="polite"></div>
<small>授权完成后请勿在其他设备主动退出该上游账号，否则登录会话可能失效。请只在官方大号商账号页面输入凭据。</small>
</main>
<script type="module">
import {e as md5} from "/assets/crypto-0zgdtz81.js";
const form=document.getElementById("login"),btn=document.getElementById("submit"),msg=document.getElementById("msg");
form.addEventListener("submit",async event=>{
 event.preventDefault();btn.disabled=true;btn.textContent="正在验证…";msg.className="";msg.textContent="正在向大号商验证账号，请稍候。";
 const account=document.getElementById("account").value.trim();
 const password=document.getElementById("password").value;
 try{
  const response=await fetch("/api/user/loginjwt",{method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},body:JSON.stringify({Account:account,Password:md5(password)})});
  const data=await response.json();
  if(!response.ok||data?.Code!==200||!data?.Data?.access_token)throw new Error(data?.Message||"登录未成功，请核对账号密码或按页面提示处理。");
  document.getElementById("password").value="";
  const status=await fetch("/_internal_session_status",{cache:"no-store"}).then(r=>r.json());
  if(status?.status==="authorized"&&status?.usable){msg.className="ok";msg.textContent="授权成功！Cloudflare 已保存加密登录会话。可以返回 Telegram 继续联调。";form.hidden=true;}
  else{msg.className="bad";msg.textContent="上游返回登录成功，但加密会话状态未确认。请勿重复登录或付款，稍后联系管理员核对。";}
 }catch(error){msg.className="bad";msg.textContent=error.message||"登录失败，请稍后重试。";}
 finally{btn.disabled=false;btn.textContent="登录并授权";}
});
</script></body></html>`;

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

    if (incoming.pathname === "/" && request.method === "GET") {
      return Response.redirect("https://" + PUBLIC_HOST + "/login", 302);
    }
    if (incoming.pathname === "/login" && request.method === "GET") {
      return new Response(LOGIN_HTML, { headers: {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
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

    const target = new URL(ORIGIN);
    target.pathname = incoming.pathname;
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