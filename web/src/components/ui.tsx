/**
 * Console primitives.
 *
 * Deliberately small and dense: compact tables, sharp hierarchy, monospace for
 * every identifier, semantic colour only where it carries meaning. No decorative
 * gauges, gradients or motion (SOURCE_OF_TRUTH section 9.2).
 *
 * `Panel` takes explicit `loading`/`error`/`empty` because every data surface in
 * this app must distinguish "still loading", "failed", and "genuinely nothing".
 */

import React from "react";

export const cx = (...p: (string | false | null | undefined)[]) =>
  p.filter(Boolean).join(" ");

// ---------------------------------------------------------------- formatting

export const fmtNum = (n: unknown, digits = 0): string => {
  if (n === null || n === undefined || Number.isNaN(n as number)) return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (Math.abs(v) >= 1e4) return `${(v / 1e3).toFixed(1)}k`;
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
};

export const fmtScore = (n: unknown): string =>
  n === null || n === undefined || Number.isNaN(Number(n))
    ? "—"
    : Number(n).toFixed(5);

export const fmtMs = (ms: unknown): string => {
  if (ms === null || ms === undefined) return "—";
  const v = Number(ms);
  if (!Number.isFinite(v)) return "—";
  if (v < 1000) return `${v.toFixed(0)}ms`;
  if (v < 60000) return `${(v / 1000).toFixed(2)}s`;
  return `${Math.floor(v / 60000)}m${((v % 60000) / 1000).toFixed(0)}s`;
};

export const fmtBytes = (b: unknown): string => {
  if (b === null || b === undefined) return "—";
  const v = Number(b);
  if (!Number.isFinite(v)) return "—";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0, n = v;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)}${u[i]}`;
};

export const fmtTime = (ts: unknown): string => {
  if (!ts) return "—";
  const d = new Date(Number(ts) * (Number(ts) > 1e12 ? 1 : 1000));
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString();
};

export const fmtAgo = (ts: unknown): string => {
  if (!ts) return "—";
  const s = Date.now() / 1000 - Number(ts);
  if (s < 0) return "just now";
  if (s < 60) return `${s.toFixed(0)}s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${(s / 86400).toFixed(1)}d ago`;
};

export const shortId = (id: unknown, n = 8): string =>
  typeof id === "string" && id.length > n ? id.slice(0, n) : String(id ?? "—");

// ------------------------------------------------------------------ elements

export const Mono: React.FC<{ children: React.ReactNode; className?: string;
  title?: string }> = ({ children, className, title }) => (
  <span title={title} className={cx("font-mono text-xs", className)}>{children}</span>
);

export const Panel: React.FC<{
  title?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyLabel?: string;
  footer?: React.ReactNode;
}> = ({ title, actions, children, className, bodyClassName, loading, error,
        empty, emptyLabel = "No data yet", footer }) => (
  <section className={cx("flex flex-col min-h-0 rounded border border-line bg-surface-1",
                          className)}>
    {(title || actions) && (
      <header className="flex items-center justify-between gap-2 px-3 py-1.5
                         border-b border-line bg-surface-2/60 shrink-0">
        <h2 className="text-xs font-semibold tracking-wide text-ink uppercase">{title}</h2>
        <div className="flex items-center gap-1.5">{actions}</div>
      </header>
    )}
    <div className={cx("flex-1 min-h-0 overflow-auto", bodyClassName)}>
      {error ? (
        <div className="p-3 text-xs text-bad font-mono whitespace-pre-wrap">
          <div className="font-semibold mb-1">Request failed</div>
          {error}
        </div>
      ) : loading && empty !== false ? (
        <div className="p-3 text-xs text-ink-faint">Loading…</div>
      ) : empty ? (
        <div className="p-6 text-center text-xs text-ink-faint">{emptyLabel}</div>
      ) : (
        children
      )}
    </div>
    {footer && (
      <footer className="px-3 py-1 border-t border-line text-2xs text-ink-faint shrink-0">
        {footer}
      </footer>
    )}
  </section>
);

export const Stat: React.FC<{
  label: string; value: React.ReactNode; sub?: React.ReactNode;
  tone?: "default" | "ok" | "warn" | "bad" | "info"; title?: string;
}> = ({ label, value, sub, tone = "default", title }) => (
  <div className="px-3 py-2 min-w-0" title={title}>
    <div className="text-2xs uppercase tracking-wide text-ink-faint truncate">{label}</div>
    <div className={cx("font-mono text-lg leading-tight truncate", {
      default: "text-ink", ok: "text-ok", warn: "text-warn",
      bad: "text-bad", info: "text-info",
    }[tone])}>{value}</div>
    {sub && <div className="text-2xs text-ink-dim truncate">{sub}</div>}
  </div>
);

const BADGE_TONES: Record<string, string> = {
  ok: "bg-ok/15 text-ok border-ok/30",
  running: "bg-live/15 text-live border-live/30",
  completed: "bg-ok/15 text-ok border-ok/30",
  failed: "bg-bad/15 text-bad border-bad/30",
  error: "bg-bad/15 text-bad border-bad/30",
  rejected: "bg-warn/15 text-warn border-warn/30",
  warning: "bg-warn/15 text-warn border-warn/30",
  stopped: "bg-ink-faint/15 text-ink-dim border-line-strong",
  cancelled: "bg-ink-faint/15 text-ink-dim border-line-strong",
  pending: "bg-info/10 text-info border-info/30",
  info: "bg-info/15 text-info border-info/30",
  accent: "bg-accent/15 text-accent border-accent/30",
};

export const Badge: React.FC<{ children: React.ReactNode; tone?: string;
  title?: string }> = ({ children, tone = "info", title }) => (
  <span title={title} className={cx(
    "inline-flex items-center px-1.5 py-0.5 rounded border text-2xs font-mono uppercase",
    BADGE_TONES[tone] ?? BADGE_TONES.info)}>
    {children}
  </span>
);

export const Button: React.FC<{
  children: React.ReactNode; onClick?: () => void; disabled?: boolean;
  title?: string; tone?: "default" | "danger" | "primary"; size?: "sm" | "xs";
}> = ({ children, onClick, disabled, title, tone = "default", size = "sm" }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    title={title}
    className={cx(
      "rounded border font-medium transition-colors disabled:opacity-40",
      "disabled:cursor-not-allowed",
      size === "xs" ? "px-1.5 py-0.5 text-2xs" : "px-2 py-1 text-xs",
      tone === "danger"
        ? "border-bad/40 text-bad hover:bg-bad/10"
        : tone === "primary"
        ? "border-info/40 text-info hover:bg-info/10"
        : "border-line-strong text-ink-dim hover:bg-surface-3 hover:text-ink",
    )}
  >
    {children}
  </button>
);

/**
 * A control the backend reports as unsupported.
 *
 * Rendered disabled with the backend's own reason in the tooltip. Section 15
 * requires unsupported commands be disabled *with an explanation* rather than
 * shown as working buttons that do nothing.
 */
export const UnsupportedButton: React.FC<{ children: React.ReactNode;
  reason: string }> = ({ children, reason }) => (
  <span title={`Not supported: ${reason}`} className="inline-flex">
    <button disabled className="rounded border border-line px-2 py-1 text-xs
                                text-ink-faint cursor-not-allowed line-through
                                decoration-ink-faint/50">
      {children}
    </button>
  </span>
);

export const Table: React.FC<{ children: React.ReactNode; className?: string }> =
  ({ children, className }) => (
    <table className={cx("w-full text-xs border-collapse", className)}>{children}</table>
  );

export const Th: React.FC<{ children?: React.ReactNode; className?: string;
  onClick?: () => void; title?: string }> = ({ children, className, onClick, title }) => (
  <th title={title} onClick={onClick}
      className={cx(
        "sticky top-0 z-10 bg-surface-2 text-left font-semibold text-2xs uppercase",
        "tracking-wide text-ink-dim px-2 py-1 border-b border-line whitespace-nowrap",
        onClick && "cursor-pointer hover:text-ink select-none", className)}>
    {children}
  </th>
);

export const Td: React.FC<{
  children?: React.ReactNode; className?: string; title?: string;
  colSpan?: number; style?: React.CSSProperties;
  onClick?: React.MouseEventHandler<HTMLTableCellElement>;
}> = ({ children, className, title, colSpan, style, onClick }) => (
  <td colSpan={colSpan} title={title} style={style} onClick={onClick}
      className={cx("px-2 py-1 border-b border-line/40 align-top", className)}>
    {children}
  </td>
);

export const Row: React.FC<{ children: React.ReactNode; onClick?: () => void;
  selected?: boolean; className?: string }> = ({ children, onClick, selected, className }) => (
  <tr onClick={onClick}
      className={cx(
        "hover:bg-surface-2/70",
        onClick && "cursor-pointer",
        selected && "bg-info/10 hover:bg-info/15",
        className)}>
    {children}
  </tr>
);

/** Colour a score consistently everywhere it appears. */
export function scoreColor(score: number | null | undefined,
                           min = 0, max = 1): string {
  if (score === null || score === undefined || Number.isNaN(score)) return "#3a4356";
  const t = max > min ? Math.max(0, Math.min(1, (score - min) / (max - min))) : 0.5;
  // Perceptually simple blue→green ramp; avoids red/green-only encoding.
  const r = Math.round(30 + t * 40);
  const g = Math.round(80 + t * 140);
  const b = Math.round(190 - t * 90);
  return `rgb(${r},${g},${b})`;
}

export const Spark: React.FC<{ values: number[]; width?: number; height?: number;
  color?: string }> = ({ values, width = 120, height = 24, color = "#60a5fa" }) => {
  if (!values.length) return <span className="text-ink-faint text-2xs">—</span>;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / Math.max(1, values.length - 1)) * width;
    const y = height - ((v - min) / span) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg width={width} height={height} className="overflow-visible">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
};

export const KV: React.FC<{ k: React.ReactNode; v: React.ReactNode;
  mono?: boolean }> = ({ k, v, mono = true }) => (
  <div className="flex justify-between gap-3 py-0.5 border-b border-line/30 last:border-0">
    <span className="text-2xs text-ink-faint shrink-0">{k}</span>
    <span className={cx("text-xs text-ink text-right break-all",
                        mono && "font-mono")}>{v}</span>
  </div>
);

export const Empty: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="p-6 text-center text-xs text-ink-faint">{children}</div>
);
