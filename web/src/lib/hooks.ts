/**
 * Data hooks.
 *
 * Loading and error are first-class in the return type, not optional extras.
 * Views are required to render all three states because a chart that shows an
 * empty grid during a failed fetch is indistinguishable from a chart showing
 * real emptiness — precisely the ambiguity section 36 rules out.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface Async<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
  lastUpdated: number | null;
}

export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[] = [],
  pollMs?: number,
): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [tick, setTick] = useState(0);
  // Guards against a slow earlier response overwriting a newer one.
  const seq = useRef(0);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const mine = ++seq.current;
    let cancelled = false;
    setLoading(true);
    fn()
      .then((d) => {
        if (cancelled || mine !== seq.current) return;
        setData(d);
        setError(null);
        setLastUpdated(Date.now());
      })
      .catch((e) => {
        if (cancelled || mine !== seq.current) return;
        setError(e?.message ?? String(e));
      })
      .finally(() => {
        if (!cancelled && mine === seq.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  useEffect(() => {
    if (!pollMs) return;
    const id = setInterval(refresh, pollMs);
    return () => clearInterval(id);
  }, [pollMs, refresh]);

  return { data, error, loading, refresh, lastUpdated };
}

/** Debounce a rapidly changing value (search boxes, sliders). */
export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Persist small UI preferences (panel sizes, density) across reloads. */
export function useLocalState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (next: T) => {
      setV(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* private mode / quota — preference simply won't persist */
      }
    },
    [key],
  );
  return [v, set];
}

/** Measure an element, for canvas surfaces that must fill their container. */
export function useSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ width, height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size] as const;
}
