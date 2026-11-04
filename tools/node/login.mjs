import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const loginUrl = "https://eniplenitude.com/my-eni";
const sessionCheckPath = "/serviceDAp/api/webOrderCommonServices/v1/user/logged-in/check";
const envPath = resolve(".env");
const requiredVariables = ["ENI_USERNAME", "ENI_PASSWORD"];
const missingVariables = requiredVariables.filter((name) => !process.env[name]);

if (missingVariables.length > 0) {
  console.error(`Variabili mancanti: ${missingVariables.join(", ")}`);
  console.error("Imposta ENI_USERNAME ed ENI_PASSWORD in .env.");
  process.exit(1);
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function updateCookie(cookie) {
  let env;

  try {
    env = await readFile(envPath, "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    env = "";
  }

  const cookieLine = `COOKIE=${cookie}`;
  const cookiePattern = new RegExp(`^${escapeRegExp("COOKIE")}=.*$`, "m");
  const updatedEnv = cookiePattern.test(env)
    ? env.replace(cookiePattern, cookieLine)
    : `${env}${env && !env.endsWith("\n") ? "\n" : ""}${cookieLine}\n`;

  await writeFile(envPath, updatedEnv, { encoding: "utf8", mode: 0o600 });
}

async function fillFirstVisible(page, selectors, value) {
  for (const selector of selectors) {
    const field = page.locator(selector).first();
    if (await field.isVisible().catch(() => false)) {
      await field.fill(value);
      return;
    }
  }

  throw new Error(`Campo di login non trovato: ${selectors.join(", ")}`);
}

async function clickPrimarySubmit(page) {
  const buttons = page.locator("#pln-login form button[type='submit']");
  const count = await buttons.count();

  for (let index = 0; index < count; index += 1) {
    const button = buttons.nth(index);
    const label = await button.innerText();
    if (
      (await button.isVisible().catch(() => false)) &&
      !/social/i.test(label)
    ) {
      await button.click();
      return;
    }
  }

  throw new Error("Pulsante di accesso non trovato.");
}

async function waitForRecaptcha(page, timeoutMs) {
  const token = page.locator("textarea[name='g-recaptcha-response']").first();

  console.log("Completa il reCAPTCHA nel browser per proseguire.");
  await page.waitForFunction(
    () => document.querySelector("textarea[name='g-recaptcha-response']")?.value,
    undefined,
    { timeout: timeoutMs },
  );

  if (!(await token.inputValue())) {
    throw new Error("Token reCAPTCHA non disponibile.");
  }
}

async function hasAuthenticatedSession(page) {
  return page.evaluate(async (path) => {
    const response = await fetch(path, {
      method: "POST",
      credentials: "include",
    });

    if (!response.ok) return false;
    const body = await response.json().catch(() => null);
    return body?.isLoggedIn === true;
  }, sessionCheckPath);
}

async function waitForAuthenticatedSession(page, timeoutMs) {
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    if (await hasAuthenticatedSession(page).catch(() => false)) return;
    await page.waitForTimeout(2_000);
  }

  throw new Error("Sessione non confermata entro 5 minuti.");
}

const browser = await chromium.launch({ headless: false });
const context = await browser.newContext();
const page = await context.newPage();

try {
  await page.goto(loginUrl, { waitUntil: "domcontentloaded" });
  await page.locator("#email").waitFor({ state: "visible", timeout: 30_000 });

  await fillFirstVisible(
    page,
    ["#email", "#pln-login input[name='email']"],
    process.env.ENI_USERNAME,
  );
  await waitForRecaptcha(page, 5 * 60_000);
  await clickPrimarySubmit(page);

  await page
    .locator("#password, #pln-login input[name='password'], #pln-login input[type='password']")
    .first()
    .waitFor({ state: "visible", timeout: 30_000 });
  await fillFirstVisible(
    page,
    ["#password", "#pln-login input[name='password']", "#pln-login input[type='password']"],
    process.env.ENI_PASSWORD,
  );
  await clickPrimarySubmit(page);

  console.log("Accesso inviato. Completa nel browser eventuali OTP richiesti dal sito.");
  await waitForAuthenticatedSession(page, 5 * 60_000);

  const cookies = (await context.cookies()).filter((cookie) => {
    const domain = cookie.domain.replace(/^\./, "");
    return domain === "eniplenitude.com" || domain.endsWith(".eniplenitude.com");
  });
  const cookieHeader = cookies.map(({ name, value }) => `${name}=${value}`).join("; ");

  if (!cookieHeader) throw new Error("Nessun cookie Plenitude disponibile dopo il login.");

  await updateCookie(cookieHeader);
  console.log(`Sessione verificata. Salvati ${cookies.length} cookie Plenitude in .env.`);
} catch (error) {
  console.error("Login fallito:", error.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
