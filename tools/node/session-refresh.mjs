import { open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { randomInt } from "node:crypto";

const logHashCharacters = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";

function createLogHash() {
  return Array.from(
    { length: 22 },
    () => logHashCharacters[randomInt(logHashCharacters.length)],
  ).join("");
}

function parseCookieHeader(value) {
  const cookies = new Map();

  for (const entry of value.split(/;\s*/)) {
    const separator = entry.indexOf("=");
    if (separator > 0) {
      cookies.set(entry.slice(0, separator), entry.slice(separator + 1));
    }
  }

  return cookies;
}

function serializeCookieHeader(cookies) {
  return [...cookies].map(([name, value]) => `${name}=${value}`).join("; ");
}

function readSetCookies(response) {
  const updates = new Map();

  for (const setCookie of response.headers.getSetCookie()) {
    const [pair] = setCookie.split(";", 1);
    const separator = pair.indexOf("=");
    if (separator > 0) {
      updates.set(pair.slice(0, separator), pair.slice(separator + 1));
    }
  }

  return updates;
}

function setEnvValue(lines, name, value) {
  const lineIndex = lines.findIndex((line) => line.startsWith(`${name}=`));

  if (lineIndex === -1) {
    lines.splice(lines.at(-1) === "" ? -1 : lines.length, 0, `${name}=${value}`);
  } else {
    lines[lineIndex] = `${name}=${value}`;
  }
}

async function lockEnv(envFile) {
  const lockFile = `${envFile}.lock`;

  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const handle = await open(lockFile, "wx");
      return async () => {
        await handle.close();
        await unlink(lockFile);
      };
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  throw new Error("Aggiornamento sessione gia' in corso");
}

async function updateEnv(envFile, expectedCookie, cookie) {
  const content = await readFile(envFile, "utf8");
  const lineEnding = content.includes("\r\n") ? "\r\n" : "\n";
  const lines = content.split(/\r?\n/);
  const legacyCookieLine = lines.findIndex((line) => line.startsWith("GIGYA_COOKIE="));
  const cookieLine = lines.find((line) => line.startsWith("COOKIE="));
  const currentCookie = cookieLine?.slice("COOKIE=".length);

  if (currentCookie && currentCookie !== expectedCookie) {
    throw new Error("Il cookie e' cambiato durante l'aggiornamento della sessione");
  }

  if (!lines.some((line) => line.startsWith("COOKIE=")) && legacyCookieLine !== -1) {
    lines[legacyCookieLine] = `COOKIE=${cookie}`;
  } else {
    setEnvValue(lines, "COOKIE", cookie);
  }
  const temporaryFile = `${envFile}.${process.pid}.tmp`;
  await writeFile(temporaryFile, lines.join(lineEnding), "utf8");
  await rename(temporaryFile, envFile);
}

async function readEnvCookie(envFile) {
  try {
    const content = await readFile(envFile, "utf8");
    const line = content
      .split(/\r?\n/)
      .find((value) => value.startsWith("COOKIE=") || value.startsWith("GIGYA_COOKIE="));
    return line?.slice(line.indexOf("=") + 1);
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
}

export async function refreshSessionIfNeeded({ envFile = ".env" } = {}) {
  const releaseLock = await lockEnv(envFile);
  try {
    const cookie = (await readEnvCookie(envFile)) || process.env.COOKIE || process.env.GIGYA_COOKIE;
    const missingVariables = [["COOKIE", cookie]]
      .filter(([, value]) => !value)
      .map(([name]) => name);

    if (missingVariables.length > 0) {
      throw new Error(`Variabili mancanti: ${missingVariables.join(", ")}`);
    }

    const initUrl = new URL("https://eniplenitude.com/serviceDAp/api/c360/init");
    initUrl.searchParams.set("logHash", createLogHash());
    initUrl.searchParams.set("channel", "PORTAL");

    const sessionResponse = await fetch(initUrl, {
      headers: {
        accept: "application/json, text/plain, */*",
        "cache-control": "no-cache, no-store",
        cookie,
        pragma: "no-cache",
        referer: "https://eniplenitude.com/my-eni/home",
      },
    });
    await sessionResponse.arrayBuffer();

    const updates = readSetCookies(sessionResponse);
    if (!sessionResponse.ok) {
      throw new Error(`c360/init non riuscita (HTTP ${sessionResponse.status})`);
    }
    if (!updates.has("GAESA")) {
      return {
        refreshed: false,
        updatedCookies: [],
      };
    }

    const cookies = parseCookieHeader(cookie);
    cookies.set("GAESA", updates.get("GAESA"));

    const refreshedCookie = serializeCookieHeader(cookies);
    await updateEnv(envFile, cookie, refreshedCookie);
    process.env.COOKIE = refreshedCookie;

    return {
      refreshed: true,
      updatedCookies: ["GAESA"],
    };
  } finally {
    await releaseLock();
  }
}
