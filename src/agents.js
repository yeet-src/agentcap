// The agent set agentcap tracks, out of the box. Edit this list — add or
// remove a line — and re-deploy (`make deploy`) to change what's watched.
//
// Each entry is a process-comm PREFIX. Matching is word-boundary aware
// (the collector inserts "name\0" / "name-" / "name_" / "name."), so "pi"
// matches `pi` and `pi-agent` but never `pipewire`. A tracked process's
// whole tree is attributed to it — a `bash`/`node`/`curl` that openclaw or
// claude spawns is counted under that agent.
//
// The kernel side is policy-free; this list is pushed into the BPF LPM
// trie at startup (up to a few hundred entries). Override at run time with
// `make dev AGENTS=a,b,c` or `--agents=a,b,c` without touching this file.
//
// (A runtime file like agents.txt can't be read — yeet scripts run in a
// bare V8 with no filesystem — so the pre-filled list lives here in JS.)
export const DEFAULT_AGENTS = [
  // — coding agents / CLIs —
  "openclaw", // OpenClaw gateway + agents
  "claude", // Claude Code
  "codex", // OpenAI Codex CLI
  "gemini", // Gemini CLI
  "aider", // aider
  "opencode", // opencode
  "goose", // Block goose
  "cline", // Cline
  "continue", // Continue
  "cursor", // Cursor agent / CLI
  "qwen", // qwen-code
  "crush", // Charm crush
  "amp", // Sourcegraph amp
  "grok", // grok CLI
  "omp", // omp
  "pi", // pi
];
