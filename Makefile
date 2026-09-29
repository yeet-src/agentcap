# Build and run the agentcap exporter.
#
# Build:
#   make          — compile the BPF object (bin/probe.bpf.o)
#   make veristat — verifier check on this kernel (needs sudo)
#   make clean    — remove build artifacts
#
# Run the exporter (yeet service on 127.0.0.1:9464):
#   make check    — preflight: verify yeet, the daemon, login, docker
#   make deploy   — build + (re)import the service and start it
#   make start / stop / restart / status / remove
#   make metrics  — curl the /metrics endpoint
#   make dev      — run the collector standalone (live dump, no HTTP)
#
# Observability stack (Prometheus + Grafana in Docker):
#   make obs-up / obs-down     — bring the stack up / down
#   make wire                  — print how to plug into an existing Grafana
#   make dashboard             — regenerate the Grafana dashboard JSON
#
# One-shot (pick your path):
#   make exporter — exporter only, no Docker (pair with `make wire`)
#   make grafana  — exporter + Dockerized Prometheus & Grafana (needs Docker)
#   make down     — stop the stack and the service
#
# This is the build *frontend*: it orchestrates two independent
# compilers — clang for the BPF objects, esbuild for the JS bundle.
# Neither understands the other; the JS references compiled objects in
# bin/ only by path, resolved at runtime. `yeet run` invokes `make`
# automatically when running this project from a trusted remote source,
# so the default goal must leave the project runnable.
#
# clang, bpftool and esbuild come from the static toolchain resolved by
# build/toolchain.mk (a shared per-machine cache, or binaries vendored in
# the bootstrap repo) — so the build needs no system C/BPF toolchain.

.DEFAULT_GOAL := all

include build/toolchain.mk
include build/bpf.mk

# The service scripts (src/*.js) import only `yeet:*` builtins and relative
# siblings, so there is no JS bundle step — `all` is the BPF object alone.
all: bpf

# Bundle the entry with the vendored esbuild. esbuild honors tsconfig `paths`
# (so `@/` resolves at bundle time), while `yeet:*` builtins and `*.bpf.o`
# objects stay external. The bundle is written to src/index.jsx, which the
# entry ladder prefers over src/main.jsx — so once built, that is what runs.
# The .jsx extension keeps the bundle eligible for component auto-mount.
# Compiled BPF objects in bin/ are loaded by path at runtime, never imported,
# so they are not bundled.
#
# The build needs no npm/node: the starter imports only `yeet:*` builtins and
# local `@/` modules, which esbuild resolves on its own. If you add third-party
# packages to package.json, install them into node_modules with the package
# manager of your choice — esbuild inlines whatever it finds there.
ESBUILD_FLAGS := --bundle --format=esm --platform=neutral \
	--main-fields=module,main --conditions=import,module \
	--define:import.meta.main=false \
	--outfile=src/index.jsx --jsx=automatic --jsx-import-source=yeet:tui

bundle: | toolchain
	$(ESBUILD) src/main.jsx $(ESBUILD_FLAGS) '--external:yeet:*' '--external:*.bpf.o'

# Post-generation finalize: initialize a git repository with the vendored git
# (fetched via `vendored-git`). Idempotent — skipped if this is already a repo.
# The scaffolders (`yeet new`, `scripts/new`) run `make postgen` after creating
# the project, so the CLI itself stays a thin caller of make.
postgen: | vendored-git
	@g="$(GIT)"; [ -x "$$g" ] || g="$$(command -v git 2>/dev/null || true)"; \
	if [ -e .git ]; then \
		echo "postgen: already a git repository"; \
	elif [ -n "$$g" ]; then \
		echo "postgen: git init"; \
		"$$g" -c init.templateDir= init -q . || echo "warning: 'git init' failed" >&2; \
	else \
		echo "warning: no git available (vendored or host); skipping 'git init'" >&2; \
	fi

clean: clean-bpf
	rm -rf node_modules dist src/index.jsx

.PHONY: all bundle clean postgen

# ---------------------------------------------------------------------------
# Run the exporter as a yeet service.
# ---------------------------------------------------------------------------
SERVICE  := agentcap
COMPOSE  := docker compose -f deploy/docker-compose.yml
METRICS  := http://127.0.0.1:9464/metrics

# Preflight: verify the environment before doing anything. Prints ✓ / ✗ with
# a fix hint for each item; exits non-zero if a hard requirement is missing.
# `make up` runs this first.
check:
	@ok=1; echo "agentcap preflight:"; \
	if command -v yeet >/dev/null 2>&1; then echo "  ✓ yeet CLI installed"; \
	else echo "  ✗ yeet CLI missing        → curl -fsSL https://yeet.cx | sh"; ok=0; fi; \
	if yeet status >/dev/null 2>&1; then echo "  ✓ yeetd daemon reachable"; \
	else echo "  ✗ yeetd not reachable     → the yeet installer sets it up; see yeet status"; ok=0; fi; \
	if yeet whoami -q >/dev/null 2>&1; then echo "  ✓ logged in to yeet"; \
	else echo "  ✗ not logged in           → yeet login"; ok=0; fi; \
	if command -v docker >/dev/null 2>&1; then echo "  ✓ docker (Prometheus/Grafana)"; \
	else echo "  • docker not found        → optional; skip it: make deploy && make wire"; fi; \
	if command -v python3 >/dev/null 2>&1; then echo "  ✓ python3 (dashboard gen)"; \
	else echo "  • python3 not found       → optional; only 'make dashboard' needs it"; fi; \
	if [ $$ok -eq 1 ]; then echo "ready → run: make up"; \
	else echo "fix the ✗ items above, then re-run: make check"; exit 1; fi

# (Re)import the service from service.toml and start it. Idempotent: an
# existing service is torn down first (the daemon copies unit scripts at
# import time, so this is also how you pick up edits to src/).
deploy: all
	@command -v yeet >/dev/null || { echo "error: yeet CLI not found on PATH"; exit 1; }
	-@yeet service stop $(SERVICE)   >/dev/null 2>&1
	-@yeet service remove $(SERVICE) >/dev/null 2>&1
	yeet service import service.toml --now
	@echo "deployed. scrape it with:  make metrics"

start:   ; yeet service start $(SERVICE)
stop:    ; yeet service stop $(SERVICE)
restart: ; yeet service restart $(SERVICE)
status:  ; yeet service tree $(SERVICE)
remove:  ; -yeet service stop $(SERVICE) >/dev/null 2>&1; yeet service remove $(SERVICE)

# Scrape the endpoint (curl, else wget) so you can eyeball the exposition.
metrics:
	@curl -fsS $(METRICS) 2>/dev/null || wget -qO- $(METRICS) 2>/dev/null \
		|| { echo "no response from $(METRICS) — is the service up? (make status)"; exit 1; }

# Run the collector standalone: loads the probe, prints a live registry
# dump every few seconds, no HTTP. Ctrl-C to stop. Pass agents like:
#   make dev AGENTS=openclaw,claude,mybot
dev: all
	@if [ -n "$(AGENTS)" ]; then yeet run src/collector.js -- --agents=$(AGENTS); \
	else yeet run src/collector.js; fi

# ---------------------------------------------------------------------------
# Observability stack (Prometheus + Grafana).
# ---------------------------------------------------------------------------
dashboard:
	python3 deploy/grafana/gen-dashboard.py

obs-up:
	@command -v docker >/dev/null 2>&1 || { \
	  echo "docker not found. Docker is only for the bundled stack —"; \
	  echo "already have Grafana/Prometheus? run:  make deploy && make wire"; exit 1; }
	$(COMPOSE) up -d
	@echo "Grafana:    http://localhost:3000  (anonymous admin)"
	@echo "Prometheus: http://localhost:9091"

obs-down:
	$(COMPOSE) down

# Already running Grafana/Prometheus? Skip Docker: `make deploy` then this
# prints exactly how to wire the exporter into what you have.
wire:
	@echo "1) Point your Prometheus at the exporter — add to prometheus.yml:"
	@echo ""
	@echo "   scrape_configs:"
	@echo "     - job_name: agentcap"
	@echo "       static_configs:"
	@echo "         - targets: [\"127.0.0.1:9464\"]   # exporter is loopback-only"
	@echo ""
	@echo "2) Import the dashboard into Grafana (Dashboards > New > Import):"
	@echo "   $(CURDIR)/deploy/grafana/dashboards/agent-activity.json"
	@echo "   It references a Prometheus datasource — pick yours on import."

# ---------------------------------------------------------------------------
# One-shot lifecycle — pick your path.
# ---------------------------------------------------------------------------

# Exporter only (no Docker): preflight, build, deploy the yeet service.
# Pair with `make wire` if you already run Grafana/Prometheus.
exporter: check deploy
	@echo "exporter live on $(METRICS)"

# Full stack: the exporter (prerequisite) + Dockerized Prometheus & Grafana
# with the dashboard pre-loaded.
grafana: exporter obs-up
	@echo
	@echo "agentcap is up."
	@echo "  metrics:    $(METRICS)"
	@echo "  dashboard:  http://localhost:3000/d/agentcap/agent-activity"

# Back-compat alias for `make grafana`.
up: grafana

down: obs-down remove

.PHONY: exporter grafana check deploy start stop restart status remove metrics dev \
	dashboard obs-up obs-down wire up down
