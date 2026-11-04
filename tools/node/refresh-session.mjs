import { refreshSessionIfNeeded } from "./session-refresh.mjs";

try {
  const result = await refreshSessionIfNeeded();
  if (result.refreshed) {
    console.log(`Sessione aggiornata: ${result.updatedCookies.join(", ")}`);
  } else {
    console.log("Sessione verificata: c360/init non ha ruotato GAESA.");
  }
} catch (error) {
  console.error("Refresh sessione fallito:", error.message);
  process.exit(1);
}
