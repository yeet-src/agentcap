#!/usr/bin/env python3
"""Generates dashboards/agent-activity.json. Edit here, then:

    python3 deploy/grafana/gen-dashboard.py

Grafana's file provider picks the change up within 30s.
"""
import json
import os

# Entity-stable agent colors (validated categorical order, dark-mode steps).
AGENT_COLORS = {
    "openclaw": "#3987e5",  # blue
    "claude": "#d95926",    # orange
    "codex": "#199e70",     # aqua
    "gemini": "#c98500",    # yellow
    "aider": "#d55181",     # magenta
    "omp": "#008300",       # green
    "pi": "#9085e9",        # violet
    "grok": "#e66767",      # red
    # opencode: falls back to Grafana's cycle (palette holds 8 fixed slots)
}
BLUE = "#3987e5"
# Signature accent per metric family, so each panel reads as its own concern
# (single-series / aggregate panels use these; multi-agent panels keep the
# entity colors above). Drawn from the same validated categorical ramp.
ACCENT = {
    "blue": "#3987e5", "orange": "#d95926", "aqua": "#199e70",
    "yellow": "#c98500", "magenta": "#d55181", "green": "#008300",
    "violet": "#9085e9", "red": "#e66767",
}
DS = {"type": "prometheus", "uid": "prometheus"}
_id = iter(range(1, 300))


def agent_overrides(suffix=""):
    return [
        {"matcher": {"id": "byName", "options": f"{a}{suffix}"},
         "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
        for a, c in AGENT_COLORS.items()
    ]


def ts(title, targets, gridPos, unit="short", overrides=None, fill=18,
       accent=None):
    # accent: a single fixed color for every series (single-series or
    # role-colored panels); default None keeps the entity/agent overrides.
    color = ({"mode": "fixed", "fixedColor": accent} if accent
             else {"mode": "palette-classic"})
    return {
        "id": next(_id), "type": "timeseries", "title": title,
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "color": color, "unit": unit, "min": 0,
                "custom": {"lineWidth": 2, "fillOpacity": fill,
                           "gradientMode": "opacity", "showPoints": "never",
                           "spanNulls": False, "pointSize": 8,
                           "stacking": {"mode": "none"}},
            },
            "overrides": ([] if accent else
                          (overrides if overrides is not None else agent_overrides())),
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [
            {"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf}
            for i, (e, lf) in enumerate(targets)
        ],
    }


def stat(title, expr, gridPos, unit="short", mappings=None, thresholds=None,
         accent=None):
    # accent gives the tile a fixed signature color; thresholds still win
    # when passed (e.g. the probe UP/DOWN tile).
    if thresholds:
        color = {"mode": "thresholds"}
    elif accent:
        color = {"mode": "fixed", "fixedColor": accent}
        thresholds = {"mode": "absolute", "steps": [{"color": accent, "value": None}]}
    else:
        color = {"mode": "thresholds"}
        thresholds = {"mode": "absolute", "steps": [{"color": "text", "value": None}]}
    return {
        "id": next(_id), "type": "stat", "title": title,
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "mappings": mappings or [],
                "thresholds": thresholds, "color": color,
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background_solid" if accent else "value",
            "graphMode": "area", "textMode": "auto",
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "legendFormat": "__auto"}],
    }


def piechart(title, expr, gridPos, legend, description=None):
    return {
        "id": next(_id), "type": "piechart", "title": title,
        **({"description": description} if description else {}),
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {"unit": "short", "color": {"mode": "palette-classic"},
                         "custom": {"hideFrom": {"legend": False, "tooltip": False,
                                                 "viz": False}}},
            "overrides": agent_overrides(),
        },
        "options": {
            "pieType": "donut", "displayLabels": ["percent"],
            "legend": {"displayMode": "table", "placement": "right",
                       "values": ["value", "percent"], "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "legendFormat": legend, "instant": True}],
    }


def barchart(title, expr, gridPos, legend, unit="short", description=None):
    return {
        "id": next(_id), "type": "barchart", "title": title,
        **({"description": description} if description else {}),
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": 0, "color": {"mode": "palette-classic"},
                "custom": {"lineWidth": 1, "fillOpacity": 85, "gradientMode": "none",
                           "axisPlacement": "auto"},
            },
            "overrides": agent_overrides(),
        },
        "options": {
            "orientation": "horizontal", "showValue": "auto", "stacking": "none",
            "xTickLabelRotation": 0, "groupWidth": 0.7, "barWidth": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "legendFormat": legend, "instant": True}],
    }


def gauge(title, expr, gridPos, unit="short", maxv=None, legend="{{agent}}",
          thresholds=None, description=None):
    defaults = {
        "unit": unit, "color": {"mode": "thresholds"},
        "thresholds": thresholds or {"mode": "absolute", "steps": [
            {"color": "#199e70", "value": None},
            {"color": "#c98500", "value": 0.6},
            {"color": "#e66767", "value": 0.9}]},
    }
    if maxv is not None:
        defaults["max"] = maxv
        defaults["min"] = 0
    return {
        "id": next(_id), "type": "gauge", "title": title,
        **({"description": description} if description else {}),
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "showThresholdLabels": False, "showThresholdMarkers": True,
            "orientation": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "legendFormat": legend, "instant": True}],
    }


def state_timeline(title, expr, gridPos, legend="{{agent}}", description=None):
    return {
        "id": next(_id), "type": "state-timeline", "title": title,
        **({"description": description} if description else {}),
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 90},
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": [
                    {"color": "#2d2d2d", "value": None},
                    {"color": "#199e70", "value": 1}]},
                "mappings": [
                    {"type": "value", "options": {"0": {"text": "idle"}}},
                    {"type": "range", "options": {"from": 1, "to": 1e12,
                                                  "result": {"text": "active"}}},
                ],
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True, "showValue": "never", "alignValue": "center",
            "rowHeight": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": [{"refId": "A", "datasource": DS, "expr": expr,
                     "legendFormat": legend}],
    }


def bargauge(title, expr, gridPos, legend, gradient=True, accent=BLUE):
    return {
        "id": next(_id), "type": "bargauge", "title": title,
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {
            "defaults": {
                "unit": "short", "min": 0,
                "color": {"mode": "continuous-GrYlRd" if gradient
                          else "fixed", "fixedColor": accent},
            },
            "overrides": [],
        },
        "options": {
            "displayMode": "gradient" if gradient else "basic",
            "orientation": "horizontal",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showUnfilled": True, "valueMode": "color",
        },
        "targets": [{"refId": "A", "datasource": DS, "instant": True,
                     "expr": expr, "legendFormat": legend}],
    }


rate = lambda m: f'sum by (agent) (rate({m}{{agent=~"$agent"}}[$__rate_interval]))'

# Ports classified for the audit table: expected agent egress is calm,
# anything else is flagged. value-mapped to colored text in the table.
KNOWN_PORTS = {
    "443": ("HTTPS", "green"), "80": ("HTTP", "yellow"),
    "53": ("DNS", "green"), "22": ("SSH", "orange"),
    "6443": ("k8s-api", "yellow"), "5432": ("postgres", "yellow"),
    "3306": ("mysql", "yellow"), "6379": ("redis", "yellow"),
    "11434": ("ollama", "green"), "587": ("smtp", "orange"),
    "25": ("smtp", "orange"), "23": ("telnet", "red"), "3389": ("rdp", "red"),
}
PORT_MAPPINGS = [
    {"type": "value", "options": {
        p: {"text": f"{p} · {name}", "color": color, "index": i}
        for i, (p, (name, color)) in enumerate(KNOWN_PORTS.items())}},
    {"type": "special", "options": {"match": "empty",
     "result": {"text": "—", "index": 99}}},
]


def table(title, expr, gridPos, value_name, rename, index, extra_overrides=None,
          mappings=None, sort_desc=True, description=None, bar_color=BLUE):
    overrides = [{
        "matcher": {"id": "byName", "options": value_name},
        "properties": [
            {"id": "custom.cellOptions",
             "value": {"type": "gauge", "mode": "gradient"}},
            {"id": "color", "value": {"mode": "fixed", "fixedColor": bar_color}},
            {"id": "thresholds", "value": {"mode": "absolute",
             "steps": [{"color": bar_color, "value": None}]}},
            {"id": "decimals", "value": 0},
        ],
    }]
    if extra_overrides:
        overrides += extra_overrides
    defaults = {"unit": "short", "custom": {"align": "auto", "filterable": True}}
    if mappings:
        defaults["mappings"] = mappings
    return {
        "id": next(_id), "type": "table", "title": title,
        **({"description": description} if description else {}),
        "datasource": DS, "gridPos": gridPos,
        "fieldConfig": {"defaults": defaults, "overrides": overrides},
        "options": {"sortBy": [{"displayName": value_name, "desc": sort_desc}],
                    "footer": {"show": False}},
        "targets": [{"refId": "A", "datasource": DS, "instant": True,
                     "format": "table", "expr": expr}],
        "transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True}, "renameByName": rename,
            "indexByName": index}}],
    }


# ---- top-level panels -------------------------------------------------------

panels = [
    stat("Active agent tasks", 'sum(agentcap_tasks{agent=~"$agent"})',
         {"h": 4, "w": 6, "x": 0, "y": 0}, accent=ACCENT["blue"]),
    stat("Agents active", 'count(agentcap_tasks{agent=~"$agent"} > 0) or vector(0)',
         {"h": 4, "w": 6, "x": 6, "y": 0}, accent=ACCENT["violet"]),
    stat("Probe", "agentcap_probe_up",
         {"h": 4, "w": 6, "x": 12, "y": 0},
         mappings=[{"type": "value", "options": {
             "1": {"text": "UP", "color": "green"},
             "0": {"text": "DOWN", "color": "red"}}}],
         thresholds={"mode": "absolute", "steps": [
             {"color": "red", "value": None}, {"color": "green", "value": 1}]}),
    stat("Since last lifecycle event",
         'time() - max(agentcap_last_activity_timestamp_seconds{agent=~"$agent"})',
         {"h": 4, "w": 6, "x": 18, "y": 0}, unit="s", accent=ACCENT["aqua"]),

    ts("Tracked tasks by agent",
       [('agentcap_tasks{agent=~"$agent"}', "{{agent}}")],
       {"h": 8, "w": 12, "x": 0, "y": 4}),
    ts("Tool execs / s", [(rate("agentcap_execs_total"), "{{agent}}")],
       {"h": 8, "w": 12, "x": 12, "y": 4}, unit="ops"),

    # Who did what: agent x tool x count over the dashboard range.
    table("Which agent ran what (session)",
          'sort_desc(sum by (agent, comm) '
          '(agentcap_execs_total{agent=~"$agent"}) > 0)',
          {"h": 8, "w": 8, "x": 0, "y": 12}, "execs",
          {"comm": "tool", "Value": "execs"},
          {"agent": 0, "comm": 1, "Value": 2}, bar_color=ACCENT["blue"],
          extra_overrides=[{"matcher": {"id": "byName", "options": "agent"},
              "properties": [{"id": "custom.cellOptions",
                              "value": {"type": "color-text"}}]}] + agent_overrides(),
          description="Tool launches per agent, cumulative since the exporter started."),
    bargauge("Top tools (all agents)",
             'topk(10, sum by (agent, comm) '
             '(agentcap_execs_total{agent=~"$agent"}))',
             {"h": 8, "w": 8, "x": 8, "y": 12}, "{{agent}} · {{comm}}"),
    ts("CPU by agent (cores)", [(rate("agentcap_cpu_seconds_total"), "{{agent}}")],
       {"h": 8, "w": 8, "x": 16, "y": 12}, unit="none"),

    # Network family: sent/recv/connects share a warm→cool trio so the row
    # reads as one concern distinct from the file row below.
    ts("Network sent", [('sum(rate(agentcap_net_transmit_bytes_total{agent=~"$agent"}[$__rate_interval]))', "sent")],
       {"h": 8, "w": 8, "x": 0, "y": 20}, unit="Bps", accent=ACCENT["blue"]),
    ts("Network received", [('sum(rate(agentcap_net_receive_bytes_total{agent=~"$agent"}[$__rate_interval]))', "received")],
       {"h": 8, "w": 8, "x": 8, "y": 20}, unit="Bps", accent=ACCENT["orange"]),
    ts("Socket connects / s",
       [('sum(rate(agentcap_net_connects_total{agent=~"$agent"}[$__rate_interval]))', "connects/s")],
       {"h": 8, "w": 8, "x": 16, "y": 20}, unit="ops", accent=ACCENT["yellow"]),

    ts("File reads", [('sum(rate(agentcap_file_read_bytes_total{agent=~"$agent"}[$__rate_interval]))', "read")],
       {"h": 8, "w": 8, "x": 0, "y": 28}, unit="Bps", accent=ACCENT["aqua"]),
    ts("File writes", [('sum(rate(agentcap_file_write_bytes_total{agent=~"$agent"}[$__rate_interval]))', "written")],
       {"h": 8, "w": 8, "x": 8, "y": 28}, unit="Bps", accent=ACCENT["magenta"]),
    ts("Process churn / s",
       [(rate("agentcap_forks_total"), "{{agent}} forks"),
        (rate("agentcap_exits_total"), "{{agent}} exits")],
       {"h": 8, "w": 8, "x": 16, "y": 28}, unit="ops",
       overrides=agent_overrides(" forks") + agent_overrides(" exits")),
]

# ---- audit / security section ----------------------------------------------
# A labeled row so it reads as its own concern; colored so anomalies pop.

panels.append({
    "id": next(_id), "type": "row", "title": "Audit — who talked to what",
    "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 36},
    "panels": [],
})
AGENT_CELL = [{"matcher": {"id": "byName", "options": "agent"},
               "properties": [{"id": "custom.cellOptions",
                               "value": {"type": "color-text"}}]}]
MODE_MAP = [{"type": "value", "options": {
    "read": {"text": "read", "color": ACCENT["aqua"], "index": 0},
    "write": {"text": "write", "color": ACCENT["red"], "index": 1}}}]
MODE_CELL = [{"matcher": {"id": "byName", "options": "mode"},
              "properties": [{"id": "mappings", "value": MODE_MAP},
                             {"id": "custom.cellOptions",
                              "value": {"type": "color-text"}}]}]

panels += [
    # Every (agent, domain) pair seen — the core audit artifact.
    table("Domains queried by agent",
          'sort_desc(sum by (agent, domain) '
          '(agentcap_dns_queries_total{agent=~"$agent"}) > 0)',
          {"h": 9, "w": 8, "x": 0, "y": 37}, "queries",
          {"domain": "domain", "Value": "queries"},
          {"agent": 0, "domain": 1, "Value": 2}, bar_color=ACCENT["aqua"],
          extra_overrides=agent_overrides() + AGENT_CELL,
          description="Cumulative since exporter start. Plaintext DNS (port "
                      "53) and nss-resolved lookups; DoH/DoT are not visible."),
    # Destination ports, port-classified so odd egress is red/orange.
    table("Destination ports by agent",
          'sort_desc(sum by (agent, port) '
          '(agentcap_net_connections_total{agent=~"$agent"}) > 0)',
          {"h": 9, "w": 8, "x": 8, "y": 37}, "connects",
          {"port": "port", "Value": "connects"},
          {"agent": 0, "port": 1, "Value": 2}, bar_color=ACCENT["violet"],
          extra_overrides=[
              {"matcher": {"id": "byName", "options": "port"},
               "properties": [{"id": "mappings", "value": PORT_MAPPINGS},
                              {"id": "custom.cellOptions",
                               "value": {"type": "color-text"}}]},
          ] + agent_overrides() + AGENT_CELL,
          description="Well-known ports are named and green/amber; "
                      "unrecognized or risky ports (telnet, rdp) show red."),
    # Files opened, with read/write mode colored.
    table("Files opened by agent",
          'sort_desc(sum by (agent, path, mode) '
          '(agentcap_files_opened_total{agent=~"$agent"}) > 0)',
          {"h": 9, "w": 8, "x": 16, "y": 37}, "opens",
          {"path": "path", "mode": "mode", "Value": "opens"},
          {"agent": 0, "path": 1, "mode": 2, "Value": 3}, bar_color=ACCENT["green"],
          extra_overrides=agent_overrides() + AGENT_CELL + MODE_CELL,
          description="Regular files opened per agent (writes always kept; "
                      "noisy read-only system/library paths filtered out)."),
    # Totals in each family's signature hue (per-agent breakdown is the table
    # beside each), so this row is aqua / violet / green, not blue+orange.
    ts("DNS query rate (total)",
       [('sum(rate(agentcap_dns_queries_total{agent=~"$agent"}[$__rate_interval]))', "queries/s")],
       {"h": 7, "w": 8, "x": 0, "y": 46}, unit="ops", accent=ACCENT["aqua"]),
    ts("New connections / s (total)",
       [('sum(rate(agentcap_net_connections_total{agent=~"$agent"}[$__rate_interval]))', "conns/s")],
       {"h": 7, "w": 8, "x": 8, "y": 46}, unit="ops", accent=ACCENT["violet"]),
    ts("File opens / s (total)",
       [('sum(rate(agentcap_files_opened_total{agent=~"$agent"}[$__rate_interval]))', "opens/s")],
       {"h": 7, "w": 8, "x": 16, "y": 46}, unit="ops", accent=ACCENT["green"]),
]

# ---- activity mix: varied chart forms, one glance --------------------------
panels.append({
    "id": next(_id), "type": "row", "title": "Activity mix",
    "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": 53},
    "panels": [],
})
panels += [
    piechart("CPU share by agent",
             'sum by (agent) (rate(agentcap_cpu_seconds_total{agent=~"$agent"}[$__range]))',
             {"h": 9, "w": 8, "x": 0, "y": 54}, "{{agent}}",
             description="On-CPU time split across agents over the range."),
    barchart("Tool launches by agent (range)",
             'sum by (agent) (increase(agentcap_execs_total{agent=~"$agent"}[$__range]))',
             {"h": 9, "w": 8, "x": 8, "y": 54}, "{{agent}}",
             description="Total execs each agent tree performed."),
    piechart("Egress bytes share by agent",
             'sum by (agent) (increase(agentcap_net_transmit_bytes_total{agent=~"$agent"}[$__range]))',
             {"h": 9, "w": 8, "x": 16, "y": 54}, "{{agent}}"),
    state_timeline("Agent active / idle timeline",
                   'clamp_max(sum by (agent) (agentcap_tasks{agent=~"$agent"}), 1)',
                   {"h": 8, "w": 24, "x": 0, "y": 63}, "{{agent}}",
                   description="Green where the agent tree had a live task."),
    gauge("CPU cores in use by agent (now)",
          'sum by (agent) (rate(agentcap_cpu_seconds_total{agent=~"$agent"}[$__rate_interval]))',
          {"h": 7, "w": 24, "x": 0, "y": 71}, unit="none", maxv=1,
          thresholds={"mode": "absolute", "steps": [
              {"color": "#199e70", "value": None},
              {"color": "#c98500", "value": 0.5},
              {"color": "#e66767", "value": 0.85}]},
          description="Per-agent CPU as a fraction of one core."),
]

# ---- per-agent detail: one collapsed row per selected agent -----------------
# Inside a repeated row $agent is the single value of that instance, so
# queries pin agent="$agent" and the row title names the agent.

FIXED = [  # stable roles inside a detail row (not entity colors)
    ("#3987e5", "execs"), ("#199e70", "forks"), ("#e66767", "exits"),
]
DY = 79  # detail panels sit under the row header at y=78
detail_panels = [
    bargauge("Tools run — $agent",
             'topk(12, sum by (comm) '
             '(agentcap_execs_total{agent="$agent"}))',
             {"h": 8, "w": 8, "x": 0, "y": DY}, "{{comm}}"),
    ts("Lifecycle — $agent",
       [('sum(rate(agentcap_execs_total{agent="$agent"}[$__rate_interval]))', "execs"),
        ('sum(rate(agentcap_forks_total{agent="$agent"}[$__rate_interval]))', "forks"),
        ('sum(rate(agentcap_exits_total{agent="$agent"}[$__rate_interval]))', "exits")],
       {"h": 8, "w": 8, "x": 8, "y": DY}, unit="ops",
       overrides=[
           {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color",
                            "value": {"mode": "fixed", "fixedColor": color}}]}
           for color, name in FIXED
       ]),
    ts("CPU & tasks — $agent",
       [('sum(rate(agentcap_cpu_seconds_total{agent="$agent"}[$__rate_interval]))', "cores"),
        ('sum(agentcap_tasks{agent="$agent"})', "tasks")],
       {"h": 8, "w": 8, "x": 16, "y": DY}, unit="none",
       overrides=[
           {"matcher": {"id": "byName", "options": "cores"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#3987e5"}}]},
           {"matcher": {"id": "byName", "options": "tasks"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#9085e9"}}]},
       ]),
    ts("Network — $agent",
       [('sum(rate(agentcap_net_transmit_bytes_total{agent="$agent"}[$__rate_interval]))', "sent"),
        ('sum(rate(agentcap_net_receive_bytes_total{agent="$agent"}[$__rate_interval]))', "received")],
       {"h": 8, "w": 8, "x": 0, "y": DY + 8}, unit="Bps",
       overrides=[
           {"matcher": {"id": "byName", "options": "sent"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#3987e5"}}]},
           {"matcher": {"id": "byName", "options": "received"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#d95926"}}]},
       ]),
    table("Domains — $agent",
          'sort_desc(sum by (domain) '
          '(agentcap_dns_queries_total{agent="$agent"}) > 0)',
          {"h": 8, "w": 8, "x": 8, "y": DY + 8}, "queries",
          {"domain": "domain", "Value": "queries"}, {"domain": 0, "Value": 1},
          bar_color=ACCENT["aqua"]),
    table("Ports — $agent",
          'sort_desc(sum by (port) '
          '(agentcap_net_connections_total{agent="$agent"}) > 0)',
          {"h": 8, "w": 8, "x": 16, "y": DY + 8}, "connects",
          {"port": "port", "Value": "connects"}, {"port": 0, "Value": 1},
          bar_color=ACCENT["violet"],
          extra_overrides=[{
              "matcher": {"id": "byName", "options": "port"},
              "properties": [{"id": "mappings", "value": PORT_MAPPINGS},
                             {"id": "custom.cellOptions",
                              "value": {"type": "color-text"}}]}]),
    table("Files opened — $agent",
          'sort_desc(sum by (path, mode) '
          '(agentcap_files_opened_total{agent="$agent"}) > 0)',
          {"h": 8, "w": 24, "x": 0, "y": DY + 16}, "opens",
          {"path": "path", "mode": "mode", "Value": "opens"},
          {"path": 0, "mode": 1, "Value": 2}, bar_color=ACCENT["green"],
          extra_overrides=MODE_CELL),
]

panels.append({
    "id": next(_id), "type": "row", "title": "Agent detail — $agent",
    "repeat": "agent", "collapsed": True,
    "gridPos": {"h": 1, "w": 24, "x": 0, "y": 78},
    "panels": detail_panels,
})

dashboard = {
    "uid": "agentcap",
    "title": "Agent Activity",
    "description": "AI-agent process-tree activity captured by the agentcap eBPF exporter (yeet).",
    "tags": ["agents", "ebpf", "yeet"],
    "schemaVersion": 39, "version": 6, "editable": True, "graphTooltip": 1,
    "time": {"from": "now-1h", "to": "now"},
    "refresh": "10s",
    "templating": {"list": [{
        "name": "agent", "label": "Agent", "type": "query", "datasource": DS,
        "query": {"query": "label_values(agentcap_tasks, agent)", "refId": "var"},
        "refresh": 2, "includeAll": True, "multi": True,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "sort": 1,
    }]},
    "panels": panels,
    "annotations": {"list": []},
}

out = os.path.join(os.path.dirname(__file__), "dashboards", "agent-activity.json")
with open(out, "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")
print(f"wrote {out} — {len(panels)} top-level panels "
      f"(+{len(detail_panels)} per agent-detail row)")
