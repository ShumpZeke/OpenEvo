# Control Center — design reference

The browser UI over a live evolution run: 20 views, one dense engineering
console. This is the reference for anyone changing how it looks or behaves.

Source: `web/src/` — `views/` (one file per view), `components/ui.tsx` (the
primitives), `lib/api.ts` (the typed client), `lib/hooks.ts` (polling and SSE).

---

## The one rule that shapes everything

**Nothing on screen may be invented.** There are no fixtures in `web/`, and
there must not be. Where the backend has no value, the UI says so — never a
zero, never a dash standing in for a number, never a plausible-looking
placeholder.

This is not a style preference. The entire product is a comparison between
candidates, and a rendered zero that means "not measured" is indistinguishable
from a real zero. Someone reads it as a result.

Two primitives exist for it, and they are the ones to reach for:

```tsx
<Empty>No candidates yet — the run has not produced one.</Empty>
```

`Empty` is the honest blank state. Say *why* it is empty, not just that it is.
"No data" is weaker than "no data yet, because the run has not started".

```tsx
<UnsupportedButton reason={cap.reason}>Pause</UnsupportedButton>
```

`UnsupportedButton` renders a control the backend reports as unsupported:
disabled, struck through, with the backend's **own** reason in the tooltip.
Never hide the control, and never show it as a working button that does nothing.
The reason string comes from `/api/control/capabilities` — it is not written in
the frontend, because the frontend does not know why.

Example of a real reason the API returns:

> Pause is not supported: OpenEvolve has no resumable in-place pause. SIGSTOP
> would leave provider connections and the worker pool in an undefined state.
> Use graceful stop → resume from checkpoint.

That sentence is worth more to an operator than a greyed-out button, and it is
why the pattern exists.

---

## Visual language

A dense console, not a dashboard. Information per pixel is the goal; whitespace
is spent only where it separates meaning.

### Colour

Defined in `web/tailwind.config.js`. Neutral surfaces, semantic accents only —
colour carries meaning here and is never decorative.

| Token | Value | Use |
|---|---|---|
| `surface-0` … `surface-4` | `#0a0c10` → `#252b38` | five elevation steps, darkest is the page |
| `line`, `line-strong` | `#2a3140`, `#3a4356` | separators; `strong` only where it must read as structural |
| `ink`, `ink-dim`, `ink-faint` | `#dbe2ee`, `#8f9bb0`, `#5d6779` | primary, secondary, tertiary text |
| `ok` | `#4ade80` | success, healthy, improved |
| `warn` | `#fbbf24` | degraded, approaching a limit |
| `bad` | `#f87171` | failure, circuit open, error |
| `info` | `#60a5fa` | neutral emphasis, selection |
| `accent` | `#a78bfa` | the current selection or focus target |
| `live` | `#34d399` | a run that is happening *right now* |

`ok`/`warn`/`bad` are a state vocabulary, not a palette. Do not use `bad` for a
destructive-but-fine action, or `ok` for "finished" when the result was poor —
`scoreColor()` in `ui.tsx` is the shared mapping from a score to a colour, so a
score is coloured the same way everywhere.

### Type

| Token | Size / line | Use |
|---|---|---|
| `2xs` | 10 / 14 | table headers, key labels, metadata |
| `xs` | 11 / 16 | table cells, most dense content |
| `sm` | 12 / 18 | body |
| `base` | 13 / 20 | default; the page baseline |

Two families: `sans` (Inter) for prose and chrome, `mono` for anything a machine
produced — ids, code, model names, paths, raw event payloads.

**Numbers are tabular.** The `.tabular` utility sets `font-variant-numeric:
tabular-nums`, and every column of figures uses it. Columns of proportional
digits shift as they update, which in a live view reads as flicker.

### Motion

There is almost none, deliberately. This is a monitoring surface; movement draws
the eye and should therefore mean something changed. `prefers-reduced-motion` is
respected globally in `index.css`.

---

## Primitives

From `web/src/components/ui.tsx`. Prefer these to ad-hoc markup — a view that
rolls its own table is a view that drifts.

| | |
|---|---|
| `Panel` | the standard bordered container with a title |
| `Stat` | one labelled figure |
| `Badge` | a small status chip |
| `Button` / `UnsupportedButton` | actions; the second for unsupported ones |
| `Table` / `Th` / `Td` / `Row` | tables with sticky headers |
| `KV` | a key/value line |
| `Spark` | an inline sparkline |
| `Mono` | monospace inline |
| `Empty` | the honest blank state |

Formatters — `fmtNum`, `fmtScore`, `fmtMs`, `fmtBytes`, `fmtTime`, `fmtAgo`,
`shortId` — exist so the same value never renders two ways in two views. They
also handle the absent case: **use them rather than formatting inline**, or a
missing value becomes `NaN` or `0` on screen, which is the rule above being
broken by accident.

Shared chrome: `CommandPalette` (keyboard navigation), `EventRail` (the live
event stream), `Inspector` (the right-hand detail pane).

---

## The views

Twenty, in `web/src/views/`. Grouped by the question each answers.

**Where is the run?**
`Overview` · `Activity` · `Experiments` · `Metrics` · `Logs` · `Errors`

**What is the search doing?**
`EvolutionGraph` (lineage) · `MapElitesLab` (the archive, with a generation
scrubber) · `IslandsLab` (islands and migration) · `Candidates` (the inspector,
with parent diffs) · `RunComparison`

**What is the machinery doing?**
`Models` (routes, health, route quality) · `Evaluators` · `Traces` ·
`SystemHealth` · `Checkpoints` · `AgentSandbox` · `Memory`

**Elsewhere**
`Settings` · `ClassicVisualizer` (a bridge to upstream's own visualizer, which
is preserved rather than reimplemented and runs on its own port)

### Adding one

1. Add the endpoint to `lib/api.ts` with its types. The view must not fetch
   ad hoc.
2. Use `lib/hooks.ts` for polling or SSE — do not write a `useEffect` fetch
   loop; the shared hooks handle backoff and unmount.
3. Compose from `ui.tsx`. If you need a primitive that does not exist, add it
   there rather than locally.
4. Handle three states explicitly: **loading**, **empty** (with `Empty`, and a
   reason), and **error**. A view with only a success path will render a lie the
   first time the backend is slow.

---

## Data contract

The frontend renders what the API returns and computes as little as possible.
There is no separate view model: `route_table()` on the backend is shaped
exactly as the Models page renders it, on purpose.

That means a display change that needs new data is a backend change too. This is
deliberate — it keeps the "no invented data" rule enforceable, because a number
on screen can always be traced to an emitted event.

Typecheck before pushing:

```bash
cd web && npm run typecheck
```

CI runs it on every push, and the types come from `lib/api.ts`, so a backend
field that disappears surfaces as a type error rather than as `undefined` on
screen.
