/** Mirrors oe_max.brain.types for the TS side — keep in sync. */

export type PolicyMode =
  | "mutation-generation"
  | "adversarial-review"
  | "search-planning"
  | "experiment-analysis"
  | "architecture-mutation"
  | "research"
  | "code-review"
  | "evaluation"
  | "general";

export type Operation = "mutate" | "patch" | "review" | "plan" | "analyze" | "evaluate";

export type BrainRequest = {
  operation?: Operation;
  objective?: string;
  context?: Record<string, unknown>;
  parent_code?: string | null;
  parent_id?: string | null;
  parent_metrics?: Record<string, number> | null;
  mutation_strategy?: string | null;
  constraints?: Record<string, unknown> | null;
  required_output_schema?: Record<string, unknown> | null;
  budget?: {
    max_tokens?: number | null;
    max_input_tokens?: number | null;
    timeout_s?: number | null;
    token_budget?: number | null;
    cost_budget?: number | null;
  } | null;
  policy?: PolicyMode;
  session_id?: string | null;
  candidate_id?: string | null;
  extra?: Record<string, unknown>;
};
