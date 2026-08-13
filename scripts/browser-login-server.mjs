import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import httpProxy from "http-proxy";


const execFileAsync = promisify(execFile);
const bindHost = process.env.BROWSER_LOGIN_BIND_HOST || "127.0.0.1";
const port = Number(process.env.BROWSER_LOGIN_PORT || "9380");
const controlKey = process.env.BROWSER_LOGIN_CONTROL_KEY || "";
const hermesRoot = process.env.HERMES_ROOT || "";
const hermesHome = process.env.HERMES_HOME || "";
const hermesPython = `${hermesRoot}/venv/bin/python`;
const camofoxUrl = "http://127.0.0.1:9377";
const noVncUrl = "http://127.0.0.1:6080";
const statePath = process.env.BROWSER_LOGIN_STATE_PATH || "";
const publicPrefix = "/browser-login";
const scopePattern = /^recipes-cart-user-[1-9][0-9]*$/;
const idPattern = /^[A-Za-z0-9_-]{20,128}$/;
const proxy = httpProxy.createProxyServer({ target: noVncUrl, ws: true });

if (!controlKey || !hermesRoot || !hermesHome || !statePath || !Number.isInteger(port)) {
  throw new Error("Browser login service environment is incomplete");
}

let activeSession = null;
let startingSession = false;
let controlMutationQueue = Promise.resolve();

function serializeControlMutation(operation) {
  const result = controlMutationQueue.then(operation, operation);
  controlMutationQueue = result.catch(() => {});
  return result;
}

function randomToken(bytes = 32) {
  return crypto.randomBytes(bytes).toString("base64url");
}

function persistRecoveryState(session) {
  const temporaryPath = `${statePath}.tmp`;
  const file = fs.openSync(temporaryPath, "w", 0o600);
  try {
    fs.writeFileSync(
      file,
      `${JSON.stringify({ id: session.id, userId: session.userId, scope: session.scope })}\n`,
    );
    fs.fsyncSync(file);
  } finally {
    fs.closeSync(file);
  }
  fs.renameSync(temporaryPath, statePath);
  fsyncStateDirectory();
}

function fsyncStateDirectory() {
  const directory = fs.openSync(path.dirname(statePath), "r");
  try {
    fs.fsyncSync(directory);
  } finally {
    fs.closeSync(directory);
  }
}

function clearRecoveryState() {
  try {
    fs.unlinkSync(statePath);
    fsyncStateDirectory();
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}

function digest(value) {
  return crypto.createHash("sha256").update(value).digest();
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(left || "");
  const rightBuffer = Buffer.from(right || "");
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function sendJson(response, status, body) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(JSON.stringify(body));
}

async function readJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 16_384) throw new Error("request_too_large");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function isControlRequest(request) {
  const value = request.headers.authorization || "";
  return value.startsWith("Bearer ") && safeEqual(value.slice(7), controlKey);
}

function cookieValue(request, name) {
  for (const pair of (request.headers.cookie || "").split(";")) {
    const [key, ...value] = pair.trim().split("=");
    if (key === name) return value.join("=");
  }
  return "";
}

function currentSession(id) {
  if (
    !activeSession
    || activeSession.id !== id
    || activeSession.closing
    || activeSession.expiresAt <= Date.now()
  ) return null;
  return activeSession;
}

function sessionFromPublicRequest(request, id) {
  const session = currentSession(id);
  const cookie = cookieValue(request, "recipes_browser_login");
  if (!session || !cookie || !safeEqual(digest(cookie), session.cookieDigest)) return null;
  return session;
}

async function camofoxIdentity(scope) {
  const program = [
    "import json, sys",
    "from tools.browser_camofox_state import get_camofox_identity",
    "print(json.dumps(get_camofox_identity(sys.argv[1])))",
  ].join("; ");
  const { stdout } = await execFileAsync(hermesPython, ["-c", program, scope], {
    cwd: hermesRoot,
    env: { ...process.env, HERMES_HOME: hermesHome },
    timeout: 10_000,
    maxBuffer: 16_384,
  });
  const identity = JSON.parse(stdout);
  if (!identity.user_id || !identity.session_key) throw new Error("invalid_identity");
  return identity;
}

async function openCamofox(identity) {
  const response = await fetch(`${camofoxUrl}/tabs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      userId: identity.user_id,
      sessionKey: identity.session_key,
      url: "https://eda.yandex.ru/retail",
    }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`camofox_start_${response.status}`);
  return identity.user_id;
}

async function closeCamofoxUser(userId) {
  const response = await fetch(`${camofoxUrl}/sessions/${encodeURIComponent(userId)}`, {
    method: "DELETE",
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok && response.status !== 404) throw new Error(`camofox_stop_${response.status}`);
}

async function recoverInterruptedSession() {
  let recovery;
  try {
    recovery = JSON.parse(fs.readFileSync(statePath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return;
    throw error;
  }
  if (!recovery || !idPattern.test(String(recovery.id || "")) || !recovery.userId) {
    throw new Error("Invalid browser login recovery state");
  }
  await closeCamofoxUser(recovery.userId);
  clearRecoveryState();
}

async function closeActiveSession(expectedId = null) {
  const session = activeSession;
  if (!session || (expectedId && session.id !== expectedId)) return false;
  session.closing = true;
  for (const socket of session.webSockets) socket.destroy();
  session.webSockets.clear();
  try {
    await closeCamofoxUser(session.userId);
  } catch (error) {
    console.error("Failed to stop Camofox login session", error);
    throw error;
  }
  clearRecoveryState();
  if (activeSession?.id === session.id) activeSession = null;
  return true;
}

function issueAccess(session) {
  const token = randomToken();
  session.accessDigests.add(digest(token).toString("hex"));
  return `${publicPrefix}/access/${token}`;
}

async function handleControl(request, response, url) {
  if (!isControlRequest(request)) return sendJson(response, 401, { error: "unauthorized" });

  if (request.method === "POST" && url.pathname === "/v1/sessions") {
    const body = await readJson(request);
    const scope = String(body.scope || "");
    const requestedSessionId = String(body.session_id || "");
    const lifetimeMinutes = Number(body.lifetime_minutes || 15);
    if (!scopePattern.test(scope) || !idPattern.test(requestedSessionId) || !Number.isInteger(lifetimeMinutes) || lifetimeMinutes < 5 || lifetimeMinutes > 30) {
      return sendJson(response, 400, { error: "invalid_request" });
    }
    return serializeControlMutation(async () => {
      if (startingSession || (activeSession && activeSession.expiresAt > Date.now())) {
        return sendJson(response, 409, { error: "browser_busy" });
      }
      if (activeSession) await closeActiveSession();
      startingSession = true;
      try {
        const identity = await camofoxIdentity(scope);
        persistRecoveryState({ id: requestedSessionId, userId: identity.user_id, scope });
        try {
          await openCamofox(identity);
        } catch (openError) {
          try {
            await closeCamofoxUser(identity.user_id);
          } catch (cleanupError) {
            console.error("Ambiguous Camofox start could not be cleaned up", cleanupError);
            process.nextTick(() => process.exit(1));
            throw cleanupError;
          }
          clearRecoveryState();
          throw openError;
        }
        const cookieToken = randomToken();
        activeSession = {
          id: requestedSessionId,
          scope,
          userId: identity.user_id,
          expiresAt: Date.now() + lifetimeMinutes * 60_000,
          cookieDigest: digest(cookieToken),
          cookieToken,
          accessDigests: new Set(),
          webSockets: new Set(),
          closing: false,
        };
      } finally {
        startingSession = false;
      }
      return sendJson(response, 201, { session_id: activeSession.id });
    });
  }

  const match = url.pathname.match(/^\/v1\/sessions\/([A-Za-z0-9_-]{20,128})(?:\/(access))?$/);
  if (!match || !idPattern.test(match[1])) return sendJson(response, 404, { error: "not_found" });
  if (request.method === "POST" && match[2] === "access") {
    const session = currentSession(match[1]);
    if (!session) return sendJson(response, 404, { error: "not_found" });
    return sendJson(response, 201, { access_path: issueAccess(session) });
  }
  if (request.method === "DELETE" && !match[2]) {
    return serializeControlMutation(async () => {
      if (activeSession && activeSession.id !== match[1]) {
        return sendJson(response, 409, { error: "browser_busy" });
      }
      if (!activeSession) {
        return sendJson(response, 404, { error: "not_found" });
      }
      await closeActiveSession(match[1]);
      return sendJson(response, 200, { status: "saved" });
    });
  }
  return sendJson(response, 405, { error: "method_not_allowed" });
}

async function handleAccess(request, response, token) {
  const session = activeSession;
  const tokenDigest = digest(token).toString("hex");
  if (!session || session.expiresAt <= Date.now() || !session.accessDigests.delete(tokenDigest)) {
    response.writeHead(410, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" });
    return response.end("Ссылка устарела. Вернитесь на страницу рецептов и откройте окно снова.");
  }
  const maxAge = Math.max(1, Math.floor((session.expiresAt - Date.now()) / 1000));
  response.writeHead(302, {
    Location: `${publicPrefix}/session/${session.id}/vnc.html?autoconnect=true&resize=scale&path=browser-login/session/${session.id}/websockify`,
    "Set-Cookie": `recipes_browser_login=${session.cookieToken}; Path=${publicPrefix}/session/${session.id}/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Strict`,
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
  });
  response.end();
}

proxy.on("proxyRes", (proxyResponse) => {
  proxyResponse.headers["cache-control"] = "no-store";
  proxyResponse.headers["x-frame-options"] = "SAMEORIGIN";
  proxyResponse.headers["content-security-policy"] = "frame-ancestors 'self'";
  proxyResponse.headers["referrer-policy"] = "no-referrer";
});
proxy.on("error", (error, request, response) => {
  console.error("noVNC proxy error", error);
  if (response && typeof response.writeHead === "function" && !response.headersSent) {
    sendJson(response, 502, { error: "browser_unavailable" });
  } else if (typeof response?.destroy === "function") {
    response.destroy();
  } else if (typeof response?.end === "function") {
    response.end();
  }
});

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://browser-login.local");
    if (request.method === "GET" && url.pathname === "/healthz") {
      return sendJson(response, 200, { status: "ok" });
    }
    if (url.pathname.startsWith("/v1/")) return await handleControl(request, response, url);
    const accessMatch = url.pathname.match(/^\/browser-login\/access\/([A-Za-z0-9_-]{20,256})$/);
    if (request.method === "GET" && accessMatch) return await handleAccess(request, response, accessMatch[1]);
    const sessionMatch = url.pathname.match(/^\/browser-login\/session\/([A-Za-z0-9_-]{20,128})(\/.*)$/);
    if (request.method === "GET" && sessionMatch && sessionFromPublicRequest(request, sessionMatch[1])) {
      request.url = sessionMatch[2] + url.search;
      return proxy.web(request, response);
    }
    return sendJson(response, 404, { error: "not_found" });
  } catch (error) {
    console.error("Browser login request failed", error);
    return sendJson(response, 500, { error: "internal_error" });
  }
});

server.on("upgrade", (request, socket, head) => {
  const url = new URL(request.url, "http://browser-login.local");
  const match = url.pathname.match(/^\/browser-login\/session\/([A-Za-z0-9_-]{20,128})\/websockify$/);
  const session = match && sessionFromPublicRequest(request, match[1]);
  if (!session) return socket.destroy();
  session.webSockets.add(socket);
  socket.once("close", () => session.webSockets.delete(socket));
  request.url = "/websockify";
  try {
    proxy.ws(request, socket, head);
  } catch (error) {
    console.error("noVNC WebSocket proxy failed", error);
    session.webSockets.delete(socket);
    socket.destroy();
  }
});

setInterval(() => {
  if (activeSession && activeSession.expiresAt <= Date.now()) {
    serializeControlMutation(() => closeActiveSession())
      .catch((error) => console.error("Failed to expire browser login session", error));
  }
}, 15_000).unref();

await recoverInterruptedSession();

server.listen(port, bindHost, () => {
  console.log(`Browser login service listening on http://${bindHost}:${port}`);
});
