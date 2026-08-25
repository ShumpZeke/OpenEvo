/** Settings: workspace, isolation scope, and where each value comes from. */
import React from "react";
import {
  ViewProps,
} from "../App";
import {
  api, Json,
} from "../lib/api";
import {
  useAsync,
} from "../lib/hooks";
import {
  Badge, KV, Mono, Panel, Table, Td, Th, Row,
} from "../components/ui";

export const Settings: React.FC<ViewProps> = () => {
  const sys = useAsync(() => api.system(), []);
  const health = useAsync(() => api.health(), []);
  const providers = useAsync(() => api.providers(), []);

  return (
    <div className="h-full grid grid-cols-2 gap-2 min-h-0 overflow-auto">
      <Panel title="Control plane" loading={sys.loading} error={sys.error}>
        <div className="p-2">
          <KV k="workspace" v={<span className="break-all">{sys.data?.workspace}</span>} />
          <KV k="collector port" v={health.data?.collector_port ?? "—"} />
          <KV k="uptime" v={health.data?.uptime_s
            ? `${(health.data.uptime_s / 60).toFixed(1)}m` : "—"} />
          <KV k="active runs" v={health.data?.active_runs ?? 0} />
        </div>
      </Panel>

      <Panel title="Provider credentials"
             footer="Only the presence of a credential is shown; values are never read into the UI or telemetry.">
        <Table>
          <thead><tr><Th>Profile</Th><Th>Env var</Th><Th>Status</Th></tr></thead>
          <tbody>
            {(providers.data?.profiles ?? []).map((p: Json) => (
              <Row key={p.id}>
                <Td><Mono>{p.id}</Mono></Td>
                <Td><Mono className="text-ink-dim">{p.secret_ref ?? "—"}</Mono></Td>
                <Td>
                  {!p.secret_ref
                    ? <span className="text-2xs text-ink-faint">not required</span>
                    : p.secret_present
                      ? <Badge tone="ok">set</Badge>
                      : <Badge tone="warn">not set</Badge>}
                </Td>
              </Row>
            ))}
          </tbody>
        </Table>
      </Panel>

      <Panel title="Secret handling" className="col-span-2">
        <div className="p-3 text-2xs text-ink-dim space-y-1.5 max-w-3xl">
          <p>
            Credentials are referenced by environment-variable name, never stored
            in the control-plane database or written into a config file.
          </p>
          <p>
            Every event is passed through redaction before it reaches any sink —
            storage, stream or log. Redaction matches both on key name
            (<Mono>api_key</Mono>, <Mono>authorization</Mono>, …) and on value
            shape (<Mono>sk-…</Mono>, <Mono>nvapi-…</Mono>, bearer tokens, JWTs,
            PEM blocks), and additionally removes any live credential registered
            at startup by exact match.
          </p>
          <p>
            Because redaction runs before persistence rather than at render time,
            a secret that reached a prompt or an error message is removed from
            the stored record itself, not merely hidden in this UI.
          </p>
        </div>
      </Panel>
    </div>
  );
};
