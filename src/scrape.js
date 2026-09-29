// scrape — per-request isolate behind the /metrics route (console portal,
// per-connection tenancy): its console output IS the HTTP response body.
// The worker spec resolves to the same shared worker src/main.js keeps
// alive, so the registry state survives between scrapes.
import { render } from "yeet:telemetry";

console.log(await render({ worker: "./collector.js" }));
