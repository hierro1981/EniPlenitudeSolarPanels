import { appendFile, mkdir, writeFile } from "node:fs/promises";
import { createHmac, randomBytes } from "node:crypto";
import { resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { chromium } from "playwright";

const baseUrl = "https://eniplenitude.com/my-eni";
const intervalMinutes = Number.parseInt(process.env.MONITOR_INTERVAL_MINUTES ?? "5", 10);
const durationMinutes = Number.parseInt(process.env.MONITOR_DURATION_MINUTES ?? "120", 10);

if (!Number.isInteger(intervalMinutes) || intervalMinutes < 3 || intervalMinutes > 60) {
  throw new Error("MONITOR_INTERVAL_MINUTES deve essere un numero intero tra 3 e 60.");
}
if (!Number.isInteger(durationMinutes) || durationMinutes < intervalMinutes) {
  throw new Error("MONITOR_DURATION_MINUTES deve essere almeno pari all'intervallo.");
}

const startedAt = new Date();
const runId = startedAt.toISOString().replace(/[:.]/g, "-");
const outputDir = resolve("monitor-logs", runId);
const eventsPath = resolve(outputDir, "events.jsonl");
const secretKey = randomBytes(32);
const cookieFingerprints = new Map();
const eventCounts = new Map();
const plenitudeRequestIds = new Set();
const pendingRequestCookieContext = new Map();
const pendingSetCookies = new Map();
let eventNumber = 0;

function fingerprint(value) {
  return createHmac("sha256", secretKey).update(value).digest("hex").slice(0, 16);
}

function isPlenitudeUrl(url) {
  try {
    const hostname = new URL(url).hostname;
    return hostname === "eniplenitude.com" || hostname.endsWith(".eniplenitude.com");
  } catch {
    return false;
  }
}

function urlSummary(url) {
  const parsed = new URL(url);
  return {
    host: parsed.hostname,
    path: parsed.pathname,
    queryParameters: [...new Set([...parsed.searchParams.keys()])].sort(),
  };
}

function findHeader(headers, name) {
  const expected = name.toLowerCase();
  const key = Object.keys(headers).find((header) => header.toLowerCase() === expected);
  return key ? String(headers[key]) : undefined;
}

function cookieNames(cookieHeader) {
  if (!cookieHeader) return [];
  return cookieHeader
    .split(";")
    .map((part) => part.trim().split("=", 1)[0])
    .filter(Boolean)
    .sort();
}

function requestHeadersSummary(headers) {
  const safeValues = {};
  for (const name of ["accept", "content-type", "origin", "referer", "x-request-id"]) {
    const value = findHeader(headers, name);
    if (value) safeValues[name] = name === "x-request-id" ? "present" : value;
  }
  return { names: Object.keys(headers).map((name) => name.toLowerCase()).sort(), safeValues };
}

function parseSetCookies(headers) {
  const values = Object.entries(headers)
    .filter(([name]) => name.toLowerCase() === "set-cookie")
    .flatMap(([, value]) => String(value).split(/\r?\n/));
  return values.flatMap((value) => {
    const [pair, ...attributes] = value.split(";").map((part) => part.trim());
    const [name, ...valueParts] = pair.split("=");
    if (!name || valueParts.length === 0) return [];
    const parsedAttributes = {};
    for (const attribute of attributes) {
      const [attributeName, ...attributeValue] = attribute.split("=");
      parsedAttributes[attributeName.toLowerCase()] = attributeValue.join("=") || true;
    }
    return [{
      name,
      valueFingerprint: fingerprint(valueParts.join("=")),
      attributes: parsedAttributes,
    }];
  });
}

function recordRequestCookieContext(event) {
  const associatedCookies = event.associatedCookies?.map(({ cookie }) => cookie.name).sort() ?? [];
  return record("request_cookie_context", { requestId: event.requestId, associatedCookies });
}

function recordSetCookies(event) {
  const setCookies = parseSetCookies(event.headers);
  if (setCookies.length > 0) return record("set_cookie", { requestId: event.requestId, setCookies });
  return Promise.resolve();
}

async function record(type, details = {}) {
  eventCounts.set(type, (eventCounts.get(type) ?? 0) + 1);
  await appendFile(
    eventsPath,
    `${JSON.stringify({ number: ++eventNumber, at: new Date().toISOString(), type, ...details })}\n`,
  );
}

async function snapshot(context, page, label) {
  const cookies = (await context.cookies()).filter((cookie) =>
    isPlenitudeUrl(`https://${cookie.domain.replace(/^\./, "")}`),
  );
  const safeCookies = cookies.map((cookie) => {
    const key = `${cookie.name}|${cookie.domain}|${cookie.path}`;
    const valueFingerprint = fingerprint(cookie.value);
    const previousFingerprint = cookieFingerprints.get(key);
    cookieFingerprints.set(key, valueFingerprint);
    return {
      name: cookie.name,
      domain: cookie.domain,
      path: cookie.path,
      expires: cookie.expires,
      httpOnly: cookie.httpOnly,
      secure: cookie.secure,
      sameSite: cookie.sameSite,
      valueFingerprint,
      changed: previousFingerprint !== undefined && previousFingerprint !== valueFingerprint,
    };
  });

  let storage = { localStorage: [], sessionStorage: [] };
  if (!page.isClosed() && isPlenitudeUrl(page.url())) {
    const rawStorage = await page.evaluate(() => ({
      localStorage: Object.entries(localStorage),
      sessionStorage: Object.entries(sessionStorage),
    })).catch(() => storage);
    storage = Object.fromEntries(
      Object.entries(rawStorage).map(([name, entries]) => [
        name,
        entries.map(([key, value]) => ({ key, valueFingerprint: fingerprint(value) })),
      ]),
    );
  }
  await record("snapshot", { label, page: page.isClosed() ? null : urlSummary(page.url()), cookies: safeCookies, storage });
}

async function waitForManualLogin() {
  const terminal = createInterface({ input: stdin, output: stdout });
  try {
    await terminal.question("Completa il login manuale nel browser, poi premi Invio per iniziare il monitoraggio: ");
  } finally {
    terminal.close();
  }
}

await mkdir(outputDir, { recursive: true });
await writeFile(resolve(outputDir, "README.txt"), [
  "Questo report non contiene valori di cookie, token, query o storage.",
  "Le impronte sono HMAC casuali valide solo per questa esecuzione.",
  `Avviato: ${startedAt.toISOString()}`,
  `Intervallo: ${intervalMinutes} minuti; durata: ${durationMinutes} minuti.`,
  "",
].join("\n"));

const browser = await chromium.launch({ headless: false });
const context = await browser.newContext();
const page = await context.newPage();
const cdp = await context.newCDPSession(page);
await cdp.send("Network.enable", { maxTotalBufferSize: 0, maxResourceBufferSize: 0 });

cdp.on("Network.requestWillBeSent", (event) => {
  if (!isPlenitudeUrl(event.request.url)) return;
  plenitudeRequestIds.add(event.requestId);
  void record("request", {
    requestId: event.requestId,
    method: event.request.method,
    url: urlSummary(event.request.url),
    headers: requestHeadersSummary(event.request.headers),
    cookieNames: cookieNames(findHeader(event.request.headers, "cookie")),
  });
  const cookieContext = pendingRequestCookieContext.get(event.requestId);
  if (cookieContext) {
    pendingRequestCookieContext.delete(event.requestId);
    void recordRequestCookieContext(cookieContext);
  }
});
cdp.on("Network.requestWillBeSentExtraInfo", (event) => {
  if (plenitudeRequestIds.has(event.requestId)) {
    void recordRequestCookieContext(event);
  } else {
    // CDP permits this event to arrive before the URL-bearing request event.
    pendingRequestCookieContext.set(event.requestId, event);
  }
});
cdp.on("Network.responseReceived", (event) => {
  if (!isPlenitudeUrl(event.response.url)) return;
  void record("response", {
    requestId: event.requestId,
    status: event.response.status,
    url: urlSummary(event.response.url),
    mimeType: event.response.mimeType,
  });
  const setCookieEvent = pendingSetCookies.get(event.requestId);
  if (setCookieEvent) {
    pendingSetCookies.delete(event.requestId);
    void recordSetCookies(setCookieEvent);
  }
});
cdp.on("Network.responseReceivedExtraInfo", (event) => {
  if (plenitudeRequestIds.has(event.requestId)) {
    void recordSetCookies(event);
  } else {
    // CDP permits this event to arrive before the URL-bearing response event.
    pendingSetCookies.set(event.requestId, event);
  }
});

let stopping = false;
let signalStop;
const stopped = new Promise((resolve) => { signalStop = resolve; });
const stop = () => {
  stopping = true;
  signalStop();
};
process.on("SIGINT", stop);
process.on("SIGTERM", stop);

try {
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await waitForManualLogin();
  await snapshot(context, page, "after-manual-login");

  const deadline = Date.now() + durationMinutes * 60_000;
  let cycle = 0;
  while (!stopping && Date.now() < deadline && !page.isClosed()) {
    const remaining = deadline - Date.now();
    await Promise.race([page.waitForTimeout(Math.min(intervalMinutes * 60_000, remaining)), stopped]);
    if (stopping || page.isClosed()) break;
    cycle += 1;
    console.log(`Refresh ${cycle} in corso...`);
    await page.reload({ waitUntil: "domcontentloaded", timeout: 60_000 }).catch(async (error) => {
      await record("reload_error", { cycle, message: error.message });
    });
    await page.waitForTimeout(10_000);
    await snapshot(context, page, `after-refresh-${cycle}`);
  }
} finally {
  await snapshot(context, page, stopping ? "stopped" : "completed").catch(() => {});
  const summary = {
    startedAt: startedAt.toISOString(),
    endedAt: new Date().toISOString(),
    output: outputDir,
    eventCounts: Object.fromEntries(eventCounts),
    trackedCookies: cookieFingerprints.size,
  };
  await writeFile(resolve(outputDir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  await browser.close();
  console.log(`Monitoraggio concluso. Report redatto: ${outputDir}`);
}
