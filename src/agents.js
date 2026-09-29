// The agent set agentcap tracks. The list itself lives in agents.txt (one
// comm prefix per line) — edit that file, then `make deploy`. This module
// just parses it: yeet loads an imported .txt as its raw string contents,
// so no filesystem access is involved.
import raw from "./agents.txt";

// One prefix per line; strip inline "# …" comments, blanks and whitespace.
export const DEFAULT_AGENTS = raw
  .split("\n")
  .map((line) => line.replace(/#.*$/, "").trim())
  .filter(Boolean);
