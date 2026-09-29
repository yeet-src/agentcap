# agentcap

A Prometheus exporter for **AI-agent activity** — OpenClaw, Claude Code,
Codex, Gemini, aider, omp, pi, grok, opencode, or any set you configure —
captured in-kernel with eBPF and served by a [yeet](https://yeet.cx)
service. A provisioned Grafana dashboard rides along.

![Agent Activity dashboard — overview: hero stats, tracked tasks, tool execs/s, which agent ran what, top tools, CPU by agent](docs/dashboard-overview.png)

```
 kernel (eBPF)                yeet service "agentcap"              observability
┌────────────────────┐   ┌──────────────────────────────────┐   ┌──────────────┐
│ tracepoints (sched)│   │ collector.js  (shared worker)    │   │ Prometheus   │
│ lsm/socket_*       ├──▶│  BPF maps → Telemetry registry   │◀──┤  :9464/metrics
│ fexit/vfs_*,recvmsg│   │ main.js       keeps it alive     │   │ Grafana      │
│  — zero kprobes    │   │ scrape.js     per-request render │   │  :3000       │
└────────────────────┘   └──────────────────────────────────┘   └──────────────┘
```

## Quickstart

```sh
curl -fsSL https://yeet.cx | sh          # 1. install yeet (CLI + yeetd daemon)
yeet login                               # 2. authenticate this host
git clone https://github.com/yeet-src/agentcap && cd agentcap
make check                               # 3. preflight (yeet, daemon, login, docker)
make up                                  # 4. build + deploy + Prometheus + Grafana
```

Then open **<http://localhost:3000/d/agentcap/agent-activity>**. `make down`
tears it all back down. Full detail — including what to do if a check fails —
is in **[Setup](#setup)**.

## Agents included

Tracked out of the box (edit [`src/agents.txt`](src/agents.txt) to change the
list — one comm prefix per line):

| | | | |
|---|---|---|---|
| OpenClaw (`openclaw`) | Claude Code (`claude`) | OpenAI Codex (`codex`) | Gemini CLI (`gemini`) |
| aider (`aider`) | opencode (`opencode`) | Block goose (`goose`) | Cline (`cline`) |
| Continue (`continue`) | Cursor (`cursor`) | qwen-code (`qwen`) | Charm crush (`crush`) |
| Sourcegraph amp (`amp`) | grok (`grok`) | omp (`omp`) | pi (`pi`) |

Each name is a **process-comm prefix**; a match pulls in that process's whole
tree, so the `bash` / `node` / `curl` an agent spawns is counted under it. Only
agents actually running appear in the metrics — the list is the watch set, not
a requirement that all be present.

## How it watches agents

The BPF side (`src/bpf/agentcap.bpf.c`) is **policy-free**: the collector
reads the prefix list from `src/agents.txt` and pushes it into a kernel
**LPM trie** at startup, so matching is a single loop-free trie lookup with
no per-agent cost. The word-boundary rule is encoded in the data — each prefix
is inserted as `name\0` / `name-` / `name_` / `name.` — so "pi" catches
`pi`, not `pipewire`. A task is tracked when its comm matches the trie or
when a tracked task forks it — each tree keeps the
identity of the prefix that rooted it, so a `bash` exec'd by openclaw is
counted as `agent="openclaw"`. Roots that predate the probe (a running
gateway) are adopted on their first context switch, fork, or socket/file op.

**Attach types: tracepoints, LSM hooks, and fexit trampolines — no
kprobes.** LSM (`socket_connect`, `socket_sendmsg`) carries exact
before-the-fact semantics and is a stable security API; fexit supplies what
LSM never sees: actual received bytes (`sock_recvmsg`) and actual file I/O
(`vfs_read`/`vfs_write`). The LSM programs need a kernel with BPF-LSM
available, which modern distros ship; if it isn't, the probe reports
`agentcap_probe_up 0` and the app stays up rather than crashing.

Egress is split by rate: rare lifecycle events (fork/exec/exit, with names)
stream over a ring buffer; high-rate sums (CPU ns, socket and file bytes)
live in a `{agent, slot}`-keyed hash the collector polls once a second.

### Audit: domains and destination ports

For security review, the probe also streams, per agent, the **destination
port** of every inet connect and the **domain** of every name lookup it can
see. To keep the kernel side verifier-safe, the probe only filters and
copies raw bytes; the collector parses them in JavaScript, where loops are
free. Two lookup paths are covered:

- **Port-53 DNS** — direct resolver traffic (c-ares, aiodns, `dig`): the
  DNS packet head is shipped and the qname decoded from label format.
- **nss-resolved varlink** — glibc `getaddrinfo` on systemd hosts resolves
  over a unix socket, never touching port 53. The probe recognizes the
  varlink `ResolveHostname` JSON on tracked tasks' unix sends and lifts the
  name.

**DoH/DoT are invisible** — an agent resolving over its own HTTPS/TLS
channel shows only as a `:443` connect, not a domain. That's a real limit
of watching from the kernel, documented rather than papered over.

## Metrics

All labeled `agent`; execs also carry `comm` (the tool that ran, capped at
50 distinct values per agent, then folded into `other`).

| Metric | Type | Meaning |
|---|---|---|
| `agentcap_tasks` | gauge | tracked tasks alive per agent tree |
| `agentcap_execs_total` | counter | binaries exec'd (tools the agent ran) |
| `agentcap_forks_total` / `agentcap_exits_total` | counter | process churn |
| `agentcap_cpu_seconds_total` | counter | on-CPU time of the tree |
| `agentcap_net_connects_total` | counter | inet socket connects |
| `agentcap_net_transmit_bytes_total` / `..._receive_bytes_total` | counter | inet socket traffic (unix sockets excluded) |
| `agentcap_file_read_bytes_total` / `..._write_bytes_total` | counter | actual VFS bytes |
| `agentcap_net_connections_total` | counter | inet connects by destination `port` — the audit view |
| `agentcap_dns_queries_total` | counter | lookups by `domain` (see DNS note below) |
| `agentcap_last_activity_timestamp_seconds` | gauge | unix time of last lifecycle event |
| `agentcap_probe_up` | gauge | BPF object loaded and attached |
| `agentcap_events_dropped_total` | counter | ring-buffer backpressure |

`yeet_worker_up` is prepended by the renderer: 0 means the scrape route is
alive but the collector worker is not.

## Setup

It works out of the box: the common agents — OpenClaw, Claude Code, Codex,
Gemini, aider, opencode, goose, cline, continue, cursor, qwen, crush, amp,
grok, omp, pi — are pre-filled in [`src/agents.txt`](src/agents.txt), one
comm prefix per line. Edit that file to change what's watched.

### 0. Install yeet and log in

```sh
curl -fsSL https://yeet.cx | sh    # installs the yeet CLI + the yeetd daemon
yeet login                         # authenticate this host
yeet status                        # must print "Status: Ok."
```

`yeetd` is the daemon the installer sets up; it does the eBPF loading and runs
the service. Docker + Python 3 are the only other things you need, for the
Prometheus/Grafana stack.

### 1. One-shot

```sh
make up
```

That builds the probe, imports and starts the yeet service on
`127.0.0.1:9464`, and brings up Prometheus + Grafana. It prints the URLs when
done. Then:

- **Dashboard:** <http://localhost:3000/d/agentcap/agent-activity>
- **Raw metrics:** `make metrics` (or `curl -s http://127.0.0.1:9464/metrics`)
- **Tear down:** `make down`

### 2. Or step by step

```sh
make                 # compile bin/probe.bpf.o
sudo make veristat   # optional: verify every program loads on this kernel
make deploy          # (re)import + start the service   → "deployed."
make metrics         # expect: agentcap_probe_up 1, agentcap_tasks{...}, …
make obs-up          # Prometheus :9091, Grafana :3000 (dashboard auto-provisioned)
```

`make deploy` tears down any previous instance and re-imports — it's also how
you pick up edits to `src/` (the daemon copies unit scripts at import time).

The service (`service.toml`) is three units: **keeper** (eager) holds the
collector shared worker — and the BPF probe and metric registry inside it —
alive; **scrape** (lazy, per-connection) renders one exposition document per
request over the console portal; **web** binds `127.0.0.1:9464` and mounts
`/metrics`.

### Changing the agent set

Edit [`src/agents.txt`](src/agents.txt) — one comm prefix per line, `#`
comments and blank lines ignored — then `make deploy`. Or override for one
run without editing anything:

```sh
make dev AGENTS=openclaw,claude,mybot   # standalone, live dump, no HTTP
```

## Prometheus + Grafana

`make obs-up` runs `deploy/docker-compose.yml` (Prometheus on **:9091** —
9090 is often Cockpit's — and Grafana on **:3000**).

Both run with host networking so Prometheus can reach the loopback-only
exporter. The **Agent Activity** dashboard (`deploy/grafana/dashboards/`)
is provisioned automatically and filterable by agent, with a stable color
per agent across every panel. It has four sections:

- **Overview** — hero stats, tracked tasks, tool-exec rates, a "which agent
  ran what" table, top tools, CPU / network / file-I/O / churn timeseries.
- **Audit — who talked to what** — per-agent domain and destination-port
  tables (ports classified and color-coded: HTTPS/DNS green, SSH/SMTP
  amber, telnet/RDP red, unknown neutral), plus DNS and connection rates.
- **Activity mix** — CPU-share and egress-share donuts, a tool-launch bar
  chart, an active/idle **state-timeline** across agents, and per-agent CPU
  gauges — varied forms for a fast read.
- **Agent detail — $agent** — a repeated, collapsed row per selected agent
  with its own tools, lifecycle, CPU/tasks, network, domains and ports.

![Agent Activity dashboard — activity over time: fleet activity clock heatmap, per-agent load band, and the agent detail row](docs/dashboard-activity.png)

The dashboard is generated by `deploy/grafana/gen-dashboard.py` (edit the
Python, rerun it, Grafana's file provider reloads within 30s) so the panel
boilerplate and the per-agent color mapping stay consistent.

### Already running Grafana / Prometheus?

Skip the bundled stack — run only the exporter and wire it into what you
have:

```sh
make deploy        # just the exporter on 127.0.0.1:9464 (no docker)
```

1. **Prometheus** — add a scrape job (the exporter listens on loopback, so
   Prometheus must reach `127.0.0.1`; if it runs elsewhere, expose the port
   or scrape from the same host):

   ```yaml
   scrape_configs:
     - job_name: agentcap
       static_configs:
         - targets: ["127.0.0.1:9464"]
   ```

2. **Grafana** — make sure a Prometheus datasource exists, then import
   `deploy/grafana/dashboards/agent-activity.json` (Dashboards → New →
   Import). The dashboard references a datasource by uid `prometheus`; if
   yours differs, the import screen lets you pick it, or set a `DS_PROMETHEUS`
   default. Nothing else is needed — no docker, no provisioning files.

To refresh the dashboard later, re-import the same JSON (regenerate it first
with `make dashboard` if you've edited the generator).

## CI

- `.github/workflows/ci.yml` — BPF object builds, JS syntax, `service.toml`
  parses, `promtool check config`, dashboard JSON lint, compose config.
- `.github/workflows/kernel-matrix.yml` — boots 6.1 / 6.6 / 6.12 / bpf-next
  in VMs and runs veristat against `bin/probe.bpf.o`; a rejection on an old
  kernel is the signal of the minimum supported kernel (LSM + fexit programs
  need BTF and `CONFIG_BPF_LSM`). Same check locally: `make veristat-matrix`.

## Layout

```
src/bpf/agentcap.bpf.c   the probe: sched tracepoints + LSM + fexit
src/agents.txt           the tracked agent list (one comm prefix per line)
src/agents.js            parses agents.txt into the prefix array
src/collector.js         shared worker: fills kernel maps, owns the
                         Telemetry registry, polls counters, serve()s scrapes
src/scrape.js            per-request /metrics renderer (console portal)
src/main.js              eager keeper — holds the worker (and probe) alive
service.toml             yeet service: units, web server, /metrics route
deploy/                  prometheus.yml, docker-compose, grafana provisioning
Makefile                 build + run/deploy lifecycle (make up, deploy, …)
```
