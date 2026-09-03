import { useSyncExternalStore } from "react";

/** The profit/loss colour pair, switchable for colourblind readers. */

export type PlMode = "cn" | "cvd";

let mode: PlMode =
  (localStorage.getItem("gate.pl") as PlMode | null) ?? "cn";
const listeners = new Set<() => void>();

export function setPlMode(next: PlMode) {
  mode = next;
  localStorage.setItem("gate.pl", next);
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void) {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

export function usePlMode(): PlMode {
  return useSyncExternalStore(subscribe, () => mode);
}

/**
 * Chosen for acceptable contrast on both light and dark antd surfaces:
 * the red pole is fixed, the negative pole swaps green for blue in cvd mode.
 */
export function plColors(): { profit: string; loss: string } {
  // Validated light-surface pair (CVD dE 8.6, normal 31.5): up red, down green.
  return mode === "cvd"
    ? { profit: "#dc2626", loss: "#2563eb" }
    : { profit: "#dc2626", loss: "#15803d" };
}
