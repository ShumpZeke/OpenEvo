/**
 * OpenEvo OpenCode Plugin — OpenCode is the brain, OpenEvo is the search.
 *
 * Hard boundary:
 *   OpenEvo owns: search, mutation strategies, candidate requests, parent selection,
 *     operator/bandit, archives, novelty, Pareto, failure memory, deterministic gates,
 *     evaluation scheduling, benchmarks, isolation, checkpoint/resume, lineage, budgets, promotion.
 *   OpenCode owns: provider, model, credentials, catalog, reasoning config, harness,
 *     coding/fs/shell tools, session/context, permissions, model switching.
 *
 * Default: brain.mode = inherit — the model selected in OpenCode is the model OpenEvo uses.
 * Roles (EVOLVER/CRITIC/PLANNER etc.) are prompt policies, not providers.
 *
 * Exposed tools:
 *   evolve_start, evolve_status, evolve_inspect, evolve_candidates, evolve_apply,
 *   evolve_pause, evolve_resume, evolve_stop
 *
 * Worker lifecycle owned here: start, health check, request, stream events, cancel, restart, shutdown.
 */

import type { Plugin } from "@opencode-ai/plugin";
import { tool } from "@opencode-ai/plugin";
import { WorkerManager } from "./worker.js";
import { fulfillBrainRequest } from "./brain-bridge.js";
import type { BrainRequest } from "./types.js";

type WorkerHolder = { worker: WorkerManager; sessionID: string; capabilities: Record<string, unknown> | null };

const workers = new Map<string, WorkerHolder>(); // key = directory (project root)

function workerKey(directory: string) {
  return directory;
}

async function getOrCreateWorker(
  directory: string,
  client: Parameters<Plugin>[0]["client"],
  opts: { brainMode?: "inherit" | "legacy" | "null"; sessionID?: string } = {}
): Promise<WorkerHolder> {
  const key = workerKey(directory);
  let holder = workers.get(key);
  if (holder && holder.worker.isAlive()) return holder;

  const brainMode = opts.brainMode ?? "inherit";
  const worker = new WorkerManager({ directory, brainMode });

  // Register brain bridge: when worker asks for LLM, fulfill via OpenCode session
  // We need a session to call session.prompt. Use the caller's session if available,
  // else create a lightweight hidden session for brains that outlive a single evolve.
  let brainSessionID = opts.sessionID ?? null;
  if (!brainSessionID) {
    // Create a dedicated hidden session for brain calls (reused across evolves)
    const s = await client.session.create({ body: { title: "openevo-brain" } });
    brainSessionID = (s.data as { id: string }).id;
  }

  // Resolve current model (inherit) — read from config
  let inheritModel: { providerID: string; modelID: string } | undefined = undefined;
  let hostCaps: Record<string, unknown> | null = null;
  try {
    await client.config.get();
    // Fallback: let session.prompt use server default (no model param = inherit)
  } catch {
    // Let worker use server default
  }
  try {
    const prov = await client.config.providers();
    const data = (prov as { data?: unknown }).data ?? prov;
    const pv = (data as { providers?: unknown[]; default?: Record<string, string> }) ?? {};
    // Expose capabilities if we can infer them; otherwise minimal
    hostCaps = { text: true, streaming: false, structured_output: true, cancellation: true };
    // If defaults hint at a model, use it — otherwise keep undefined to inherit
    if (pv.default) {
      const first = Object.values(pv.default)[0];
      if (typeof first === "string" && first.includes("/")) {
        const [providerID, ...rest] = first.split("/");
        inheritModel = { providerID, modelID: rest.join("/") };
      }
    }
  } catch {
    hostCaps = { text: true };
  }

  holder = { worker, sessionID: brainSessionID!, capabilities: hostCaps };
  workers.set(key, holder);

  // Helper to resolve current model lazily (inherit) — never hardcode, supports switching without restart
  async function resolveInheritModel(): Promise<{ providerID: string; modelID: string } | undefined> {
    try {
      const prov = await client.config.providers();
      const data = (prov as { data?: unknown }).data ?? prov;
      const pv = (data as { providers?: unknown[]; default?: Record<string, string> }) ?? {};
      if (pv.default) {
        const first = Object.values(pv.default)[0];
        if (typeof first === "string" && first.includes("/")) {
          const [providerID, ...rest] = first.split("/");
          return { providerID, modelID: rest.join("/") };
        }
      }
    } catch {}
    return inheritModel; // fallback to initial
  }

  worker.onBrainRequest(async (request, id) => {
    const br = request as BrainRequest;
    // Re-resolve model each call so switching model in OpenCode takes effect without source change
    const currentModel = await resolveInheritModel();
    // If model switched since last call, push update to worker (metadata only, no restart)
    if (currentModel && (currentModel.providerID !== inheritModel?.providerID || currentModel.modelID !== inheritModel?.modelID)) {
      try {
        await worker.request("brain/update", { model: currentModel, capabilities: holder.capabilities }, 5000);
        inheritModel = currentModel;
      } catch {}
    }
    const resp = await fulfillBrainRequest(br, {
      sessionPrompt: async (opts) => {
        const r = await (client.session.prompt as unknown as (a: unknown) => Promise<unknown>)(opts as unknown);
        return r as { data: { info?: { structured_output?: unknown; error?: { name?: string; message?: string } }; parts?: { type: string; text?: string }[] } };
      },
      sessionID: brainSessionID!,
      model: currentModel ?? inheritModel,
      capabilities: { structured_output: !!(hostCaps as Record<string, unknown> | null)?.structured_output },
    });
    worker.sendBrainResponse(id, resp);
  });

  worker.onEvent((evt) => {
    // Concise live surface: generation / candidate / parent / operator / gate / fitness / delta / best / cache / time
    const data = evt.data as Record<string, unknown> | undefined;
    const concise =
      evt.event === "generation_done"
        ? `gen ${String(data?.generation)} cand ${String(data?.candidate ?? "").slice(0, 8)} parent ${String(data?.parent ?? "")} op ${String(data?.operator ?? "")} gate ${String(data?.gate ?? "")} score ${String(data?.score ?? "")} delta ${String(data?.delta ?? "")} best ${String(data?.best_score ?? "")}`
        : evt.event === "improvement"
          ? `IMPROVEMENT gen ${String(data?.generation)} score ${String(data?.score)}`
          : evt.event === "gate"
            ? `gate ${String(data?.stage)} passed=${String(data?.passed)} ${String(data?.reason ?? "")}`
            : evt.event === "eval"
              ? `eval cand ${String(data?.candidate ?? "").slice(0, 8)} score ${String(data?.score)} wall ${String(data?.wall_s)}`
              : evt.event === "bandit"
                ? `bandit ${String(data?.operator)} reward ${String(data?.reward)}`
                : `event ${evt.event}`;
    client.app.log({
      body: {
        service: "openevo",
        level: evt.event.includes("failed") || evt.event.includes("error") ? "warn" : "info",
        message: concise,
        extra: { event: evt.event, data: data as unknown as Record<string, unknown> },
      },
    });
    // Also surface as toast for improvement
    if (evt.event === "improvement") {
      client.app.log({
        body: { service: "openevo", level: "info", message: concise, extra: {} },
      });
    }
  });

  await worker.start();

  // Push capabilities to worker so its BrainPort can cache them
  // (worker hello already sent minimal caps; this upgrades them)
  // We do it lazily on next evolve/start via params.capabilities

  return holder;
}

export const OpenEvoPlugin: Plugin = async ({ client, directory }) => {
  return {
    tool: {
      evolve_start: tool({
        description:
          "Start an OpenEvo evolution run. OpenCode's selected model is the brain (inherit). Provide goal/objective and optional config. Returns run_id.",
        args: {
          goal: tool.schema.string().describe("Evolution objective/goal (e.g., optimize function, fix bug)"),
          iterations: tool.schema.number().optional().describe("Max iterations/generations"),
          config_path: tool.schema.string().optional().describe("Path to evolution config YAML"),
          initial_program: tool.schema.string().optional().describe("Path to initial program file"),
          evaluator: tool.schema.string().optional().describe("Path to evaluator file"),
          run_id: tool.schema.string().optional().describe("Optional run ID for resume"),
          brain_mode: tool.schema.enum(["inherit", "legacy", "null"]).optional().describe("Brain mode: inherit (default) uses OpenCode model"),
        },
        async execute(args, ctx) {
          const holder = await getOrCreateWorker(directory, client, {
            brainMode: (args.brain_mode as "inherit" | "legacy" | "null" | undefined) ?? "inherit",
            sessionID: ctx.sessionID,
          });
          const res = (await holder.worker.request("evolve/start", {
            goal: args.goal,
            iterations: args.iterations,
            config_path: args.config_path,
            initial_program: args.initial_program,
            evaluator: args.evaluator,
            run_id: args.run_id,
            brain: { mode: args.brain_mode ?? "inherit" },
            capabilities: holder.capabilities,
          })) as Record<string, unknown>;
          return {
            output: JSON.stringify(res, null, 2),
            title: `evolve started ${res.run_id ?? ""}`,
            metadata: res as Record<string, unknown>,
          };
        },
      }),

      evolve_status: tool({
        description: "Get status of evolution run(s). Pass run_id for one run, or omit for all.",
        args: {
          run_id: tool.schema.string().optional().describe("Run ID"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/status", { run_id: args.run_id })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_inspect: tool({
        description: "Inspect a run: generation, candidate, parent, operator, gates, tests, fitness, novelty, Pareto, budgets.",
        args: {
          run_id: tool.schema.string().optional().describe("Run ID"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/inspect", { run_id: args.run_id })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_candidates: tool({
        description: "List candidates for a run.",
        args: {
          run_id: tool.schema.string().describe("Run ID"),
          limit: tool.schema.number().optional().describe("Max candidates"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/candidates", {
            run_id: args.run_id,
            limit: args.limit,
          })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_apply: tool({
        description: "Apply (promote) a winning candidate to the working tree. Explicit operation — candidates never silently alter the worktree.",
        args: {
          run_id: tool.schema.string().describe("Run ID"),
          candidate_id: tool.schema.string().describe("Candidate ID to apply"),
          dry_run: tool.schema.boolean().optional().describe("If true, do not write, just report patch"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/apply", {
            run_id: args.run_id,
            candidate_id: args.candidate_id,
            dry_run: args.dry_run,
          })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_pause: tool({
        description: "Pause a running evolution.",
        args: {
          run_id: tool.schema.string().describe("Run ID"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/pause", { run_id: args.run_id })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_resume: tool({
        description: "Resume a paused evolution.",
        args: {
          run_id: tool.schema.string().describe("Run ID"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/resume", { run_id: args.run_id })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),

      evolve_stop: tool({
        description: "Stop an evolution run.",
        args: {
          run_id: tool.schema.string().describe("Run ID"),
        },
        async execute(args) {
          const holder = await getOrCreateWorker(directory, client);
          const res = (await holder.worker.request("evolve/stop", { run_id: args.run_id })) as Record<string, unknown>;
          return { output: JSON.stringify(res, null, 2), metadata: res as Record<string, unknown> };
        },
      }),
    },

    // Health event: log that plugin loaded
    event: async ({ event }) => {
      if (event.type === "server.connected") {
        await client.app.log({
          body: {
            service: "openevo",
            level: "info",
            message: "OpenEvo plugin loaded — brain.mode=inherit (OpenCode model drives evolution)",
          },
        });
      }
    },
  };
};
