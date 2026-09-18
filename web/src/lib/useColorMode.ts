import { useEffect, useState } from "react";

/** The chart palette and the shell's surface tokens must agree.
 *
 * The dark hex values were validated for contrast against the dark surface;
 * rendering them on the light surface (or vice versa) silently voids that
 * validation. So both derive from this one subscription rather than each
 * sampling the media query on their own schedule.
 */
export function useColorMode(): "light" | "dark" {
  const [mode, setMode] = useState<"light" | "dark">(() =>
    window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = (e: MediaQueryListEvent) => setMode(e.matches ? "dark" : "light");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return mode;
}
