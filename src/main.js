// keeper — the service's one eager isolate. The host stops a shared
// worker when its last connection closes, and a scrape isolate lives for
// milliseconds; this long-lived port is what holds the collector (and the
// BPF probe + registry inside it) alive between scrapes.
import { keep } from "yeet:telemetry";

keep("./collector.js");
// No park: evaluation must finish for the service to leave "starting";
// the open worker port keeps this isolate (and the worker) alive.
