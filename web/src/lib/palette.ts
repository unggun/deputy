export const SERIES = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
} as const;

export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

export const STATIONS = ["triage", "plan", "implement", "review", "fix"] as const;
export type Station = (typeof STATIONS)[number];

export const STATE_STATUS: Record<string, keyof typeof STATUS | null> = {
  "done": "good",
  // `serious`, not `good`. Green is reserved for `done`: at `pr-open` the
  // runner has done everything it can and the outcome now rests with CI and
  // a human reviewer, which is not the same thing as finished. The Outcomes
  // chart on the Throughput tab already made this call -- it needs `pr-open`
  // and `done` to be two distinguishable hues in one legend, and green is
  // done's. This badge used to say `good` while that chart painted the same
  // state orange: one state, two colours, one app. They agree now, and they
  // agree on the chart's choice, because aligning the badge introduces no
  // new hue anywhere.
  "pr-open": "serious",
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

// The surface a chart is actually drawn on, which is `--card`, NOT
// `--background`. Every chart lives inside a `<Card>`; the shell's background
// is only ever visible around the cards, never behind a bar. Two things read
// this and both are wrong if it tracks the wrong token: the 2px "surface gap"
// stroke between stacked segments paints a near-miss colour and shows as a
// seam, and the contrast figures the palette was validated against stop
// describing what is on screen.
//
// Values are `--card` from index.css: light `oklch(1 0 0)` = #ffffff, dark
// `oklch(0.205 0 0)` = #171717. Re-validated against these surfaces: the
// dark palette passes all five slots at >= 3:1; the light palette carries the
// known WARN on three of five (2.82 / 2.17 / 2.69:1), which `ChartFrame`'s
// mandatory table view relieves. Both PASS, so the hex values above are
// unchanged -- only the surface they are measured against is corrected.
export const SURFACE = { light: "#ffffff", dark: "#171717" } as const;
export const GRID = { light: "#e1e0d9", dark: "#2c2c2a" } as const;
export const MUTED = "#898781";
