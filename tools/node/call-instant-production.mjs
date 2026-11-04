import { refreshSessionIfNeeded } from "./session-refresh.mjs";

const requiredVariables = ["ACCOUNT_ID", "SUPPLY_ID"];
const missingVariables = requiredVariables.filter((name) => !process.env[name]);

if (missingVariables.length > 0) {
  console.error(`Variabili mancanti: ${missingVariables.join(", ")}`);
  console.error("Esegui: node --env-file=.env call-instant-production.mjs");
  process.exit(1);
}

const baseUrl = "https://eniplenitude.com";
const path = `/serviceDAp/api/pannelli-services/v1/accounts/${encodeURIComponent(process.env.ACCOUNT_ID)}/supply/${encodeURIComponent(process.env.SUPPLY_ID)}`;
function endpointUrl(endpoint) {
  const url = new URL(`${path}/${endpoint}`, baseUrl);
  // The API requires the parameter to be present, including when it has no value.
  url.searchParams.set("logHash", process.env.LOG_HASH ?? "");
  url.searchParams.set("channel", "PORTAL");
  return url;
}

async function readResponse(response) {
  const body = await response.text();

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}\n${body}`);
  }

  try {
    return JSON.parse(body);
  } catch {
    return body;
  }
}

try {
  const refreshResult = await refreshSessionIfNeeded();
  if (refreshResult.refreshed) {
    console.log("Sessione aggiornata prima della richiesta.");
  }

  const headers = {
    accept: "application/json, text/plain, */*",
    "cache-control": "no-cache, no-store",
    cookie: process.env.COOKIE,
    pragma: "no-cache",
  };
  const detailsResponse = await fetch(endpointUrl("details"), { headers });
  const details = await readResponse(detailsResponse);

  if (!Array.isArray(details.panels)) {
    throw new Error("La risposta details non contiene un array panels.");
  }

  const panels = details.panels.map(({ identifier, position, power }) => ({
    identifier,
    position,
    power,
  }));
  const productionResponse = await fetch(endpointUrl("instant-production"), {
    method: "POST",
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify(panels),
  });

  console.log(JSON.stringify(await readResponse(productionResponse), null, 2));
} catch (error) {
  console.error("Richiesta fallita:", error.message);
  process.exit(1);
}
