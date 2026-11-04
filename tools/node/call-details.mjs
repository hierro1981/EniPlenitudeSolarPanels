import { refreshSessionIfNeeded } from "./session-refresh.mjs";

const requiredVariables = ["COOKIE", "ACCOUNT_ID", "SUPPLY_ID"];
const missingVariables = requiredVariables.filter((name) => !process.env[name]);

if (missingVariables.length > 0) {
  console.error(`Variabili mancanti: ${missingVariables.join(", ")}`);
  console.error("Esegui: node --env-file=.env call-details.mjs");
  process.exit(1);
}

const url = new URL(
  `/serviceDAp/api/pannelli-services/v1/accounts/${encodeURIComponent(process.env.ACCOUNT_ID)}/supply/${encodeURIComponent(process.env.SUPPLY_ID)}/details`,
  "https://eniplenitude.com",
);

// The API requires the parameter to be present, including when it has no value.
url.searchParams.set("logHash", process.env.LOG_HASH ?? "");
url.searchParams.set("channel", "PORTAL");

try {
  await refreshSessionIfNeeded();
  const response = await fetch(url, {
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache, no-store",
      cookie: process.env.COOKIE,
      pragma: "no-cache",
    },
  });

  const body = await response.text();

  if (!response.ok) {
    console.error(`Richiesta fallita: ${response.status} ${response.statusText}`);
    console.error(body);
    process.exit(1);
  }

  try {
    console.log(JSON.stringify(JSON.parse(body), null, 2));
  } catch {
    console.log(body);
  }
} catch (error) {
  console.error("Errore di rete:", error.message);
  process.exit(1);
}
