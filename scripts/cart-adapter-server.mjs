import crypto from "node:crypto";
import http from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";


const execFileAsync = promisify(execFile);
const bindHost = process.env.CART_ADAPTER_BIND_HOST || "127.0.0.1";
const port = Number(process.env.CART_ADAPTER_PORT || "9381");
const controlKey = process.env.CART_ADAPTER_CONTROL_KEY || "";
const hermesRoot = process.env.HERMES_ROOT || "";
const hermesHome = process.env.HERMES_HOME || "";
const hermesPython = `${hermesRoot}/venv/bin/python`;
const camofoxUrl = (process.env.CAMOFOX_URL || "http://127.0.0.1:9377").replace(/\/$/, "");
const camofoxAccessKey = process.env.CAMOFOX_ACCESS_KEY || process.env.CAMOFOX_API_KEY || "";
const scopePattern = /^recipes-cart-user-[1-9][0-9]*$/;
const productIdPattern = /^[A-Za-z0-9_-]{8,128}$/;
const cartItemIdPattern = /^[A-Za-z0-9_-]{1,128}$/;
const businessPattern = /^[a-z][a-z0-9_-]{0,63}$/;
const slugPattern = /^[A-Za-z0-9_-]{1,128}$/;
const tokenLifetimeMs = 10 * 60 * 1000;
const selectionKey = crypto.createHash("sha256").update(controlKey).digest();
const stores = {
  auchan: ["ашан", "auchan"],
  perekrestok: ["перекресток", "perekrestok"],
  pyaterochka: ["пятерочка", "pyaterochka"],
  magnit: ["магнит", "magnit"],
  lavka: ["яндекс лавка", "лавка", "yandex lavka"],
};

if (!controlKey || !hermesRoot || !hermesHome || !Number.isInteger(port)) {
  throw new Error("Cart adapter service environment is incomplete");
}

class OperationError extends Error {
  constructor(code, message, { mutationPossible = false } = {}) {
    super(message);
    this.code = code;
    this.mutationPossible = mutationPossible;
  }
}

let operationQueue = Promise.resolve();

function serializeOperation(operation) {
  const result = operationQueue.then(operation, operation);
  operationQueue = result.catch(() => {});
  return result;
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
      signal: AbortSignal.timeout(options.timeout || 30_000),
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
      timeout: 10_000,
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
  const opened = await camofoxRequest("/tabs", {
    method: "POST",
    body: JSON.stringify({
      userId: identity.user_id,
      sessionKey: identity.session_key,
      url: initialUrl,
    }),
    timeout: 25_000,
  });
  const tabId = String(opened.tabId || opened.id || "");
  if (!tabId) throw new OperationError("browser_unavailable", "Браузер не вернул вкладку.");
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
    throw new OperationError("browser_close_failed", "Не удалось безопасно закрыть браузер.");
  }
  if (!response.ok && response.status !== 404) {
    throw new OperationError("browser_close_failed", "Не удалось безопасно закрыть браузер.");
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
  let result;
  try {
    result = await operation(browser, mutationState);
  } catch (error) {
    operationError = error;
  }
  try {
    await closeBrowser(browser.identity.user_id);
  } catch (error) {
    if (!operationError) operationError = error;
  }
  if (operationError) {
    if (mutationState.possible) operationError.mutationPossible = true;
    throw operationError;
  }
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
  const aliases = stores[store];
  for (const link of links || []) {
    const label = normalize(link.text);
    if (!aliases.some((alias) => label.includes(normalize(alias)))) continue;
    try {
      const url = new URL(link.href);
      const placeSlug = url.searchParams.get("placeSlug") || "";
      if (url.protocol !== "https:" || url.hostname !== "eda.yandex.ru" || !url.pathname.startsWith("/retail/") || !slugPattern.test(placeSlug) || url.pathname.includes("/product/")) continue;
      return { url: url.href, placeSlug, pathGroupSlug: url.pathname.split("/").filter(Boolean)[1] || "" };
    } catch {}
  }
  throw new OperationError("store_unavailable", "Выбранная сеть недоступна по сохранённому адресу.");
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
  const listingState = await waitForPageState(browser, { needLinks: true });
  const selected = selectStoreLink(store, listingState.links);
  await navigate(browser, selected.url);
  const state = await waitForPageState(browser);
  const location = {
    latitude: state.latitude ?? listingState.latitude,
    longitude: state.longitude ?? listingState.longitude,
  };
  const catalog = await evaluate(browser, catalogExpression({ ...location, placeSlug: selected.placeSlug }));
  classifyApiStatus(Number(catalog?.status || 0), "Каталог магазина недоступен.");
  if (!catalog?.place || catalog.place.slug !== selected.placeSlug) {
    throw new OperationError("store_unavailable", "Выбранный магазин не подтвердил свою витрину.");
  }
  const groupSlug = String(catalog.place.brandSlug || selected.pathGroupSlug || "");
  const placeBusiness = String(catalog.place.business || "");
  if (!slugPattern.test(groupSlug) || !businessPattern.test(placeBusiness)) {
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
      if (current.hostname === "eda.yandex.ru" && current.searchParams.get("placeSlug") === context.place_slug) {
        ready = true;
        break;
      }
    } catch {}
    await sleep(300);
  }
  if (!ready) throw new OperationError("store_unavailable", "Витрина магазина не загрузилась.");
  const catalog = await evaluate(browser, catalogExpression({
    latitude: context.latitude,
    longitude: context.longitude,
    placeSlug: context.place_slug,
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
  return context;
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

function legacyExistingCartParams(context) {
  return {
    ...legacyCartParams(context),
    placeSlug: context.place_slug,
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
        const response = await fetch('/api/v1/menu/search', {
          method: 'POST',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify({place_slug: context.place_slug, text: ingredient.query, location: {lat: context.latitude, lon: context.longitude}}),
        });
        let data = {};
        try { data = await response.json(); } catch {}
        const products = (data.blocks || []).flatMap((block) => Array.isArray(block?.payload?.products) ? block.payload.products : []);
        results[index] = {
          index,
          status: response.status,
          candidates: products.slice(0, 5).map((product) => ({
            product_id: String(product.public_id || product.publicId || product.uid || product.sku_id || product.skuId || ''),
            sku_id: String(product.sku_id || product.skuId || product.public_id || product.publicId || product.uid || ''),
            name: String(product.name || '').slice(0, 300),
            weight: String(product.weight || '').slice(0, 80),
            available: product.available !== false,
            in_stock: product.inStock ?? product.in_stock ?? null,
            price: product.decimalPromoPrice ?? product.decimalPrice ?? product.promoPrice ?? product.price ?? null,
          })),
        };
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
    const candidates = [data?.cart?.items, data?.place_cart?.items, data?.placeCart?.items];
    for (const entry of data?.cart_places_list || []) candidates.push(entry?.items, entry?.cart?.items);
    const items = candidates.find((value) => Array.isArray(value) && value.length) || (Array.isArray(data?.cart?.items) ? data.cart.items : []);
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
      items: items.map((item) => ({
        ids: identifiers(item),
        cart_item_id: String(item.id ?? item.cart_item_id ?? item.cartItemId ?? ''),
        quantity: Number(item.quantity ?? item.count ?? item.amount ?? item.item?.quantity ?? 0),
      })).filter((item) => item.ids.length && Number.isFinite(item.quantity) && item.quantity >= 0),
    };
  })()`;
}

function addLegacyItemExpression(context, productId, quantity) {
  return `(async () => {
    const context = ${JSON.stringify(context)};
    const query = new URLSearchParams(${JSON.stringify(legacyCartParams(context))});
    const response = await fetch('/api/v1/cart?' + query, {
      method: 'POST',
      headers: {'accept': 'application/json', 'content-type': 'application/json'},
      body: JSON.stringify({
        item_id: ${JSON.stringify(productId)},
        quantity: ${quantity},
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
    return {
      status: response.status,
      error_code: String(data?.code || data?.error?.code || '').slice(0, 80),
      error_message: String(errorMessage || '').slice(0, 240),
      response_keys: Object.keys(data || {}).filter((key) => /^[a-zA-Z0-9_-]{1,40}$/.test(key)).slice(0, 20),
    };
  })()`;
}

function addLegacyItemsExpression(context, items) {
  const payloadItems = items.map((item) => ({
    item_id: item.product_id,
    quantity: item.target,
  }));
  return `(async () => {
    const context = ${JSON.stringify(context)};
    const items = ${JSON.stringify(payloadItems)};
    const query = new URLSearchParams(${JSON.stringify(legacyCartParams(context))});
    const response = await fetch('/api/v1/cart/add_bulk?' + query, {
      method: 'POST',
      headers: {'accept': 'application/json', 'content-type': 'application/json'},
      body: JSON.stringify({
        items,
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
    return {
      status: response.status,
      error_code: String(data?.code || data?.error?.code || '').slice(0, 80),
      error_message: String(errorMessage || '').slice(0, 240),
      response_keys: Object.keys(data || {}).filter((key) => /^[a-zA-Z0-9_-]{1,40}$/.test(key)).slice(0, 20),
    };
  })()`;
}

function changeLegacyItemExpression(context, cartItemId, quantity) {
  return `(async () => {
    const query = new URLSearchParams(${JSON.stringify(legacyExistingCartParams(context))});
    const response = await fetch('/api/v1/cart/' + encodeURIComponent(${JSON.stringify(cartItemId)}) + '?' + query, {
      method: 'PUT',
      headers: {'accept': 'application/json', 'content-type': 'application/json'},
      body: JSON.stringify({quantity: ${quantity}}),
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
    return {
      status: response.status,
      error_code: String(data?.code || data?.error?.code || '').slice(0, 80),
      error_message: String(errorMessage || '').slice(0, 240),
      response_keys: Object.keys(data || {}).filter((key) => /^[a-zA-Z0-9_-]{1,40}$/.test(key)).slice(0, 20),
    };
  })()`;
}

function removeLegacyItemExpression(context, cartItemId) {
  return `(async () => {
    const query = new URLSearchParams(${JSON.stringify(legacyExistingCartParams(context))});
    const response = await fetch('/api/v1/cart/' + encodeURIComponent(${JSON.stringify(cartItemId)}) + '?' + query, {
      method: 'DELETE',
      headers: {'accept': 'application/json'},
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
    return {
      status: response.status,
      error_code: String(data?.code || data?.error?.code || '').slice(0, 80),
      error_message: String(errorMessage || '').slice(0, 240),
      response_keys: Object.keys(data || {}).filter((key) => /^[a-zA-Z0-9_-]{1,40}$/.test(key)).slice(0, 20),
    };
  })()`;
}

function quantityInCart(cart, ...identifiers) {
  const expected = new Set(identifiers.map(String));
  return (cart?.items || []).reduce((total, item) => (
    item.ids?.some((identifier) => expected.has(String(identifier))) ? total + Number(item.quantity || 0) : total
  ), 0);
}

function rowsInCart(cart, ...identifiers) {
  const expected = new Set(identifiers.map(String));
  return (cart?.items || []).filter((item) => (
    item.ids?.some((identifier) => expected.has(String(identifier)))
  ));
}

async function evaluateCartMutation(browser, expression) {
  let result = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    result = await evaluate(browser, expression);
    if (Number(result?.status || 0) !== 429) return result;
    // An explicit 429 cannot have applied the mutation, so retrying the exact
    // request after a short bounded backoff is safe.
    await sleep(1_500 * (attempt + 1));
  }
  return result;
}

async function readCart(browser, context) {
  const cart = await evaluate(browser, cartExpression(context));
  classifyApiStatus(Number(cart?.status || 0), "Корзина Яндекс Еды недоступна.");
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

function validateIngredients(body) {
  if (!Array.isArray(body.ingredients) || body.ingredients.length < 1 || body.ingredients.length > 24) {
    throw new OperationError("invalid_request", "В одной сборке допустимо от 1 до 24 ингредиентов.");
  }
  return body.ingredients.map((ingredient) => {
    const name = text(ingredient?.name, 180);
    const query = text(ingredient?.search_query || ingredient?.name, 180);
    if (!name || !query) throw new OperationError("invalid_request", "Ингредиент не имеет безопасного поискового запроса.");
    return { name, query };
  });
}

async function search(body) {
  const { scope, store } = validateBaseRequest(body);
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
  const selection = openSelection(body.selection_token);
  if (selection.scope !== scope || selection.store !== store) {
    throw new OperationError("invalid_selection", "Поиск относится к другому пользователю или магазину.");
  }
  const signedContext = validatedContext(selection.context);
  const requested = validateApplyItems(body, selection);
  const started = Date.now();
  const mutationState = { possible: false };
  return withBrowser(scope, async (browser) => {
    const context = await validateSignedStore(browser, signedContext);
    const before = await readCart(browser, context);
    const quantitiesBefore = new Map();
    const newItems = [];
    const existingItems = [];
    for (const item of requested) {
      const existing = quantityInCart(before, item.product_id, item.sku_id);
      const matchingRows = rowsInCart(before, item.product_id, item.sku_id);
      quantitiesBefore.set(item.product_id, existing);
      const target = Math.max(existing, item.quantity);
      if (target === existing) continue;
      if (existing > 0) {
        const cartItemId = String(matchingRows[0]?.cart_item_id || "");
        if (matchingRows.length !== 1 || !cartItemIdPattern.test(cartItemId)) {
          throw new OperationError(
            "verification_failed",
            "Точный товар в корзине нельзя безопасно изменить.",
          );
        }
        existingItems.push({ ...item, target, cart_item_id: cartItemId });
      } else {
        newItems.push({ ...item, target });
      }
    }

    const runMutation = async (mutationExpression) => {
      const mutationWasPossible = mutationState.possible;
      mutationState.possible = true;
      const changed = await evaluateCartMutation(browser, mutationExpression);
      const changedStatus = Number(changed?.status || 0);
      // A completed 4xx response means Yandex rejected this request before
      // applying it. Preserve uncertainty from any earlier successful item,
      // while avoiding a false manual-check state for the first rejected item.
      if (changedStatus >= 400 && changedStatus < 500) {
        mutationState.possible = mutationWasPossible;
      }
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

    if (newItems.length === 1) {
      await runMutation(addLegacyItemExpression(
        context,
        newItems[0].product_id,
        newItems[0].target,
      ));
    } else if (newItems.length > 1) {
      await runMutation(addLegacyItemsExpression(context, newItems));
    }
    for (const item of existingItems) {
      await runMutation(changeLegacyItemExpression(
        context,
        item.cart_item_id,
        item.target,
      ));
      await sleep(500);
    }
    const after = await readCartUntil(browser, context, (cart) => requested.every((item) => {
      const beforeQuantity = quantitiesBefore.get(item.product_id) || 0;
      return quantityInCart(cart, item.product_id, item.sku_id) >= Math.max(beforeQuantity, item.quantity);
    }));
    const additions = [];
    for (const item of requested) {
      const beforeQuantity = quantitiesBefore.get(item.product_id) || 0;
      const afterQuantity = quantityInCart(after, item.product_id, item.sku_id);
      const target = Math.max(beforeQuantity, item.quantity);
      if (afterQuantity < target || afterQuantity < beforeQuantity) {
        throw new OperationError("verification_failed", "Количество товара в корзине не удалось подтвердить.", { mutationPossible: mutationState.possible });
      }
      additions.push({
        product_id: item.product_id,
        before_quantity: beforeQuantity,
        after_quantity: afterQuantity,
        added_quantity: Math.max(0, afterQuantity - beforeQuantity),
      });
    }
    return {
      status: "applied",
      cart_url: context.store_url,
      additions,
      elapsed_ms: Date.now() - started,
    };
  }, mutationState, signedContext.store_url);
}

function validateCleanupItems(body) {
  if (!Array.isArray(body.items) || body.items.length < 1 || body.items.length > 24) {
    throw new OperationError("invalid_request", "Неверный журнал очистки.");
  }
  const grouped = new Map();
  for (const item of body.items) {
    const productId = text(item?.product_id, 128);
    const quantity = Number(item?.package_count);
    if (!productIdPattern.test(productId) || !Number.isInteger(quantity) || quantity < 1 || quantity > 100) {
      throw new OperationError("invalid_request", "Журнал очистки не содержит точный товар.");
    }
    const total = (grouped.get(productId) || 0) + quantity;
    if (total > 100) throw new OperationError("invalid_request", "Журнал очистки слишком велик.");
    grouped.set(productId, total);
  }
  return [...grouped].map(([product_id, quantity]) => ({ product_id, quantity }));
}

async function cleanup(body) {
  const { scope, store } = validateBaseRequest(body);
  const requested = validateCleanupItems(body);
  const mutationState = { possible: false };
  return withBrowser(scope, async (browser) => {
    const context = await resolveStore(browser, store);
    const before = await readCart(browser, context);
    const targets = [];
    for (const item of requested) {
      const existing = quantityInCart(before, item.product_id);
      const target = Math.max(0, existing - item.quantity);
      const matchingRows = rowsInCart(before, item.product_id);
      const cartItemId = String(matchingRows[0]?.cart_item_id || "");
      targets.push({ ...item, before: existing, target, cart_item_id: cartItemId });
      if (target === existing) continue;
      if (matchingRows.length !== 1 || !cartItemIdPattern.test(cartItemId)) {
        throw new OperationError(
          "verification_failed",
          "Точный товар в корзине нельзя безопасно изменить.",
        );
      }
      const mutationWasPossible = mutationState.possible;
      mutationState.possible = true;
      const changed = await evaluateCartMutation(
        browser,
        target === 0
          ? removeLegacyItemExpression(context, cartItemId)
          : changeLegacyItemExpression(context, cartItemId, target),
      );
      const changedStatus = Number(changed?.status || 0);
      if (changedStatus >= 400 && changedStatus < 500) {
        mutationState.possible = mutationWasPossible;
      }
      const errorCode = text(changed?.error_code, 80);
      const errorMessage = text(changed?.error_message, 240);
      if (changedStatus < 200 || changedStatus >= 300) {
        console.warn("Yandex legacy cart cleanup rejected", {
          status: changedStatus,
          code: errorCode,
          message: errorMessage,
          response_keys: Array.isArray(changed?.response_keys) ? changed.response_keys : [],
        });
      }
      classifyApiStatus(
        changedStatus,
        `Яндекс Еда не смогла уменьшить точно выбранный товар${errorCode ? ` (${errorCode})` : ""}${errorMessage ? `: ${errorMessage}` : ""}.`,
      );
    }
    const after = await readCartUntil(browser, context, (cart) => targets.every(
      (item) => quantityInCart(cart, item.product_id) === item.target,
    ));
    for (const item of targets) {
      if (quantityInCart(after, item.product_id) !== item.target) {
        throw new OperationError("verification_failed", "Очистку точного SKU не удалось подтвердить.", { mutationPossible: mutationState.possible });
      }
    }
    return { status: "cleared", summary: "Добавления этой сборки удалены из корзины." };
  }, mutationState);
}

function errorBody(error) {
  const known = error instanceof OperationError;
  const code = known ? error.code : "internal_error";
  const mutationPossible = Boolean(known && error.mutationPossible);
  if (code === "login_required") return { status: "login_required", summary: error.message, mutation_possible: mutationPossible };
  if (code === "blocked") return { status: "blocked", summary: error.message, mutation_possible: mutationPossible };
  if (code === "store_unavailable") return { status: "incomplete", summary: error.message, mutation_possible: false };
  return {
    status: "failed",
    summary: known ? error.message : "Адаптер корзины завершился с ошибкой.",
    error: code,
    mutation_possible: mutationPossible,
  };
}

const server = http.createServer(async (request, response) => {
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
    return sendJson(response, 200, await serializeOperation(operation));
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
});

server.requestTimeout = 180_000;
server.headersTimeout = 10_000;
server.listen(port, bindHost, () => {
  console.log(`Recipes cart adapter listening on ${bindHost}:${port}`);
});
