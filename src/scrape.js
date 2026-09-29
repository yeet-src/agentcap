// scrape — per-request isolate behind the /metrics route (console portal,
// per-connection tenancy): its console output IS the HTTP response body.
// The worker spec resolves to the same shared worker src/main.js keeps
// alive, so the registry state survives between scrapes.
import { render } from "yeet:telemetry";

// Render first, then emit on a deferred tick. The per-connection console
// portal only streams what the isolate emits AFTER the request's console
// attaches, which happens a moment after the isolate starts. Writing (and
// finishing) immediately races that attach — "a script that finishes before
// the console attaches has nothing left to stream" (yeet services docs) —
// and intermittently yields an empty 200 / 502. Deferring the write lets the
// attach win, and the pending timer keeps the isolate alive until the body
// has streamed. Same reason the docs' snapshot.js example defers.
const body = await render({ worker: "./collector.js" });
setTimeout(() => console.log(body), 100);
