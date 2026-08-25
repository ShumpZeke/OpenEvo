/**
 * Global command palette (Ctrl/Cmd+K, section 21).
 *
 * Searches the server-side FTS index across candidates, experiments and code,
 * and doubles as a keyboard navigator for every view.
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { api, Json } from "../lib/api";
import { useAsync, useDebounced } from "../lib/hooks";
import { Mono, cx, shortId } from "./ui";

export const CommandPalette: React.FC<{
  runId: string | null;
  onClose: () => void;
  onSelectCandidate: (id: string) => void;
  onNavigate: (v: any) => void;
  navItems: { id: string; label: string; group: string }[];
}> = ({ runId, onClose, onSelectCandidate, onNavigate, navItems }) => {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounced = useDebounced(q, 200);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const search = useAsync(
    () => (debounced.trim().length >= 2
      ? api.search(debounced, runId ?? undefined).catch(() => ({ results: [] }))
      : Promise.resolve({ results: [] as Json[] })),
    [debounced, runId],
  );

  const navMatches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return navItems;
    return navItems.filter((n) => n.label.toLowerCase().includes(needle));
  }, [q, navItems]);

  const results = search.data?.results ?? [];
  const total = navMatches.length + results.length;

  useEffect(() => { setCursor(0); }, [debounced]);

  const activate = (idx: number) => {
    if (idx < navMatches.length) onNavigate(navMatches[idx].id);
    else {
      const r = results[idx - navMatches.length];
      if (r?.entity_type === "candidate") onSelectCandidate(r.entity_id);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/60 flex items-start justify-center pt-24"
         onClick={onClose}>
      <div className="w-[640px] max-w-[92vw] bg-surface-1 border border-line-strong
                      rounded shadow-2xl overflow-hidden"
           onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, total - 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
            if (e.key === "Enter") { e.preventDefault(); activate(cursor); }
          }}
          placeholder="Search candidates, code, experiments — or jump to a view…"
          className="w-full bg-surface-2 px-3 py-2.5 text-sm outline-none
                     border-b border-line placeholder:text-ink-faint"
        />
        <div className="max-h-[420px] overflow-auto">
          {navMatches.length > 0 && (
            <Section label="Views">
              {navMatches.map((n, i) => (
                <Item key={n.id} active={cursor === i}
                      onClick={() => activate(i)}
                      onHover={() => setCursor(i)}>
                  <span className="text-ink">{n.label}</span>
                  <span className="text-2xs text-ink-faint">{n.group}</span>
                </Item>
              ))}
            </Section>
          )}
          {search.loading && debounced.length >= 2 && (
            <div className="px-3 py-2 text-xs text-ink-faint">Searching…</div>
          )}
          {results.length > 0 && (
            <Section label={`Results (${results.length})`}>
              {results.map((r: Json, i: number) => {
                const idx = navMatches.length + i;
                return (
                  <Item key={`${r.entity_type}-${r.entity_id}-${i}`}
                        active={cursor === idx}
                        onClick={() => activate(idx)}
                        onHover={() => setCursor(idx)}>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-2xs uppercase text-accent">{r.entity_type}</span>
                        <Mono className="text-ink">{shortId(r.entity_id, 14)}</Mono>
                      </div>
                      {r.excerpt && (
                        <div className="text-2xs text-ink-faint truncate font-mono">
                          {String(r.excerpt).replace(/[«»]/g, "")}
                        </div>
                      )}
                    </div>
                  </Item>
                );
              })}
            </Section>
          )}
          {!search.loading && debounced.length >= 2 && results.length === 0 && (
            <div className="px-3 py-3 text-xs text-ink-faint">
              No stored records match “{debounced}”.
            </div>
          )}
        </div>
        <div className="px-3 py-1.5 border-t border-line bg-surface-2 text-2xs
                        text-ink-faint flex gap-3">
          <span>↑↓ navigate</span><span>↵ open</span><span>esc close</span>
          <span className="ml-auto">filters: type: status: island: generation:</span>
        </div>
      </div>
    </div>
  );
};

const Section: React.FC<{ label: string; children: React.ReactNode }> =
  ({ label, children }) => (
  <div>
    <div className="px-3 py-1 text-2xs uppercase tracking-wide text-ink-faint
                    bg-surface-2/50 sticky top-0">{label}</div>
    {children}
  </div>
);

const Item: React.FC<{ children: React.ReactNode; active: boolean;
  onClick: () => void; onHover: () => void }> = ({ children, active, onClick, onHover }) => (
  <button onClick={onClick} onMouseEnter={onHover}
          className={cx("w-full flex items-center justify-between gap-3 px-3 py-1.5 text-xs text-left",
            active ? "bg-info/15" : "hover:bg-surface-2")}>
    {children}
  </button>
);
