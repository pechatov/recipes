import crypto from "node:crypto";
import http from "node:http";
import https from "node:https";
import { execFile } from "node:child_process";
import { AsyncLocalStorage } from "node:async_hooks";
import { readFileSync } from "node:fs";
import {
  mkdir,
  open as openFile,
  readFile,
  rename,
  stat,
  unlink,
} from "node:fs/promises";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";


const execFileAsync = promisify(execFile);
const bindHost = process.env.CART_ADAPTER_BIND_HOST || "127.0.0.1";
const port = Number(process.env.CART_ADAPTER_PORT || "9381");
const controlKey = process.env.CART_ADAPTER_CONTROL_KEY || "";
const hermesRoot = process.env.HERMES_ROOT || "";
const hermesHome = process.env.HERMES_HOME || "";
const hermesPython = `${hermesRoot}/venv/bin/python`;
const tlsCertPath = process.env.CART_ADAPTER_TLS_CERT || "";
const tlsKeyPath = process.env.CART_ADAPTER_TLS_KEY || "";
const operationStateFile = process.env.CART_ADAPTER_STATE_FILE
  || `${hermesHome}/cart-adapter-operations.json`;
const quarantineStateFile = process.env.CART_ADAPTER_QUARANTINE_FILE
  || `${hermesHome}/cart-adapter-quarantine.json`;
const camofoxUrl = (process.env.CAMOFOX_URL || "http://127.0.0.1:9377").replace(/\/$/, "");
const camofoxAccessKey = process.env.CAMOFOX_ACCESS_KEY || process.env.CAMOFOX_API_KEY || "";
const scopePattern = /^recipes-cart-user-[1-9][0-9]*$/;
const productIdPattern = /^[A-Za-z0-9_-]{8,128}$/;
const businessPattern = /^[a-z][a-z0-9_-]{0,63}$/;
const slugPattern = /^[A-Za-z0-9_-]{1,128}$/;
const operationIdPattern = /^cart-run-[1-9][0-9]{0,18}-[0-9]{20}-[a-z][a-z0-9_-]{0,31}$/;
const cleanupOperationIdPattern = new RegExp(
  `^(?:${operationIdPattern.source.slice(1, -1)}|[a-f0-9-]{36})$`,
);
const tokenLifetimeMs = 10 * 60 * 1000;
// All normal browser work must stop before Django's 210-second client timeout.
// closeBrowser has its own 20-second allowance, leaving roughly 30 seconds for
// response delivery and scheduling jitter.
const operationBudgetMs = 160_000;
const operationDeadline = new AsyncLocalStorage();
// Django caps confirmation at seven days. The extra day ensures a journal
// created before the worker persists its deadline still outlives that deadline.
const cleanupTokenLifetimeMs = 8 * 24 * 60 * 60 * 1000;
const completedOperationRetentionMs = 30 * 24 * 60 * 60 * 1000;
const startedOperationRetentionMs = 90 * 24 * 60 * 60 * 1000;
const maximumOperationRecords = 2_000;
const maximumOperationStateBytes = 16 * 1024 * 1024;
// Camofox's non-proxy tab creation is bounded by browser launch (60s), page
// recovery (two 10s attempts) and initial navigation (30s). Keep an extra
// margin before trusting that a timed-out POST /tabs can no longer create a
// late persistent session.
const deferredBrowserCreateSettlementMs = 2 * 60 * 1000;
const selectionKey = crypto.createHash("sha256").update(controlKey).digest();
const stores = {
  auchan: { aliases: ["ашан", "auchan"], groupSlugs: ["asan_giper"] },
  perekrestok: { aliases: ["перекресток", "perekrestok"], groupSlugs: ["perekrestok"] },
  pyaterochka: { aliases: ["пятерочка", "pyaterochka"], groupSlugs: ["paterocka"] },
  magnit: { aliases: ["магнит", "magnit"], groupSlugs: ["magnit_celevaya"] },
  lavka: {
    aliases: ["яндекс лавка", "лавка", "yandex lavka"],
    groupSlugs: ["lavka"],
    // The "Магазины" landing links Яндекс Лавка to lavka.yandex.ru, a separate
    // site without the Yandex Food retail catalog and cart APIs.
    unavailableSummary: "Яндекс Лавка работает на отдельном сайте и не поддерживается быстрой сборкой.",
  },
};

const loopbackHosts = new Set(["127.0.0.1", "::1", "localhost"]);
const tlsEnabled = Boolean(tlsCertPath && tlsKeyPath);
if (
  !controlKey
  || !hermesRoot
  || !hermesHome
  || !Number.isInteger(port)
  || Boolean(tlsCertPath) !== Boolean(tlsKeyPath)
  || (!tlsEnabled && !loopbackHosts.has(bindHost))
) {
  throw new Error("Cart adapter service environment is incomplete");
}

class OperationError extends Error {
  constructor(code, message, { mutationPossible = false } = {}) {
    super(message);
    this.code = code;
    this.mutationPossible = mutationPossible;
  }
}

function preserveMutationUncertainty(error, mutationPossible) {
  const preserved = error instanceof Error
    ? error
    : new Error("Cart adapter operation failed");
  if (mutationPossible) preserved.mutationPossible = true;
  return preserved;
}

function markProfileUncertain(error) {
  const uncertain = preserveMutationUncertainty(error, true);
  uncertain.profileUncertain = true;
  return uncertain;
}

function finalBrowserError(operationError, closeError, mutationPossible) {
  if (closeError) {
    // Even a read-only operation is no longer safe to hand to Hermes when the
    // adapter could not release its persistent profile. Preserve the useful
    // original error code while making the ownership uncertainty explicit.
    return markProfileUncertain(operationError || closeError);
  }
  return operationError
    ? preserveMutationUncertainty(operationError, mutationPossible)
    : null;
}

let operationRecordsPromise = null;
let operationWriteQueue = Promise.resolve();

function operationStateError() {
  return new OperationError(
    "operation_state_unavailable",
    "Журнал операций корзины недоступен; проверьте корзину вручную.",
    { mutationPossible: true },
  );
}

function pruneOperationRecords(records, now = Date.now()) {
  for (const [key, record] of Object.entries(records)) {
    if (!record || typeof record !== "object") throw operationStateError();
    const timestamp = Date.parse(
      record.status === "completed" ? record.completed_at : record.started_at,
    );
    const retention = record.status === "completed"
      ? completedOperationRetentionMs
      : record.status === "started"
        ? startedOperationRetentionMs
        : 0;
    if (!retention || !Number.isFinite(timestamp)) throw operationStateError();
    if (now - timestamp > retention) delete records[key];
  }
  return records;
}

async function loadOperationRecords() {
  if (!operationRecordsPromise) {
    operationRecordsPromise = (async () => {
      try {
        const information = await stat(operationStateFile);
        if (information.size > maximumOperationStateBytes) {
          throw new Error("operation state is too large");
        }
        const parsed = JSON.parse(await readFile(operationStateFile, "utf8"));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("invalid operation state");
        }
        return pruneOperationRecords(
          Object.assign(Object.create(null), parsed),
        );
      } catch (error) {
        if (error?.code === "ENOENT") return Object.create(null);
        if (error instanceof OperationError) throw error;
        throw operationStateError();
      }
    })();
  }
  return operationRecordsPromise;
}

async function readOperationRecord(key) {
  await operationWriteQueue;
  const records = await loadOperationRecords();
  return Object.hasOwn(records, key) ? records[key] : null;
}

async function durableWriteState(target, serialized) {
  const parent = dirname(target);
  await mkdir(parent, { recursive: true, mode: 0o700 });
  const temporary = `${target}.${process.pid}.${crypto.randomUUID()}.tmp`;
  let renamed = false;
  try {
    const file = await openFile(temporary, "w", 0o600);
    try {
      await file.writeFile(serialized);
      await file.sync();
    } finally {
      await file.close();
    }
    await rename(temporary, target);
    renamed = true;
    const directory = await openFile(parent, "r");
    try {
      await directory.sync();
    } finally {
      await directory.close();
    }
  } finally {
    if (!renamed) await unlink(temporary).catch(() => {});
  }
}

async function storeOperationRecord(key, record, writeState = durableWriteState) {
  const write = operationWriteQueue.then(async () => {
    const currentRecords = await loadOperationRecords();
    const records = Object.assign(Object.create(null), currentRecords);
    pruneOperationRecords(records);
    if (
      !Object.hasOwn(records, key)
      && Object.keys(records).length >= maximumOperationRecords
    ) {
      throw operationStateError();
    }
    records[key] = record;
    const serialized = JSON.stringify(records);
    if (Buffer.byteLength(serialized) > maximumOperationStateBytes) {
      throw operationStateError();
    }
    await writeState(operationStateFile, serialized);
    // Publish the new in-memory snapshot only after both durability barriers
    // completed. A failed write must remain indistinguishable from no write.
    operationRecordsPromise = Promise.resolve(records);
  });
  operationWriteQueue = write.catch(() => {});
  try {
    await write;
  } catch (error) {
    if (error instanceof OperationError) throw error;
    throw new OperationError(
      "operation_state_unavailable",
      "Не удалось сохранить журнал операции; проверьте корзину вручную.",
      { mutationPossible: true },
    );
  }
}

function operationFingerprint(scope, store, requested) {
  const items = [...requested]
    .map((item) => ({
      product_id: item.product_id,
      sku_id: item.sku_id,
      quantity: item.quantity,
    }))
    .sort((left, right) => left.product_id.localeCompare(right.product_id));
  return crypto.createHash("sha256")
    .update(JSON.stringify({ scope, store, items }))
    .digest("hex");
}

let quarantinedScopesPromise = null;
let quarantineWriteQueue = Promise.resolve();

function quarantineError(message = "Профиль корзины требует безопасного восстановления.") {
  return markProfileUncertain(new OperationError(
    "profile_quarantined",
    message,
    { mutationPossible: true },
  ));
}

async function loadQuarantinedScopes() {
  if (!quarantinedScopesPromise) {
    quarantinedScopesPromise = (async () => {
      try {
        const information = await stat(quarantineStateFile);
        if (information.size > 1_048_576) throw new Error("quarantine is too large");
        const parsed = JSON.parse(await readFile(quarantineStateFile, "utf8"));
        // Older deployments stored a bare array. Preserve every lease and use
        // the file timestamp as the earliest possible create dispatch time.
        let entries = null;
        if (Array.isArray(parsed)) {
          entries = parsed.map((scope) => [
            scope,
            information.mtimeMs + deferredBrowserCreateSettlementMs,
          ]);
        } else if (
          parsed?.version === 1
          && parsed.scopes
          && typeof parsed.scopes === "object"
        ) {
          entries = Object.entries(parsed.scopes);
        }
        if (
          !entries
          || entries.length > 10_000
          || entries.some(([scope, recoverAfter]) => (
            !scopePattern.test(String(scope))
            || !Number.isFinite(Number(recoverAfter))
            || Number(recoverAfter) < 0
          ))
        ) {
          throw new Error("invalid quarantine state");
        }
        return new Map(entries.map(([scope, recoverAfter]) => [
          String(scope),
          Number(recoverAfter),
        ]));
      } catch (error) {
        if (error?.code === "ENOENT") return new Map();
        throw quarantineError("Журнал карантина профилей недоступен.");
      }
    })();
  }
  return quarantinedScopesPromise;
}

async function persistQuarantinedScopes(scopes) {
  const serialized = JSON.stringify({
    version: 1,
    scopes: Object.fromEntries([...scopes.entries()].sort(([left], [right]) => (
      left.localeCompare(right)
    ))),
  });
  if (Buffer.byteLength(serialized) > 1_048_576) throw quarantineError();
  await durableWriteState(quarantineStateFile, serialized);
}

async function quarantineScope(scope) {
  const write = quarantineWriteQueue.then(async () => {
    const scopes = new Map(await loadQuarantinedScopes());
    scopes.set(scope, Date.now());
    await persistQuarantinedScopes(scopes);
    quarantinedScopesPromise = Promise.resolve(scopes);
  });
  quarantineWriteQueue = write.catch(() => {});
  await write;
}

async function deferScopeRecovery(scope, now = Date.now()) {
  const recoverAfter = now + deferredBrowserCreateSettlementMs;
  let effectiveRecoverAfter = recoverAfter;
  const write = quarantineWriteQueue.then(async () => {
    const scopes = new Map(await loadQuarantinedScopes());
    if (!scopes.has(scope)) throw quarantineError();
    effectiveRecoverAfter = Math.max(scopes.get(scope) || 0, recoverAfter);
    scopes.set(scope, effectiveRecoverAfter);
    await persistQuarantinedScopes(scopes);
    quarantinedScopesPromise = Promise.resolve(scopes);
  });
  quarantineWriteQueue = write.catch(() => {});
  await write;
  return effectiveRecoverAfter;
}

async function releaseScopeQuarantine(scope) {
  const write = quarantineWriteQueue.then(async () => {
    const scopes = await loadQuarantinedScopes();
    if (!scopes.has(scope)) return;
    const remaining = new Map(scopes);
    remaining.delete(scope);
    await persistQuarantinedScopes(remaining);
    quarantinedScopesPromise = Promise.resolve(remaining);
  });
  quarantineWriteQueue = write.catch(() => {});
  await write;
}

async function isScopeQuarantined(scope) {
  await quarantineWriteQueue;
  return (await loadQuarantinedScopes()).has(scope);
}

async function scopeRecoveryAt(scope) {
  await quarantineWriteQueue;
  return (await loadQuarantinedScopes()).get(scope) ?? null;
}

async function recoverQuarantinedScope(scope) {
  const recoverAfter = await scopeRecoveryAt(scope);
  if (recoverAfter === null) return;
  if (Date.now() < recoverAfter) {
    throw quarantineError(
      "Браузер завершает предыдущую операцию; повторите попытку через несколько минут.",
    );
  }
  try {
    const identity = await camofoxIdentity(scope);
    await closeBrowser(identity.user_id);
    await releaseScopeQuarantine(scope);
  } catch (error) {
    throw quarantineError(
      error?.message || "Профиль корзины пока не удалось безопасно восстановить.",
    );
  }
}

const activeScopes = new Set();

async function runExclusiveOperation(scope, operation) {
  if (activeScopes.has(scope)) {
    // Never enqueue work that could begin after the caller's timeout. The
    // active operation may still own the profile, so busy is uncertain and
    // must not permit a Hermes fallback.
    throw new OperationError(
      "scope_busy",
      "Другая операция с этой корзиной ещё выполняется.",
      { mutationPossible: true },
    );
  }
  activeScopes.add(scope);
  let leased = false;
  try {
    await recoverQuarantinedScope(scope);
    // Persist exclusive ownership before any operation can open Camofox. If
    // this process dies, the next one observes the lease and closes the old
    // session before doing any work with the same profile.
    await quarantineScope(scope);
    leased = true;
    const result = await operation();
    await releaseScopeQuarantine(scope);
    leased = false;
    return result;
  } catch (error) {
    if (leased && !error?.profileUncertain) {
      try {
        await releaseScopeQuarantine(scope);
        leased = false;
      } catch (releaseFailure) {
        throw quarantineError(releaseFailure?.message);
      }
    }
    throw error;
  } finally {
    activeScopes.delete(scope);
  }
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(String(left || ""));
  const rightBuffer = Buffer.from(String(right || ""));
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer);
}

function isAuthorized(request) {
  const value = request.headers.authorization || "";
  return value.startsWith("Bearer ") && safeEqual(value.slice(7), controlKey);
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
    if (size > 65_536) throw new OperationError("invalid_request", "Запрос слишком большой.");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw new OperationError("invalid_request", "Неверный JSON запроса.");
  }
}

function text(value, maximum) {
  const result = String(value || "").trim();
  return result.length <= maximum ? result : "";
}

function validateBaseRequest(body) {
  const scope = text(body?.scope, 80);
  const store = text(body?.store, 32);
  if (!scopePattern.test(scope) || !Object.hasOwn(stores, store)) {
    throw new OperationError("invalid_request", "Неверные границы операции с корзиной.");
  }
  return { scope, store };
}

function normalize(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase("ru")
    .replaceAll("ё", "е")
    .replace(/[^a-zа-я0-9]+/g, " ")
    .trim();
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function boundedOperationTimeout(maximum) {
  const requested = Math.max(1, Number(maximum) || 1);
  const deadline = operationDeadline.getStore()?.deadline;
  if (!Number.isFinite(deadline)) return requested;
  return Math.max(1, Math.min(requested, deadline - Date.now()));
}

function runWithOperationDeadline(operation, budget = operationBudgetMs) {
  return operationDeadline.run(
    { deadline: Date.now() + Math.max(1, Number(budget) || 1) },
    operation,
  );
}

function camofoxHeaders(json = false) {
  const headers = {};
  if (json) headers["Content-Type"] = "application/json";
  if (camofoxAccessKey) headers.Authorization = `Bearer ${camofoxAccessKey}`;
  return headers;
}

async function camofoxRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(`${camofoxUrl}${path}`, {
      ...options,
      headers: { ...camofoxHeaders(Boolean(options.body)), ...(options.headers || {}) },
      signal: AbortSignal.timeout(
        boundedOperationTimeout(options.timeout || 30_000),
      ),
    });
  } catch (error) {
    throw new OperationError("browser_unavailable", "Браузерная сессия недоступна.");
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    // Status is sufficient; never copy an upstream HTML response to callers.
  }
  if (!response.ok) {
    throw new OperationError("browser_unavailable", `Браузер вернул ошибку ${response.status}.`);
  }
  return data;
}

async function camofoxIdentity(scope) {
  const program = [
    "import json, sys",
    "from tools.browser_camofox_state import get_camofox_identity",
    "print(json.dumps(get_camofox_identity(sys.argv[1])))",
  ].join("; ");
  let stdout;
  try {
    ({ stdout } = await execFileAsync(hermesPython, ["-c", program, scope], {
      cwd: hermesRoot,
      env: { ...process.env, HERMES_HOME: hermesHome },
      timeout: boundedOperationTimeout(10_000),
      maxBuffer: 16_384,
    }));
  } catch {
    throw new OperationError("browser_unavailable", "Не удалось открыть профиль корзины.");
  }
  let identity;
  try {
    identity = JSON.parse(stdout);
  } catch {
    throw new OperationError("browser_unavailable", "Профиль корзины повреждён.");
  }
  if (!identity.user_id || !identity.session_key) {
    throw new OperationError("browser_unavailable", "Профиль корзины повреждён.");
  }
  return identity;
}

async function openBrowser(scope, initialUrl = "https://eda.yandex.ru/retail") {
  const identity = await camofoxIdentity(scope);
  // Extend the durable lease before dispatching POST /tabs. If the adapter or
  // connection dies, recovery cannot release the profile until every bounded
  // part of Camofox's abandoned create request has had time to settle.
  await deferScopeRecovery(scope);
  let opened;
  try {
    opened = await camofoxRequest("/tabs", {
      method: "POST",
      body: JSON.stringify({
        userId: identity.user_id,
        sessionKey: identity.session_key,
        url: initialUrl,
      }),
      timeout: 25_000,
    });
  } catch (error) {
    // A lost create response may still have opened the persistent profile,
    // so attempt an immediate close and quarantine the scope until a later
    // request confirms another close. This also covers a delayed create that
    // races the first cleanup request.
    try {
      await closeBrowser(identity.user_id);
    } catch {}
    throw markProfileUncertain(error);
  }
  const tabId = String(opened.tabId || opened.id || "");
  if (!tabId) {
    const missingTab = new OperationError(
      "browser_unavailable",
      "Браузер не вернул вкладку.",
    );
    try {
      await closeBrowser(identity.user_id);
    } catch {
      throw markProfileUncertain(missingTab);
    }
    throw missingTab;
  }
  return { identity, tabId };
}

async function closeBrowser(userId) {
  let response;
  try {
    response = await fetch(`${camofoxUrl}/sessions/${encodeURIComponent(userId)}`, {
      method: "DELETE",
      headers: camofoxHeaders(),
      signal: AbortSignal.timeout(20_000),
    });
  } catch {
    throw new OperationError(
      "browser_close_failed",
      "Не удалось безопасно закрыть браузер.",
      { mutationPossible: true },
    );
  }
  if (!response.ok && response.status !== 404) {
    throw new OperationError(
      "browser_close_failed",
      "Не удалось безопасно закрыть браузер.",
      { mutationPossible: true },
    );
  }
}

async function evaluate(browser, expression, timeout = 60_000) {
  const data = await camofoxRequest(`/tabs/${encodeURIComponent(browser.tabId)}/evaluate`, {
    method: "POST",
    body: JSON.stringify({ userId: browser.identity.user_id, expression }),
    timeout,
  });
  if (data.ok === false || !("result" in data)) {
    throw new OperationError("browser_evaluate_failed", "Страница магазина вернула неполный результат.");
  }
  return data.result;
}

async function navigate(browser, url) {
  await camofoxRequest(`/tabs/${encodeURIComponent(browser.tabId)}/navigate`, {
    method: "POST",
    body: JSON.stringify({ userId: browser.identity.user_id, url }),
    timeout: 30_000,
  });
}

async function withBrowser(
  scope,
  operation,
  mutationState = { possible: false },
  initialUrl = "https://eda.yandex.ru/retail",
) {
  const browser = await openBrowser(scope, initialUrl);
  let operationError = null;
  let closeError = null;
  let result;
  try {
    result = await operation(browser, mutationState);
  } catch (error) {
    operationError = error;
  }
  try {
    await closeBrowser(browser.identity.user_id);
  } catch (error) {
    closeError = error;
  }
  const failure = finalBrowserError(
    operationError,
    closeError,
    mutationState.possible,
  );
  if (failure) throw failure;
  return result;
}

const pageStateExpression = `(() => {
  const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
  const links = [...document.querySelectorAll('a[href]')]
    .map((anchor) => ({
      text: clean(anchor.innerText || anchor.getAttribute('aria-label') || anchor.title || anchor.querySelector('img')?.alt),
      href: anchor.href,
    }))
    .filter((link) => link.text && link.href.includes('/retail/') && link.href.includes('placeSlug='))
    .slice(0, 250);
  let latitude = null;
  let longitude = null;
  const resources = performance.getEntriesByType('resource').map((entry) => entry.name).reverse();
  for (const name of resources) {
    try {
      const url = new URL(name);
      const rawLatitude = url.searchParams.get('latitude') || url.searchParams.get('lat');
      const rawLongitude = url.searchParams.get('longitude') || url.searchParams.get('lon');
      if (rawLatitude === null || rawLongitude === null) continue;
      const candidateLatitude = Number(rawLatitude);
      const candidateLongitude = Number(rawLongitude);
      if (Number.isFinite(candidateLatitude) && Number.isFinite(candidateLongitude)) {
        latitude = candidateLatitude;
        longitude = candidateLongitude;
        break;
      }
    } catch {}
  }
  const body = String(document.body?.innerText || '').toLocaleLowerCase('ru');
  const controls = [...document.querySelectorAll('a, button')]
    .filter((element) => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden')
    .map((element) => clean(element.innerText || element.getAttribute('aria-label')).toLocaleLowerCase('ru'));
  return {
    url: location.href,
    links,
    latitude,
    longitude,
    loginRequired: controls.some((label) => ['войти', 'log in', 'sign in'].includes(label)),
    addressRequired: controls.some((label) => [
      'укажите адрес',
      'введите адрес',
      'добавить адрес',
      'enter address',
      'add address',
    ].includes(label)),
    blocked: body.includes('captcha') || body.includes('капч') || body.includes('подтвердите, что вы не робот'),
  };
})()`;

async function waitForPageState(browser, { needLinks = false } = {}) {
  let state = null;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    state = await evaluate(browser, pageStateExpression);
    if (state?.blocked) throw new OperationError("blocked", "Яндекс запросил ручную проверку.");
    if (state?.loginRequired) throw new OperationError("login_required", "Нужно войти в Яндекс Еду.");
    if (state?.addressRequired) throw new OperationError("login_required", "Нужно сохранить адрес доставки в Яндекс Еде.");
    if (Number.isFinite(state?.latitude) && Number.isFinite(state?.longitude) && (!needLinks || state.links?.length)) {
      return state;
    }
    await sleep(500);
  }
  if (needLinks && !state?.links?.length) {
    throw new OperationError("store_unavailable", "Магазины не загрузились для сохранённого адреса.");
  }
  throw new OperationError("location_unavailable", "Яндекс Еда не вернула координаты сохранённого адреса.");
}

function selectStoreLink(store, links) {
  const policy = stores[store];
  for (const link of links || []) {
    const label = normalize(link.text);
    if (!policy.aliases.some((alias) => label.includes(normalize(alias)))) continue;
    try {
      const url = new URL(link.href);
      const placeSlug = url.searchParams.get("placeSlug") || "";
      const pathGroupSlug = url.pathname.split("/").filter(Boolean)[1] || "";
      if (url.protocol !== "https:" || url.hostname !== "eda.yandex.ru" || !url.pathname.startsWith("/retail/") || !slugPattern.test(placeSlug) || url.pathname.includes("/product/") || !policy.groupSlugs.includes(pathGroupSlug)) continue;
      return { url: url.href, placeSlug, pathGroupSlug };
    } catch {}
  }
  throw new OperationError("store_unavailable", "Выбранная сеть недоступна по сохранённому адресу.");
}

// Yandex Food resolves https://eda.yandex.ru/retail/<brand> to the nearest
// storefront of that brand for the saved address by appending placeSlug, and
// bounces unknown or unavailable brands back to the landing page. Classify the
// current URL as a confirmed storefront (object), an explicit brand miss
// (null) or an intermediate redirect state (undefined).
function classifyStorefrontUrl(store, value) {
  const policy = stores[store];
  let url;
  try {
    url = new URL(String(value || ""));
  } catch {
    return undefined;
  }
  if (url.protocol !== "https:" || url.hostname !== "eda.yandex.ru") return undefined;
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts[0] !== "retail") return undefined;
  if (parts.length === 1) {
    return url.searchParams.get("redirectFrom") === "not_found_place" ? null : undefined;
  }
  const pathGroupSlug = parts[1] || "";
  const placeSlug = url.searchParams.get("placeSlug") || "";
  if (
    parts.length !== 2
    || !policy.groupSlugs.includes(pathGroupSlug)
    || !slugPattern.test(placeSlug)
  ) {
    return undefined;
  }
  url.search = "";
  url.searchParams.set("placeSlug", placeSlug);
  return { url: url.href, placeSlug, pathGroupSlug };
}

async function openBrandStorefront(browser, store, groupSlug) {
  await navigate(browser, `https://eda.yandex.ru/retail/${encodeURIComponent(groupSlug)}`);
  let state = null;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    state = await evaluate(browser, pageStateExpression);
    if (state?.blocked) throw new OperationError("blocked", "Яндекс запросил ручную проверку.");
    if (state?.loginRequired) throw new OperationError("login_required", "Нужно войти в Яндекс Еду.");
    if (state?.addressRequired) throw new OperationError("login_required", "Нужно сохранить адрес доставки в Яндекс Еде.");
    const selected = classifyStorefrontUrl(store, state?.url);
    if (selected === null) return null;
    if (selected) return selected;
    await sleep(500);
  }
  return null;
}

// The landing page renders its store carousels lazily while scrolling, so a
// single snapshot of links only covers the first section. Scroll through the
// page before collecting links for the fallback lookup.
const scrollLandingExpression = `(() => {
  window.scrollBy(0, Math.max(800, window.innerHeight || 0));
  return true;
})()`;

const landingGeometryExpression = `(() => {
  const height = document.documentElement.scrollHeight;
  return {
    height,
    atBottom: window.scrollY + window.innerHeight >= height - 2,
  };
})()`;

async function collectLandingLinks(browser) {
  const listingState = await waitForPageState(browser, { needLinks: true });
  const links = [...(listingState.links || [])];
  const seen = new Set(links.map((link) => link.href));
  let previousHeight = -1;
  let stableRounds = 0;
  // Stop only once the viewport has actually reached the bottom and three
  // further rounds neither grew the page nor revealed new links. Geometry is
  // measured after the wait so a lazily loaded section that arrives during
  // the pause is reflected in the same round. The attempt cap is a guard
  // against endlessly growing pages, not the normal exit.
  for (let attempt = 0; attempt < 40 && stableRounds < 3; attempt += 1) {
    await evaluate(browser, scrollLandingExpression);
    await sleep(700);
    const scrolled = await evaluate(browser, landingGeometryExpression);
    const height = Number(scrolled?.height);
    const state = await evaluate(browser, pageStateExpression);
    if (state?.blocked) throw new OperationError("blocked", "Яндекс запросил ручную проверку.");
    if (state?.loginRequired) throw new OperationError("login_required", "Нужно войти в Яндекс Еду.");
    let added = 0;
    for (const link of state?.links || []) {
      if (!seen.has(link.href)) {
        seen.add(link.href);
        links.push(link);
        added += 1;
      }
    }
    stableRounds = scrolled?.atBottom && height === previousHeight && added === 0
      ? stableRounds + 1
      : 0;
    previousHeight = height;
  }
  return { ...listingState, links };
}

function catalogExpression(input) {
  return `(async () => {
    const input = ${JSON.stringify(input)};
    const query = new URLSearchParams({latitude: String(input.latitude), longitude: String(input.longitude), shippingType: 'delivery'});
    const response = await fetch('/api/v2/catalog/' + encodeURIComponent(input.placeSlug) + '?' + query);
    let data = {};
    try { data = await response.json(); } catch {}
    const place = data?.payload?.foundPlace?.place || null;
    return {status: response.status, place: place ? {slug: place.slug, name: place.name, business: place.business, brandSlug: place.brand?.slug || ''} : null};
  })()`;
}

function classifyApiStatus(status, message) {
  if (status === 401 || status === 403) throw new OperationError("login_required", "Нужно войти в Яндекс Еду.");
  if (status === 429) throw new OperationError("blocked", "Яндекс временно ограничил запросы.");
  if (status < 200 || status >= 300) throw new OperationError("upstream_failed", message);
}

async function resolveStore(browser, store) {
  if (stores[store].unavailableSummary) {
    throw new OperationError("store_unavailable", stores[store].unavailableSummary);
  }
  let selected = null;
  let fallbackLocation = null;
  for (const groupSlug of stores[store].groupSlugs) {
    selected = await openBrandStorefront(browser, store, groupSlug);
    if (selected) break;
  }
  if (!selected) {
    await navigate(browser, "https://eda.yandex.ru/retail");
    const listingState = await collectLandingLinks(browser);
    fallbackLocation = { latitude: listingState.latitude, longitude: listingState.longitude };
    selected = selectStoreLink(store, listingState.links);
    await navigate(browser, selected.url);
  }
  const state = await waitForPageState(browser);
  const location = {
    latitude: state.latitude ?? fallbackLocation?.latitude,
    longitude: state.longitude ?? fallbackLocation?.longitude,
  };
  const catalog = await evaluate(browser, catalogExpression({ ...location, placeSlug: selected.placeSlug }));
  classifyApiStatus(Number(catalog?.status || 0), "Каталог магазина недоступен.");
  if (!catalog?.place || catalog.place.slug !== selected.placeSlug) {
    throw new OperationError("store_unavailable", "Выбранный магазин не подтвердил свою витрину.");
  }
  const groupSlug = String(catalog.place.brandSlug || selected.pathGroupSlug || "");
  const placeBusiness = String(catalog.place.business || "");
  if (!stores[store].groupSlugs.includes(groupSlug) || !businessPattern.test(placeBusiness)) {
    throw new OperationError("store_unavailable", "Витрина магазина не имеет стабильного идентификатора.");
  }
  return {
    place_slug: selected.placeSlug,
    place_business: placeBusiness,
    group_slug: groupSlug,
    latitude: location.latitude,
    longitude: location.longitude,
    store_url: selected.url,
  };
}

function validatedContext(value) {
  const context = {
    place_slug: text(value?.place_slug, 128),
    place_business: text(value?.place_business, 64),
    group_slug: text(value?.group_slug, 128),
    latitude: Number(value?.latitude),
    longitude: Number(value?.longitude),
    store_url: text(value?.store_url, 2048),
  };
  let url;
  try { url = new URL(context.store_url); } catch {}
  if (
    !slugPattern.test(context.place_slug)
    || !businessPattern.test(context.place_business)
    || !slugPattern.test(context.group_slug)
    || !Number.isFinite(context.latitude)
    || context.latitude < -90
    || context.latitude > 90
    || !Number.isFinite(context.longitude)
    || context.longitude < -180
    || context.longitude > 180
    || url?.protocol !== "https:"
    || url?.hostname !== "eda.yandex.ru"
    || url.searchParams.get("placeSlug") !== context.place_slug
    || !url.pathname.startsWith("/retail/")
    || url.pathname.includes("/product/")
  ) {
    throw new OperationError("invalid_selection", "Контекст поиска недействителен.");
  }
  return context;
}

function sameLocation(left, right) {
  const latitude = Number(left?.latitude);
  const longitude = Number(left?.longitude);
  return Number.isFinite(latitude)
    && Number.isFinite(longitude)
    && Math.abs(latitude - right.latitude) <= 0.00001
    && Math.abs(longitude - right.longitude) <= 0.00001;
}

async function validateSignedStore(browser, context) {
  let state = null;
  let ready = false;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    state = await evaluate(browser, pageStateExpression);
    if (state?.blocked) throw new OperationError("blocked", "Яндекс запросил ручную проверку.");
    if (state?.loginRequired) throw new OperationError("login_required", "Нужно войти в Яндекс Еду.");
    if (state?.addressRequired) throw new OperationError("login_required", "Нужно сохранить адрес доставки в Яндекс Еде.");
    try {
      const current = new URL(state?.url || "");
      if (
        current.hostname === "eda.yandex.ru"
        && current.searchParams.get("placeSlug") === context.place_slug
        && Number.isFinite(state?.latitude)
        && Number.isFinite(state?.longitude)
      ) {
        ready = true;
        break;
      }
    } catch {}
    await sleep(300);
  }
  if (!ready) throw new OperationError("store_unavailable", "Витрина магазина не загрузилась.");
  if (!sameLocation(state, context)) {
    throw new OperationError(
      "invalid_selection",
      "Адрес доставки изменился после поиска; соберите корзину заново.",
    );
  }
  const currentContext = {
    ...context,
    latitude: Number(state.latitude),
    longitude: Number(state.longitude),
  };
  const catalog = await evaluate(browser, catalogExpression({
    latitude: currentContext.latitude,
    longitude: currentContext.longitude,
    placeSlug: currentContext.place_slug,
  }));
  classifyApiStatus(Number(catalog?.status || 0), "Каталог магазина недоступен.");
  const groupSlug = String(catalog?.place?.brandSlug || new URL(context.store_url).pathname.split("/").filter(Boolean)[1] || "");
  if (
    catalog?.place?.slug !== context.place_slug
    || catalog?.place?.business !== context.place_business
    || groupSlug !== context.group_slug
  ) {
    throw new OperationError("invalid_selection", "Витрина магазина изменилась после поиска.");
  }
  return currentContext;
}

function cartParams(context) {
  return {
    longitude: String(context.longitude),
    latitude: String(context.latitude),
    screen: "menu",
    shippingType: "delivery",
    autoTranslate: "false",
    plus_subscription_toggle_state: "false",
    combo_subscription_toggle_state: "false",
    placeSlug: context.place_slug,
  };
}

function legacyCartParams(context) {
  return {
    soft_multi: "true",
    longitude: String(context.longitude),
    latitude: String(context.latitude),
    screen: "menu",
    shippingType: "delivery",
    autoTranslate: "false",
    plus_subscription_toggle_state: "false",
    combo_subscription_toggle_state: "false",
    is_delivery_without_address: "false",
  };
}

function searchExpression(context, ingredients) {
  return `(async () => {
    const context = ${JSON.stringify(context)};
    const ingredients = ${JSON.stringify(ingredients)};
    let cursor = 0;
    const results = new Array(ingredients.length);
    const worker = async () => {
      while (true) {
        const index = cursor++;
        if (index >= ingredients.length) return;
        const ingredient = ingredients[index];
        const queries = Array.isArray(ingredient.queries) && ingredient.queries.length
          ? ingredient.queries
          : [ingredient.query];
        const candidates = [];
        const seen = new Set();
        let status = 0;
        for (const query of queries) {
          const response = await fetch('/api/v1/menu/search', {
            method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({place_slug: context.place_slug, text: query, location: {lat: context.latitude, lon: context.longitude}}),
          });
          if (response.status < 200 || response.status >= 300) {
            // A failed broader fallback must not discard candidates that a
            // more precise query already returned. Only the first query
            // determines the reported status when nothing was found yet.
            if (!candidates.length) status = response.status;
            break;
          }
          status = response.status;
          let data = {};
          try { data = await response.json(); } catch {}
          const products = (data.blocks || []).flatMap((block) => Array.isArray(block?.payload?.products) ? block.payload.products : []);
          for (const product of products) {
            if (candidates.length >= ${maximumCandidatesPerIngredient}) break;
            const productId = String(product.public_id || product.publicId || product.uid || product.sku_id || product.skuId || '');
            if (!productId || seen.has(productId)) continue;
            seen.add(productId);
            candidates.push({
              product_id: productId,
              sku_id: String(product.sku_id || product.skuId || product.public_id || product.publicId || product.uid || ''),
              name: String(product.name || '').slice(0, 300),
              weight: String(product.weight || '').slice(0, 80),
              available: product.available !== false,
              in_stock: product.inStock ?? product.in_stock ?? null,
              price: product.decimalPromoPrice ?? product.decimalPrice ?? product.promoPrice ?? product.price ?? null,
              query,
            });
          }
          if (candidates.length >= ${sufficientCandidates}) break;
        }
        results[index] = { index, status, candidates };
      }
    };
    await Promise.all(Array.from({length: Math.min(4, ingredients.length)}, worker));
    return results;
  })()`;
}

function cartExpression(context) {
  return `(async () => {
    const context = ${JSON.stringify(context)};
    const query = new URLSearchParams(${JSON.stringify(cartParams(context))});
    const response = await fetch('/eats/v1/cart/v2/full-carts?' + query, {method: 'POST', headers: {'content-type': 'application/json'}, body: '{}'});
    let data = {};
    try { data = await response.json(); } catch {}
    const describe = (source, value) => {
      if (!value || typeof value !== 'object') return null;
      const cart = value.cart && typeof value.cart === 'object' ? value.cart : value;
      const place = value.place || cart.place || {};
      const placeSlug = String(
        value.place_slug ?? value.placeSlug ?? cart.place_slug ?? cart.placeSlug ?? place.slug ?? ''
      );
      const placeBusiness = String(
        value.place_business ?? value.placeBusiness ?? cart.place_business ?? cart.placeBusiness ?? place.business ?? ''
      );
      return {
        source,
        place_slug: placeSlug,
        place_business: placeBusiness,
        cart_id: String(cart.id ?? value.id ?? ''),
        items: Array.isArray(cart.items) ? cart.items : [],
      };
    };
    const candidates = [
      describe('cart', data?.cart),
      describe('place_cart', data?.place_cart),
      describe('placeCart', data?.placeCart),
      ...(Array.isArray(data?.cart_places_list)
        ? data.cart_places_list.map((entry) => describe('cart_places_list', entry))
        : []),
    ].filter(Boolean);
    const matching = candidates.filter((candidate) => (
      candidate.place_slug === context.place_slug
      && (!candidate.place_business || candidate.place_business === context.place_business)
    ));
    // Yandex represents an empty cart for the requested place as one unscoped
    // empty cart. cart_places_list may still enumerate other places the user
    // recently opened (summary rows without items). None of those rows can
    // belong to the requested place because matching is empty, so the
    // requested store cart is safely empty when the unscoped cart has no
    // rows and no identifier.
    const unscopedEmpty = matching.length === 0
      && candidates.length >= 1
      && candidates[0].source === 'cart'
      && !candidates[0].place_slug
      && !candidates[0].place_business
      && !candidates[0].cart_id
      && candidates[0].items.length === 0
      && candidates.slice(1).every((candidate) => (
        candidate.source === 'cart_places_list'
        && candidate.place_slug
        && candidate.place_slug !== context.place_slug
        && candidate.items.length === 0
      ))
      && Array.isArray(data?.cart_places_list);
    const selected = matching[0] || (unscopedEmpty ? candidates[0] : null);
    const conflicting = selected && matching.some((candidate) => (
      candidate !== selected
      && candidate.cart_id
      && selected.cart_id
      && candidate.cart_id !== selected.cart_id
    ));
    const items = selected?.items || [];
    const identifiers = (item) => {
      const keys = ['item_uid', 'itemUid', 'public_id', 'publicId', 'sku_id', 'skuId', 'uid'];
      const values = [];
      for (const object of [
        item,
        item?.item,
        item?.product,
        item?.data,
        item?.place_menu_item,
        item?.placeMenuItem,
      ]) {
        if (!object || typeof object !== 'object') continue;
        for (const key of keys) if (object[key] != null) values.push(String(object[key]));
      }
      if (!values.length && item?.id != null) values.push(String(item.id));
      return [...new Set(values)];
    };
    return {
      status: response.status,
      matched: Boolean(selected) && !conflicting,
      diagnostic: Boolean(selected) && !conflicting ? null : {
        response_keys: Object.keys(data || {}).slice(0, 30),
        candidates: candidates.map((candidate) => ({
          source: candidate.source,
          place_slug: candidate.place_slug,
          place_business: candidate.place_business,
          has_cart_id: Boolean(candidate.cart_id),
          items_count: candidate.items.length,
        })),
      },
      items: items.map((item) => ({
        ids: identifiers(item),
        cart_item_id: String(item.id ?? item.cart_item_id ?? item.cartItemId ?? ''),
        quantity: Number(item.quantity ?? item.count ?? item.amount ?? item.item?.quantity ?? 0),
      })).filter((item) => item.ids.length && Number.isFinite(item.quantity) && item.quantity >= 0),
    };
  })()`;
}

function addLegacyItemsExpression(context, items) {
  const payloadItems = items.map((item) => ({
    item_id: item.product_id,
    quantity: item.delta,
  }));
  return `(async () => {
    const context = ${JSON.stringify(context)};
    const items = ${JSON.stringify(payloadItems)};
    const query = new URLSearchParams(${JSON.stringify(legacyCartParams(context))});
    for (const item of items) {
      const response = await fetch('/api/v1/cart?' + query, {
        method: 'POST',
        headers: {'accept': 'application/json', 'content-type': 'application/json'},
        body: JSON.stringify({
          item_id: item.item_id,
          quantity: item.quantity,
          place_slug: context.place_slug,
          place_business: context.place_business,
        }),
      });
      let data = {};
      try { data = await response.json(); } catch {}
      const errorMessage = [
        data?.message,
        data?.description,
        data?.err?.message,
        data?.err?.description,
        typeof data?.err === 'string' ? data.err : '',
        data?.error?.message,
        data?.error?.description,
        typeof data?.error === 'string' ? data.error : '',
      ].find((value) => typeof value === 'string' && value.trim());
      const result = {
        status: response.status,
        error_code: String(data?.code || data?.error?.code || '').slice(0, 80),
        error_message: String(errorMessage || '').slice(0, 240),
        response_keys: Object.keys(data || {}).filter((key) => /^[a-zA-Z0-9_-]{1,40}$/.test(key)).slice(0, 20),
      };
      if (result.status < 200 || result.status >= 300) return result;
    }
    return {status: 200, error_code: '', error_message: '', response_keys: []};
  })()`;
}

function quantityInCart(cart, ...identifiers) {
  const expected = new Set(identifiers.map(String));
  return (cart?.items || []).reduce((total, item) => (
    item.ids?.some((identifier) => expected.has(String(identifier))) ? total + Number(item.quantity || 0) : total
  ), 0);
}

async function dispatchCartMutation(browser, expression, mutationState) {
  // Once the request is dispatched, no HTTP status or transport error proves
  // that Yandex applied none of it. Never retry the
  // mutation or allow a Hermes fallback after crossing this boundary.
  mutationState.possible = true;
  return evaluate(browser, expression);
}

async function readCart(browser, context) {
  const cart = await evaluate(browser, cartExpression(context));
  classifyApiStatus(Number(cart?.status || 0), "Корзина Яндекс Еды недоступна.");
  if (!cart?.matched) {
    console.warn("Yandex cart did not match the selected store", cart?.diagnostic || {});
    throw new OperationError(
      "verification_failed",
      "Корзину выбранного магазина нельзя однозначно определить.",
    );
  }
  return cart;
}

async function readCartUntil(browser, context, predicate) {
  let cart = null;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    cart = await readCart(browser, context);
    if (predicate(cart)) return cart;
    await sleep(300);
  }
  return cart;
}

function sealSelection(payload) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", selectionKey, iv);
  cipher.setAAD(Buffer.from("recipes-cart-selection-v1"));
  const encrypted = Buffer.concat([
    cipher.update(JSON.stringify(payload), "utf8"),
    cipher.final(),
  ]);
  return [
    iv.toString("base64url"),
    encrypted.toString("base64url"),
    cipher.getAuthTag().toString("base64url"),
  ].join(".");
}

function openSelection(token) {
  const value = text(token, 60_000);
  const [rawIv, rawEncrypted, rawTag, extra] = value.split(".");
  if (!rawIv || !rawEncrypted || !rawTag || extra) throw new OperationError("invalid_selection", "Результат поиска недействителен.");
  let payload;
  try {
    const decipher = crypto.createDecipheriv(
      "aes-256-gcm",
      selectionKey,
      Buffer.from(rawIv, "base64url"),
    );
    decipher.setAAD(Buffer.from("recipes-cart-selection-v1"));
    decipher.setAuthTag(Buffer.from(rawTag, "base64url"));
    payload = JSON.parse(Buffer.concat([
      decipher.update(Buffer.from(rawEncrypted, "base64url")),
      decipher.final(),
    ]).toString("utf8"));
  } catch {}
  if (!payload || !Number.isFinite(payload.expires_at) || payload.expires_at < Date.now()) {
    throw new OperationError("invalid_selection", "Срок действия поиска истёк; запустите сборку ещё раз.");
  }
  return payload;
}

function sealCleanup(payload) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", selectionKey, iv);
  cipher.setAAD(Buffer.from("recipes-cart-cleanup-v1"));
  const encrypted = Buffer.concat([
    cipher.update(JSON.stringify(payload), "utf8"),
    cipher.final(),
  ]);
  return [
    iv.toString("base64url"),
    encrypted.toString("base64url"),
    cipher.getAuthTag().toString("base64url"),
  ].join(".");
}

function openCleanup(token) {
  const value = text(token, 60_000);
  const [rawIv, rawEncrypted, rawTag, extra] = value.split(".");
  if (!rawIv || !rawEncrypted || !rawTag || extra) {
    throw new OperationError("invalid_cleanup", "Журнал очистки недействителен.");
  }
  let payload;
  try {
    const decipher = crypto.createDecipheriv(
      "aes-256-gcm",
      selectionKey,
      Buffer.from(rawIv, "base64url"),
    );
    decipher.setAAD(Buffer.from("recipes-cart-cleanup-v1"));
    decipher.setAuthTag(Buffer.from(rawTag, "base64url"));
    payload = JSON.parse(Buffer.concat([
      decipher.update(Buffer.from(rawEncrypted, "base64url")),
      decipher.final(),
    ]).toString("utf8"));
  } catch {}
  if (!payload || !Number.isFinite(payload.expires_at) || payload.expires_at < Date.now()) {
    throw new OperationError(
      "invalid_cleanup",
      "Срок действия журнала очистки истёк; проверьте корзину вручную.",
    );
  }
  return payload;
}

function validateIngredients(body) {
  if (!Array.isArray(body.ingredients) || body.ingredients.length < 1 || body.ingredients.length > 24) {
    throw new OperationError("invalid_request", "В одной сборке допустимо от 1 до 24 ингредиентов.");
  }
  return body.ingredients.map((ingredient) => {
    const name = text(ingredient?.name, 180);
    const query = text(ingredient?.search_query || ingredient?.name, 180);
    if (!name || !query) throw new OperationError("invalid_request", "Ингредиент не имеет безопасного поискового запроса.");
    return { name, query, queries: searchQueries(query, name) };
  });
}

const queryStopWords = new Set([
  "в", "для", "и", "из", "на", "по", "с", "со", "без", "или", "к", "от", "у",
  "свежий", "свежая", "свежие", "свежее",
]);
const maximumCandidatesPerIngredient = 12;
const sufficientCandidates = 6;

// Some storefront search engines (Перекрёсток among them) require every
// query word to match and answer "ничего не найдено" for descriptive queries
// such as "целая курица". Fall back from the full query to the ingredient
// name and then to its individual meaningful words. Later, broader queries
// only fill remaining candidate slots, so the matcher still ranks results
// from the precise query first.
function searchQueries(query, name) {
  const queries = [];
  const seen = new Set();
  const push = (value) => {
    const candidate = String(value || "").trim().slice(0, 180);
    const key = normalize(candidate);
    if (!key || seen.has(key)) return;
    seen.add(key);
    queries.push(candidate);
  };
  push(query);
  push(name);
  for (const source of [query, name]) {
    const words = normalize(source).split(" ").filter((word) => (
      word.length >= 3 && !queryStopWords.has(word) && !/^[0-9.,]+$/.test(word)
    ));
    if (words.length < 2) continue;
    for (const word of words) push(word);
  }
  return queries.slice(0, 6);
}

async function search(body) {
  const { scope, store } = validateBaseRequest(body);
  const operationId = text(body?.operation_id, 128);
  if (!operationIdPattern.test(operationId)) {
    throw new OperationError("invalid_request", "Неверный идентификатор операции.");
  }
  const ingredients = validateIngredients(body);
  const started = Date.now();
  return withBrowser(scope, async (browser) => {
    const context = await resolveStore(browser, store);
    // Reading the cart here validates the authenticated cookie/session before
    // returning a selection token. It never changes existing items.
    await readCart(browser, context);
    const rawResults = await evaluate(browser, searchExpression(context, ingredients), 90_000);
    if (!Array.isArray(rawResults) || rawResults.length !== ingredients.length) {
      throw new OperationError("upstream_failed", "Поиск товаров вернул неполный ответ.");
    }
    const allowed = Object.create(null);
    const results = rawResults.map((result, index) => {
      classifyApiStatus(Number(result?.status || 0), "Поиск товаров недоступен.");
      const candidates = [];
      for (const candidate of result.candidates || []) {
        const productId = text(candidate.product_id, 128);
        const skuId = text(candidate.sku_id || candidate.product_id, 128);
        const name = text(candidate.name, 300);
        if (!productIdPattern.test(productId) || !productIdPattern.test(skuId) || !name) continue;
        allowed[productId] = skuId;
        candidates.push({
          ...candidate,
          product_id: productId,
          sku_id: skuId,
          name,
          product_url: `https://eda.yandex.ru/retail/${encodeURIComponent(context.group_slug)}/product/${encodeURIComponent(productId)}?placeSlug=${encodeURIComponent(context.place_slug)}`,
        });
      }
      return { index, candidates };
    });
    const selectionToken = sealSelection({
      scope,
      store,
      operation_id: operationId,
      context,
      allowed,
      expires_at: Date.now() + tokenLifetimeMs,
    });
    return {
      status: "ready",
      cart_url: context.store_url,
      selection_token: selectionToken,
      results,
      elapsed_ms: Date.now() - started,
    };
  });
}

async function cartState(body) {
  const { scope, store } = validateBaseRequest(body);
  const started = Date.now();
  return withBrowser(scope, async (browser) => {
    const context = await resolveStore(browser, store);
    const cart = await readCart(browser, context);
    return {
      status: "ready",
      cart_url: context.store_url,
      items: (cart.items || []).map((item) => ({
        product_ids: (item.ids || []).filter((value) => productIdPattern.test(String(value))).slice(0, 8),
        quantity: Number(item.quantity || 0),
      })).filter((item) => item.product_ids.length && Number.isFinite(item.quantity)),
      elapsed_ms: Date.now() - started,
    };
  });
}

function validateApplyItems(body, selection) {
  if (!Array.isArray(body.items) || body.items.length < 1 || body.items.length > 24) {
    throw new OperationError("invalid_request", "Неверный список товаров для корзины.");
  }
  const grouped = new Map();
  for (const item of body.items) {
    const productId = text(item?.product_id, 128);
    const skuId = text(item?.sku_id || productId, 128);
    const quantity = Number(item?.package_count);
    if (
      !productIdPattern.test(productId)
      || !selection.allowed
      || !Object.hasOwn(selection.allowed, productId)
      || selection.allowed[productId] !== skuId
      || !Number.isInteger(quantity)
      || quantity < 1
      || quantity > 100
    ) {
      throw new OperationError("invalid_selection", "Выбран товар, которого не было в подписанном поиске.");
    }
    const current = grouped.get(productId) || { product_id: productId, sku_id: skuId, quantity: 0 };
    current.quantity += quantity;
    if (current.quantity > 100) throw new OperationError("invalid_request", "Требуемое число упаковок слишком велико.");
    grouped.set(productId, current);
  }
  return [...grouped.values()];
}

async function applySelection(body) {
  const { scope, store } = validateBaseRequest(body);
  const operationId = text(body?.operation_id, 128);
  const selection = openSelection(body.selection_token);
  if (
    !operationIdPattern.test(operationId)
    || selection.operation_id !== operationId
    || selection.scope !== scope
    || selection.store !== store
  ) {
    throw new OperationError("invalid_selection", "Поиск относится к другому пользователю или магазину.");
  }
  const signedContext = validatedContext(selection.context);
  const requested = validateApplyItems(body, selection);
  const recordKey = `${scope}:${operationId}`;
  const fingerprint = operationFingerprint(scope, store, requested);
  const previous = await readOperationRecord(recordKey);
  if (previous) {
    if (
      typeof previous !== "object"
      || previous.fingerprint !== fingerprint
      || !["started", "completed"].includes(previous.status)
    ) {
      throw new OperationError(
        "operation_conflict",
        "Повтор операции не совпадает с сохранённым журналом; проверьте корзину вручную.",
        { mutationPossible: true },
      );
    }
    if (previous.status === "completed") {
      if (!previous.result || previous.result.status !== "applied") {
        throw new OperationError(
          "operation_state_unavailable",
          "Сохранённый результат операции повреждён; проверьте корзину вручную.",
          { mutationPossible: true },
        );
      }
      return previous.result;
    }
    throw new OperationError(
      "operation_in_progress",
      "Предыдущий запуск мог изменить корзину; проверьте её вручную.",
      { mutationPossible: true },
    );
  }
  const started = Date.now();
  const mutationState = { possible: false };
  const result = await withBrowser(scope, async (browser) => {
    const context = await validateSignedStore(browser, signedContext);
    const before = await readCart(browser, context);
    const quantitiesBefore = new Map();
    const quantitiesExpected = new Map();
    const itemsToAdd = [];
    for (const item of requested) {
      const existing = quantityInCart(before, item.product_id, item.sku_id);
      const expected = existing + item.quantity;
      if (
        !Number.isInteger(existing)
        || existing < 0
        || !Number.isInteger(expected)
        || expected > 100
      ) {
        throw new OperationError(
          "quantity_limit",
          "Количество товара в корзине не позволяет безопасно добавить рецепт.",
        );
      }
      quantitiesBefore.set(item.product_id, existing);
      quantitiesExpected.set(item.product_id, expected);
      // POST applies a positive delta. Never write an absolute
      // quantity here: a user may increase this SKU after the preceding read.
      itemsToAdd.push({ ...item, delta: item.quantity });
    }

    const runMutation = async (mutationExpression) => {
      const changed = await dispatchCartMutation(
        browser,
        mutationExpression,
        mutationState,
      );
      const changedStatus = Number(changed?.status || 0);
      const errorCode = text(changed?.error_code, 80);
      const errorMessage = text(changed?.error_message, 240);
      if (changedStatus < 200 || changedStatus >= 300) {
        console.warn("Yandex legacy cart mutation rejected", {
          status: changedStatus,
          code: errorCode,
          message: errorMessage,
          response_keys: Array.isArray(changed?.response_keys) ? changed.response_keys : [],
        });
      }
      classifyApiStatus(
        changedStatus,
        `Яндекс Еда не смогла изменить товар в корзине${errorCode ? ` (${errorCode})` : ""}${errorMessage ? `: ${errorMessage}` : ""}.`,
      );
    };

    // Persist intent before crossing the mutation boundary. If the process or
    // response is lost afterwards, a retry fails closed instead of adding the
    // same recipe twice.
    await storeOperationRecord(recordKey, {
      status: "started",
      fingerprint,
      started_at: new Date().toISOString(),
    });
    await runMutation(addLegacyItemsExpression(context, itemsToAdd));
    const after = await readCartUntil(browser, context, (cart) => requested.every((item) => {
      const expectedQuantity = quantitiesExpected.get(item.product_id) || 0;
      return quantityInCart(cart, item.product_id, item.sku_id) === expectedQuantity;
    }));
    const additions = [];
    for (const item of requested) {
      const beforeQuantity = quantitiesBefore.get(item.product_id) || 0;
      const afterQuantity = quantityInCart(after, item.product_id, item.sku_id);
      const expectedQuantity = quantitiesExpected.get(item.product_id) || 0;
      if (afterQuantity !== expectedQuantity) {
        throw new OperationError("verification_failed", "Количество товара в корзине не удалось подтвердить.", { mutationPossible: mutationState.possible });
      }
      additions.push({
        product_id: item.product_id,
        sku_id: item.sku_id,
        before_quantity: beforeQuantity,
        after_quantity: afterQuantity,
        added_quantity: Math.max(0, afterQuantity - beforeQuantity),
      });
    }
    const cleanupItems = additions.filter((item) => item.added_quantity > 0);
    const cleanupToken = cleanupItems.length ? sealCleanup({
      scope,
      store,
      context,
      operation_id: operationId,
      items: cleanupItems.map((item) => ({
        product_id: item.product_id,
        sku_id: item.sku_id,
        before_quantity: item.before_quantity,
        after_quantity: item.after_quantity,
      })),
      expires_at: Date.now() + cleanupTokenLifetimeMs,
    }) : "";
    return {
      status: "applied",
      cart_url: context.store_url,
      additions,
      cleanup_token: cleanupToken,
      elapsed_ms: Date.now() - started,
    };
  }, mutationState, signedContext.store_url);
  // A completed record is written only after withBrowser confirms that the
  // persistent profile was closed. A replay can then return this exact result
  // without touching Yandex or opening the profile again.
  await storeOperationRecord(recordKey, {
    status: "completed",
    fingerprint,
    completed_at: new Date().toISOString(),
    result,
  });
  return result;
}

function validateCleanup(body, scope, store) {
  const cleanup = openCleanup(body.cleanup_token);
  if (cleanup.scope !== scope || cleanup.store !== store) {
    throw new OperationError(
      "invalid_cleanup",
      "Журнал очистки относится к другому пользователю или магазину.",
    );
  }
  const context = validatedContext(cleanup.context);
  if (!cleanupOperationIdPattern.test(String(cleanup.operation_id || ""))) {
    throw new OperationError("invalid_cleanup", "Журнал очистки не содержит операцию.");
  }
  if (!Array.isArray(cleanup.items) || cleanup.items.length < 1 || cleanup.items.length > 24) {
    throw new OperationError("invalid_cleanup", "Неверный журнал очистки.");
  }
  const seen = new Set();
  const items = [];
  for (const item of cleanup.items) {
    const productId = text(item?.product_id, 128);
    const skuId = text(item?.sku_id, 128);
    const beforeQuantity = Number(item?.before_quantity);
    const afterQuantity = Number(item?.after_quantity);
    if (
      !productIdPattern.test(productId)
      || !productIdPattern.test(skuId)
      || seen.has(productId)
      || !Number.isInteger(beforeQuantity)
      || beforeQuantity < 0
      || beforeQuantity > 100
      || !Number.isInteger(afterQuantity)
      || afterQuantity <= beforeQuantity
      || afterQuantity > 100
    ) {
      throw new OperationError("invalid_cleanup", "Журнал очистки не содержит точный товар.");
    }
    seen.add(productId);
    items.push({
      product_id: productId,
      sku_id: skuId,
      before_quantity: beforeQuantity,
      after_quantity: afterQuantity,
    });
  }
  return { context, items };
}

async function cleanup(body) {
  const { scope, store } = validateBaseRequest(body);
  const cleanupRequest = validateCleanup(body, scope, store);
  return withBrowser(scope, async (browser) => {
    const context = await validateSignedStore(browser, cleanupRequest.context);
    const cart = await readCart(browser, context);
    let outstanding = 0;
    for (const item of cleanupRequest.items) {
      const existing = quantityInCart(cart, item.product_id, item.sku_id);
      if (existing > item.before_quantity) outstanding += 1;
    }
    if (outstanding === 0) {
      return { status: "cleared", summary: "Добавления этой сборки уже удалены из корзины." };
    }
    // Yandex exposes no conditional/versioned decrement. An automatic PUT or
    // DELETE could overwrite a change made by the user after this read, so the
    // adapter deliberately performs no downward mutation.
    return {
      status: "login_required",
      summary: `Удалите вручную добавления этой сборки (${outstanding} поз.).`,
      mutation_possible: false,
    };
  }, { possible: false }, cleanupRequest.context.store_url);
}

function errorBody(error) {
  const known = error instanceof OperationError;
  const code = known ? error.code : "internal_error";
  // withBrowser also annotates ordinary Error instances raised after the
  // mutation boundary. Never discard that uncertainty during serialization.
  const mutationPossible = Boolean(error?.mutationPossible);
  if (code === "login_required") return { status: "login_required", summary: error.message, mutation_possible: mutationPossible };
  if (code === "blocked") return { status: "blocked", summary: error.message, mutation_possible: mutationPossible };
  if (code === "store_unavailable") return { status: "incomplete", summary: error.message, mutation_possible: mutationPossible };
  return {
    status: "failed",
    summary: known ? error.message : "Адаптер корзины завершился с ошибкой.",
    error: code,
    mutation_possible: mutationPossible,
  };
}

const handleRequest = async (request, response) => {
  const url = new URL(request.url, "http://cart-adapter.local");
  if (request.method === "GET" && url.pathname === "/healthz") {
    return sendJson(response, 200, { status: "ok" });
  }
  if (!isAuthorized(request)) return sendJson(response, 401, { error: "unauthorized" });
  if (request.method !== "POST" || !["/v1/search", "/v1/cart-state", "/v1/apply", "/v1/cleanup"].includes(url.pathname)) {
    return sendJson(response, 404, { error: "not_found" });
  }
  try {
    const body = await readJson(request);
    const operation = url.pathname === "/v1/search" ? () => search(body)
      : url.pathname === "/v1/cart-state" ? () => cartState(body)
      : url.pathname === "/v1/apply" ? () => applySelection(body)
        : () => cleanup(body);
    return sendJson(
      response,
      200,
      await runExclusiveOperation(
        text(body?.scope, 80),
        () => runWithOperationDeadline(operation),
      ),
    );
  } catch (error) {
    const body = errorBody(error);
    const status = error instanceof OperationError && error.code === "invalid_request" ? 400 : 200;
    if (error instanceof OperationError) {
      console.warn("Cart adapter operation rejected", {
        path: url.pathname,
        code: error.code,
        mutation_possible: Boolean(error.mutationPossible),
      });
    } else {
      console.error("Cart adapter operation failed", error);
    }
    return sendJson(response, status, body);
  }
};

const server = tlsEnabled
  ? https.createServer(
    {
      cert: readFileSync(tlsCertPath),
      key: readFileSync(tlsKeyPath),
      minVersion: "TLSv1.2",
    },
    handleRequest,
  )
  : http.createServer(handleRequest);

server.requestTimeout = 180_000;
server.headersTimeout = 10_000;
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  server.listen(port, bindHost, () => {
    console.log(`Recipes cart adapter listening on ${bindHost}:${port}`);
  });
}

export {
  classifyStorefrontUrl,
  searchQueries,
  errorBody,
  boundedOperationTimeout,
  deferScopeRecovery,
  durableWriteState,
  finalBrowserError,
  isScopeQuarantined,
  markProfileUncertain,
  OperationError,
  operationFingerprint,
  pruneOperationRecords,
  preserveMutationUncertainty,
  quarantineScope,
  readOperationRecord,
  recoverQuarantinedScope,
  releaseScopeQuarantine,
  runExclusiveOperation,
  runWithOperationDeadline,
  sameLocation,
  selectStoreLink,
  scopeRecoveryAt,
  storeOperationRecord,
};
