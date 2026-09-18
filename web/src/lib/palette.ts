// Validated with dataviz scripts/validate_palette.js. Light mode carries a
// contrast WARN on slots 3-5, which obligates the table-view toggle on every
// chart. Do not substitute hues without re-running the validator.
export const SERIES = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
} as const;

// Reserved. Never reused as a series color -- a status hue must not
// impersonate a station, and `fix` must never render in the red that means
// `blocked`.
export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

// Station kinds, in fixed slot order. Never cycled: a sixth kind would take a
// named slot or fold into "Other", never a generated hue.
export const STATIONS = ["triage", "plan", "implement", "review", "fix"] as const;
export type Station = (typeof STATIONS)[number];

export function stationColor(s: Station, mode: "light" | "dark"): string {
  return SERIES[mode][STATIONS.indexOf(s)];
}

// Every state carries an icon and a word alongside this color -- color alone
// never conveys state.
export const STATE_STATUS: Record<string, keyof typeof STATUS | null> = {
  "done": "good",
  "pr-open": "good",
  "blocked": "critical",
  "human-only": "critical",
  "needs-input": "warning",
  "plan-review": "warning",
  "in-progress": null,
  "review": null,
  "planning": null,
  "queued": null,
  "new": null,
};

export const SURFACE = { light: "#fcfcfb", dark: "#1a1a19" } as const;
export const GRID = { light: "#e1e0d9", dark: "#2c2c2a" } as const;
export const MUTED = "#898781";
