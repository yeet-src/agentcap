// collector — shared worker that owns the BPF object and the Telemetry
// registry. Scrapes reach it through yeet:telemetry's snapshot protocol
// (src/scrape.js renders per request); src/main.js holds it alive.
//
// Counter flow: the kernel sums high-rate activity into the `counters`
// hash keyed {agent, slot} (see the enum in src/bpf/agentcap.bpf.c); a 1s
// poll here turns absolute kernel counters into registry increments.
// Lifecycle events stream over the ring buffer and carry names, which is
// where the per-comm exec labels and last-activity timestamps come from.
import Telemetry from "yeet:telemetry";
import { BpfObject, RingBuf, HashMap, LpmTrie } from "yeet:bpf";
import { DEFAULT_AGENTS } from "./agents.js";

// The tracked agent set lives in src/agents.js (pre-filled with the common
// agents) and is pushed into the kernel LPM trie at startup — the BPF side
// is policy-free. Override at run time with --agents=a,b,c.

// Mirrors of the C-side layout — keep in sync with src/bpf/agentcap.bpf.c.
const SLOTS = [
  "forks", // C_FORKS
  "execs", // C_EXECS (exported via labeled ringbuf events instead)
  "exits", // C_EXITS
  "net_connects", // C_TCP_CONNECTS
  "net_tx_bytes", // C_TCP_TX_BYTES
  "net_rx_bytes", // C_TCP_RX_BYTES
  "file_rd_bytes", // C_VFS_RD_BYTES
  "file_wr_bytes", // C_VFS_WR_BYTES
  "cpu_ns", // C_CPU_NS
  "running", // C_RUNNING (gauge)
  "ev_drops", // C_EV_DROPS
];
const C_MAX = SLOTS.length;
const EV = { 1: "fork", 2: "exec", 3: "exit" };

const telemetry = new Telemetry({ service: "agentcap" });

const probeUp = telemetry.gauge("agentcap_probe_up", {
  help: "1 when the BPF object is loaded and attached, 0 on failure.",
});
const tasks = telemetry.gauge("agentcap_tasks", {
  help: "Tracked tasks (threads) alive in each agent's process tree.",
  labels: ["agent"],
});
const forks = telemetry.counter("agentcap_forks_total", {
  help: "Tasks forked inside an agent tree.",
  labels: ["agent"],
});
const execs = telemetry.counter("agentcap_execs_total", {
  help: "Binaries exec'd inside an agent tree (tools the agent ran).",
  labels: ["agent", "comm"],
});
const exits = telemetry.counter("agentcap_exits_total", {
  help: "Tasks exited inside an agent tree.",
  labels: ["agent"],
});
const cpu = telemetry.counter("agentcap_cpu_seconds_total", {
  help: "On-CPU time consumed by an agent tree.",
  labels: ["agent"],
});
const connects = telemetry.counter("agentcap_net_connects_total", {
  help: "Internet-family socket connects by an agent tree.",
  labels: ["agent"],
});
const netTx = telemetry.counter("agentcap_net_transmit_bytes_total", {
  help: "Bytes sent on internet-family sockets by an agent tree.",
  labels: ["agent"],
});
const netRx = telemetry.counter("agentcap_net_receive_bytes_total", {
  help: "Bytes received on internet-family sockets by an agent tree.",
  labels: ["agent"],
});
const fileRd = telemetry.counter("agentcap_file_read_bytes_total", {
  help: "Bytes read from files by an agent tree.",
  labels: ["agent"],
});
const fileWr = telemetry.counter("agentcap_file_write_bytes_total", {
  help: "Bytes written to files by an agent tree.",
  labels: ["agent"],
});
const drops = telemetry.counter("agentcap_events_dropped_total", {
  help: "Lifecycle events lost to ring-buffer backpressure.",
  labels: ["agent"],
});
const connsByPort = telemetry.counter("agentcap_net_connections_total", {
  help: "Inet socket connects by destination port.",
  labels: ["agent", "port"],
});
const dnsQueries = telemetry.counter("agentcap_dns_queries_total", {
  help: "DNS queries by name (plain port-53 DNS; DoH/DoT are not visible).",
  labels: ["agent", "domain"],
});
const filesOpened = telemetry.counter("agentcap_files_opened_total", {
  help: "Regular files opened by an agent tree, by path and access mode " +
    "(noisy system/library reads are filtered out).",
  labels: ["agent", "path", "mode"],
});
const lastActivity = telemetry.gauge(
  "agentcap_last_activity_timestamp_seconds",
  {
    help: "Unix time of the last lifecycle event seen for an agent.",
    labels: ["agent"],
  },
);

// Registry counters want increments; the kernel keeps absolutes. Poll and
// feed the deltas. Slot -> series wiring lives here.
const SERIES = {
  forks,
  exits,
  net_connects: connects,
  net_tx_bytes: netTx,
  net_rx_bytes: netRx,
  file_rd_bytes: fileRd,
  file_wr_bytes: fileWr,
  ev_drops: drops,
};

// Label-cardinality guard: past `max` distinct values per agent, new ones
// fold into "other".
const capped = (max) => {
  const seen = new Map(); // agent -> Set
  return (agent, value) => {
    let set = seen.get(agent);
    if (!set) seen.set(agent, (set = new Set()));
    if (set.has(value)) return value;
    if (set.size >= max) return "other";
    set.add(value);
    return value;
  };
};
const commLabel = capped(50);
const domainLabel = capped(200);
const portLabel = capped(50);
const fileLabel = capped(300);

// Agents open a storm of shared libraries, runtime metadata and caches on
// every start. For an audit view those are noise; skip read-only opens
// under system prefixes so the table shows the files an agent actually
// works with (writes are always kept). Tune to taste.
const NOISE_PREFIXES = [
  "/usr/", "/lib/", "/lib64/", "/proc/", "/sys/", "/dev/", "/run/",
  "/etc/ld.so", "/etc/ssl/", "/etc/pki/",
];
const isNoise = (path) => NOISE_PREFIXES.some((p) => path.startsWith(p));

// The kernel ships raw lookup payloads (see dns_event in agentcap.bpf.c);
// the string parsing happens here where loops are verifier-free.
// enc 1: DNS packet head — 12-byte header, then length-prefixed labels.
// enc 2: nss-resolved varlink JSON — {"method":"…ResolveHostname",
//        "parameters":{"name":"…"}}.
const DNS_RE = /"method"\s*:\s*"io\.systemd\.Resolve\.ResolveHostname"/;
const NAME_RE = /"name"\s*:\s*"([^"\\]{1,255})"/;

function parseLookup(enc, payload) {
  const bytes = payload ?? [];
  if (enc === 1) {
    const parts = [];
    let i = 12;
    while (i < bytes.length) {
      const len = bytes[i];
      if (!len) break;
      if (len & 0xc0 || i + 1 + len > bytes.length) return null;
      let label = "";
      for (let j = i + 1; j <= i + len; j++) {
        const c = bytes[j];
        if (c < 33 || c > 126) return null; // not a hostname query
        label += String.fromCharCode(c);
      }
      parts.push(label);
      i += 1 + len;
    }
    return parts.length ? parts.join(".").toLowerCase() : null;
  }
  if (enc === 2) {
    let s = "";
    for (const b of bytes) {
      if (!b) break;
      s += String.fromCharCode(b);
    }
    if (!DNS_RE.test(s)) return null;
    const m = NAME_RE.exec(s);
    return m ? m[1].toLowerCase() : null;
  }
  return null;
}

// Agent index -> label; slot i of the kernel `prefixes` map is agent i.
let agentNames = [];
const nameOf = (i) => agentNames[i] || `agent${i}`;

async function start() {
  const ctl = await new BpfObject({
    exe: "../bin/probe.bpf.o",
    base: import.meta.dirname,
  })
    .bind("events", { kind: "ringbuf", btf_struct: "agent_event" })
    .bind("conn_events", { kind: "ringbuf", btf_struct: "conn_event" })
    .bind("dns_events", { kind: "ringbuf", btf_struct: "dns_event" })
    .bind("file_events", { kind: "ringbuf", btf_struct: "file_event" })
    .bind("counters", { kind: "hashmap" })
    .bind("prefixes", { kind: "lpm_trie" })
    .start();

  // Push the agent set into the kernel LPM trie. Each prefix is inserted
  // once per boundary character — "name\0" (zero padding), "name-",
  // "name_", "name." — so "pi" matches "pi" and "pi-agent" but never
  // "pipewire"; the value is the agent index metrics are labeled with.
  // --agents=openclaw,claude,… overrides the default set.
  agentNames = typeof yeet.args?.agents === "string"
    ? yeet.args.agents.split(",").map((s) => s.trim()).filter(Boolean)
    : DEFAULT_AGENTS;

  const prefixes = new LpmTrie(ctl, "prefixes");
  for (let i = 0; i < agentNames.length; i++) {
    const name = agentNames[i].slice(0, 15); // comm is 15 chars + NUL
    for (const b of ["\0", "-", "_", "."]) {
      await prefixes.update(
        { prefixlen: (name.length + 1) * 8, data: name + b },
        i,
      );
    }
  }

  // Lifecycle events: exec labels, last-activity stamps.
  const stamp = (agent) => lastActivity.labels({ agent }).set(Date.now() / 1000);
  const events = new RingBuf(ctl, "events");
  await events.subscribe((w) => {
    const e = w?.agent_event ?? w;
    const agent = nameOf(e.agent);
    stamp(agent);
    if (EV[e.kind] === "exec") {
      execs.labels({ agent, comm: commLabel(agent, e.comm) }).inc();
    }
  });

  // Audit streams: destination ports and DNS query names.
  const conns = new RingBuf(ctl, "conn_events");
  await conns.subscribe((w) => {
    const e = w?.conn_event ?? w;
    const agent = nameOf(e.agent);
    stamp(agent);
    connsByPort.labels({ agent, port: portLabel(agent, String(e.dport)) }).inc();
  });

  const dns = new RingBuf(ctl, "dns_events");
  await dns.subscribe((w) => {
    const e = w?.dns_event ?? w;
    const domain = parseLookup(e.enc, e.payload);
    if (!domain) return;
    const agent = nameOf(e.agent);
    stamp(agent);
    dnsQueries.labels({ agent, domain: domainLabel(agent, domain) }).inc();
  });

  const files = new RingBuf(ctl, "file_events");
  await files.subscribe((w) => {
    const e = w?.file_event ?? w;
    const path = (e.path ?? "").replace(/\0.*$/, "");
    const write = e.write ? true : false;
    if (!path || (!write && isNoise(path))) return; // keep all writes
    const agent = nameOf(e.agent);
    stamp(agent);
    filesOpened
      .labels({ agent, path: fileLabel(agent, path), mode: write ? "write" : "read" })
      .inc();
  });

  // Counter poll: absolute kernel u64s -> registry deltas. The kernel map
  // is a hash keyed {agent, slot}, so only series that were ever touched
  // exist — one walk covers any number of agents.
  const counters = new HashMap(ctl, "counters");
  const prev = new Map();
  const tick = async () => {
    for await (const [k, cur] of counters.entries()) {
      const agent = nameOf(k.agent);
      const slot = SLOTS[k.slot];
      const key = `${k.agent}:${k.slot}`;
      const delta = cur - (prev.get(key) ?? 0n);
      prev.set(key, cur);
      if (slot === "running") {
        tasks.labels({ agent }).set(Number(cur));
      } else if (slot === "cpu_ns") {
        if (delta > 0n) cpu.labels({ agent }).inc(Number(delta) / 1e9);
      } else if (SERIES[slot] && delta > 0n) {
        SERIES[slot].labels({ agent }).inc(Number(delta));
      }
    }
  };
  setInterval(() => tick().catch((e) => console.error("poll:", e)), 1000);
  await tick();

  probeUp.set(1);
  console.log(`agentcap: tracing agents: ${agentNames.filter(Boolean).join(", ")}`);
}

start().catch((e) => {
  probeUp.set(0);
  console.error(`agentcap: probe failed: ${e?.message ?? e}`);
});

telemetry.serve();

// Standalone correctness probe: `yeet run src/collector.js` (no worker, no
// HTTP) dumps live events and slot deltas so field names, the btf_struct
// envelope, and BigInt handling can be eyeballed before any service exists.
if (import.meta.main) {
  const big = (_k, v) => (typeof v === "bigint" ? `${v}n` : v);
  setInterval(async () => {
    console.log("--- registry snapshot ---");
    try {
      const { render } = await import("yeet:telemetry");
      console.log(await render(telemetry.snapshot()));
    } catch (e) {
      console.log("(snapshot render unavailable:", e?.message ?? e, ")");
    }
  }, 3000);
}
