/**
 * Brain bridge — fulfills BrainRequest via OpenCode's selected model.
 *
 * Default is inherit: use whatever model the user selected in OpenCode.
 * No provider/model is configured here — the model is injected as modelParam
 * from the current session's resolved model.
 *
 * The worker sends { type: "brain_request", request: BrainRequest } over stdio.
 * The plugin answers with { type: "brain_response", response: BrainResponse }.
 */

import type { BrainRequest, PolicyMode } from "./types.js";

const POLICY_TEMPLATES: Record<string, string> = {
  "mutation-generation":
    "You are an expert code mutator in an evolutionary loop. Propose a SMALL targeted patch. Prefer unified diff.",
  "adversarial-review":
    "You are an adversarial reviewer. Critique the candidate patch for correctness and regression risk.",
  "search-planning":
    "You are a search planner. Suggest the next mutation operator and search region.",
  "experiment-analysis":
    "You are an experiment analyst. Summarize progress: fitness deltas, novelty, Pareto status.",
  "architecture-mutation":
    "You are an architecture mutator. Propose structural changes with trade-off justification.",
  research: "You are a researcher. Surface relevant techniques for the objective.",
  "code-review": "You are a code reviewer. Check interface contracts and type correctness.",
  evaluation: "You are a semantic judge. Assess correctness conservatively.",
  general: "You are a helpful assistant in an evolutionary optimization runtime.",
};

function buildPrompt(req: BrainRequest): { system: string; user: string } {
  const policy = (req.policy ?? "general") as string;
  const sysBase = POLICY_TEMPLATES[policy] ?? POLICY_TEMPLATES.general;
  const sysParts: string[] = [sysBase];
  if (req.objective) sysParts.push(`OBJECTIVE:\n${req.objective}`);
  if (req.constraints) sysParts.push(`CONSTRAINTS:\n${JSON.stringify(req.constraints, null, 2)}`);
  if (req.required_output_schema) {
    sysParts.push(
      `You MUST produce output matching this JSON schema:\n${JSON.stringify(req.required_output_schema, null, 2)}`
    );
  }
  const system = sysParts.join("\n\n");

  const userParts: string[] = [];
  if (req.mutation_strategy) userParts.push(`MUTATION STRATEGY: ${req.mutation_strategy}`);
  if (req.parent_code) userParts.push(`PARENT PROGRAM (apply a small patch to this):\n\`\`\`\n${req.parent_code}\n\`\`\``);
  if (req.parent_metrics) userParts.push(`PARENT METRICS: ${JSON.stringify(req.parent_metrics)}`);
  if (req.context && Object.keys(req.context).length) {
    userParts.push(`CONTEXT:\n${Object.entries(req.context).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n")}`);
  }
  const user = userParts.length ? userParts.join("\n\n") : req.objective || "Proceed.";
  return { system, user };
}

export type BrainResponse = {
  content: string;
  structured?: Record<string, unknown> | null;
  usage?: Record<string, number>;
  latency_ms?: number | null;
  model_meta?: Record<string, unknown>;
  reasoning_tokens?: number | null;
  truncated?: boolean;
  error?: string | null;
  ok: boolean;
};

export type OpenCodeSessionPrompt = (opts: {
  sessionID: string;
  parts: { type: "text"; text: string }[];
  model?: { providerID: string; modelID: string };
  format?: { type: "json_schema"; schema: unknown };
}) => Promise<{ data: { info?: { structured_output?: unknown; error?: { name?: string; message?: string } }; parts?: { type: string; text?: string }[] } }>;

export async function fulfillBrainRequest(
  request: BrainRequest,
  ctx: {
    // Call OpenCode's LLM via session.prompt (injected so this module stays host-agnostic)
    sessionPrompt: OpenCodeSessionPrompt;
    sessionID: string;
    // Current model selected in OpenCode — injected as model param (inherit)
    model?: { providerID: string; modelID: string };
    capabilities?: { structured_output?: boolean };
  }
): Promise<BrainResponse> {
  const t0 = Date.now();
  const { system, user } = buildPrompt(request);

  // Combine system + user into parts — OpenCode's session.prompt takes parts with optional system
  // We use a single user message that includes system context explicitly, since the SDK's
  // session.prompt system handling varies by version. The policy instruction is prepended.
  const fullPrompt = system ? `${system}\n\n---\n\n${user}` : user;

  const parts: { type: "text"; text: string }[] = [{ type: "text", text: fullPrompt }];

  const promptBody: Parameters<OpenCodeSessionPrompt>[0] & { format?: unknown } = {
    sessionID: ctx.sessionID,
    parts,
    ...(ctx.model ? { model: ctx.model } : {}),
    ...(request.required_output_schema && ctx.capabilities?.structured_output
      ? {
          format: {
            type: "json_schema",
            schema: request.required_output_schema,
          },
        }
      : {}),
  };

  try {
    const res = await ctx.sessionPrompt(promptBody as Parameters<OpenCodeSessionPrompt>[0]);
    const latency_ms = Date.now() - t0;

    // Check structured output
    const structured = res.data?.info?.structured_output as Record<string, unknown> | undefined;
    if (structured && typeof structured === "object") {
      return {
        content: JSON.stringify(structured, null, 2),
        structured,
        usage: {},
        latency_ms,
        model_meta: ctx.model ? { provider: ctx.model.providerID, model: ctx.model.modelID } : {},
        ok: true,
      };
    }

    const err = res.data?.info?.error as { name?: string; message?: string } | undefined;
    if (err?.name === "StructuredOutputError") {
      return {
        content: "",
        ok: false,
        error: `structured output failed: ${err.message ?? "unknown"}`,
        latency_ms,
        model_meta: ctx.model ? { provider: ctx.model.providerID, model: ctx.model.modelID } : {},
      };
    }

    // Extract text from parts
    const textParts = (res.data?.parts ?? []).filter((p) => p.type === "text").map((p) => p.text ?? "");
    const content = textParts.join("\n") || "";

    return {
      content,
      usage: {},
      latency_ms,
      model_meta: ctx.model ? { provider: ctx.model.providerID, model: ctx.model.modelID } : {},
      ok: true,
    };
  } catch (e) {
    const latency_ms = Date.now() - t0;
    return {
      content: "",
      ok: false,
      error: String((e as Error)?.message ?? e),
      latency_ms,
      model_meta: ctx.model ? { provider: ctx.model.providerID, model: ctx.model.modelID } : {},
    };
  }
}
