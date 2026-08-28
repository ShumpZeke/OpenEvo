# Agent Sandbox & OpenCode Isolation

## The boundary

The operator uses OpenCode for unrelated work. Evolution must never hijack,
rewrite, interrupt, migrate, reconfigure or otherwise interfere with it. This is
a hard architectural boundary, not a best-effort courtesy.

**Rule.** Evolution may *execute* an OpenCode binary. Every byte of
configuration, state, cache, session and log it produces is redirected into
`<workspace>/.evolution/opencode/`. Nothing outside that tree is written.

## Enforced by environment, not by policy

`OpenCodeIsolation.env()` builds a **filtered** environment:

```
HOME, USERPROFILE      → <workspace>/.evolution/opencode/home
XDG_CONFIG_HOME        → …/opencode/config
XDG_DATA_HOME          → …/opencode/state
XDG_CACHE_HOME         → …/opencode/cache
XDG_STATE_HOME         → …/opencode/state
OPENCODE_*_DIR         → the corresponding Evolution path
OMO_CONFIG_DIR/_STATE  → …/omo/{config,state}
```

Inherited `OPENCODE_*` and `OMO_*` variables are **dropped**, not overwritten,
so a stale value cannot point a child back at the operator's installation.

This matters: a child process started with this environment *cannot find* the
operator's global config, so it cannot read or overwrite it even if it tried.
That is a stronger guarantee than a promise not to.

A second, independent check: `_assert_safe()` refuses to create or write any
path under an operator-owned root, and runs on every layout creation and config
write.

## Never touched

```
~/.config/opencode          ~/.local/share/opencode
~/.cache/opencode           ~/.opencode
~/.config/omo               ~/.oh-my-openagent
~/.config/oh-my-openagent
```

The System Health page lists these explicitly alongside the paths Evolution does
own, so the operator can see the boundary rather than trust it.

## Isolation levels

| Level | Condition | Strength |
|---|---|---|
| `container` | Docker available + OpenCode binary | strongest — host not visible |
| `project_local` | binary only | dedicated HOME/XDG, weaker filesystem isolation |
| `unavailable` | no binary, or isolation cannot be established | backend **disabled** |

### What the container backend exposes

"Host not visible" is the default, not the whole story. A sandboxed evaluation
runs a script that names the evaluator and the candidate by their **host**
absolute paths, so those paths have to exist inside the container or the
evaluation dies on a missing file rather than on the candidate's own merits.

`SandboxedRunner.run_script(script, read_only_paths=...)` names them explicitly.
Each one is bind-mounted **read-only at the same absolute path**, a directory at
a time — an evaluator routinely imports a sibling, and upstream puts its
directory on `sys.path`. Nothing else is exposed, and nothing is writable, so a
candidate cannot rewrite the task it is being judged against.

### The process ceiling counts the whole user

`RLIMIT_NPROC` is not per-process. The kernel applies it per **UID**, counting
every process and thread that user already has, so a limit of 64 does not mean
"this candidate may create 64" — it means "this user may have 64 in total". On
a laptop that distinction never shows. On a CI runner or any shared machine
already past the number, the candidate cannot create a single thread.

That failure is quiet and it corrupts the result rather than reporting it. The
evaluator's own worker thread hits the wall first, reports `can't start new
thread`, returns zeroed metrics and exits cleanly — at which point a memory bomb
and an infinite loop both look **handled**, having never run. A sandbox that
reports a hostile candidate as contained because its own limit stopped the
evaluator is worse than one with no limit at all, because the test still passes.

`ResourceLimits.processes` is therefore an *allowance*, not an absolute: the
ceiling is `current user usage + processes`. A fork bomb still meets a wall a
fixed distance away, and an ordinary evaluator gets the threads it needs.

### The process ceiling and numeric thread pools

`RLIMIT_NPROC` is the sandbox's defence against a fork bomb, and it interacts
badly with numpy out of the box. OpenBLAS, MKL and OpenMP size their thread
pools from the **machine's** core count when they are imported, not from what
the process is permitted, so on a many-core host `import numpy` hits the process
ceiling and fails outright:

```
OpenBLAS blas_thread_init: pthread_create failed
```

The candidate is then reported as crashed before its first line ran. Measured on
a 16-core CI runner; invisible on a machine where the POSIX-limits tests skip.

The ceiling is the security property, so it does not move. The thread pools do:
`child_env()` pins `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`,
`NUMEXPR_NUM_THREADS` and `VECLIB_MAXIMUM_THREADS` to `1`. That is also the
behaviour this sandbox wants anyway — one candidate should not take every core,
and single-threaded BLAS makes a score reproducible rather than dependent on how
many cores happened to be free. A caller that genuinely needs more can override
any of them through `ResourceLimits(env=...)`, having raised the process limit
to match.

### The image must carry the task's dependencies

The container runs `python:3.11-slim` by default, which has **no third-party
packages**. The shipped `function_minimization` evaluator imports numpy, so in
that image it cannot be imported at all and every candidate is reported as
crashed — a true statement about the image and a misleading one about the
candidate.

This is inherent to running an arbitrary task in a container, not a defect, so
the image is configurable rather than fixed:

```bash
OE_MAX_SANDBOX_IMAGE=my-registry/evolution-task:1.4   # or any image with the
                                                      # task's dependencies
```

Precedence is explicit argument → `OE_MAX_SANDBOX_IMAGE` → `python:3.11-slim`.
Build an image with the task's requirements installed and point this at it;
there is no way for the sandbox to infer them, and installing at run time is
not an option because the container has no network by design.

The rest of the isolation is unchanged and is asserted by test:
`--network none`, a read-only root filesystem, `--cap-drop ALL`,
`--security-opt no-new-privileges`, memory/pids/CPU ceilings, and no credentials
in the environment.

Worth knowing: this backend only runs where a container runtime exists, so on a
machine without Docker the sandbox tests skip. It first executed in CI, and two
real defects surfaced immediately — the workdir was created 0700/0600 and owned
by the host user, which a non-root container image cannot read, and the task
files were not mounted at all. `tests/oe_max/test_sandbox_mounts.py` covers the
argv construction without needing a runtime, so the wiring stays checked on the
machines where it broke.

`preflight()` fails closed. If isolation cannot be guaranteed, the OpenCode
backend is disabled and native OpenEvolve evaluators continue to run. Evolution
never falls back to touching the operator's installation — that is the specified
behaviour, and it is reported as such rather than as an error.

A globally installed binary is used **as an executable only**. It is never
reconfigured, upgraded or uninstalled, and the System page says so.

## Oh My OpenAgent

Treated as optional and fast-moving. Detection probes several plausible commands
(`omo`, `oh-my-openagent`, `ohmyopenagent`) and reports what it actually finds.
No package name is hardcoded as *the* install path, because OMO has been through
naming transitions and a stale command in the architecture would be worse than
none.

Absence is a normal outcome. OpenCode-only and native OpenEvolve modes continue
to work, and the UI says the component is optional rather than reporting a
failure.

## Evaluation modes (designed)

| Mode | Flow |
|---|---|
| Direct | engine emits a candidate → sandbox runs the benchmark → metrics |
| Agent-realized | engine emits a mutation objective → OpenCode/OMO implements it → evaluator scores the result |
| Agent-harness | the candidate *is* an agent config (prompt, skill, workflow, routing) → benchmark suite runs through it |
| Hybrid | program and agent strategy evolve together |

## Implementation status — stated plainly

**Implemented and tested:** the isolation boundary, environment construction,
forbidden-path enforcement, preflight and level detection, OMO detection,
container-runtime detection, and the System/Agent Sandbox reporting. Nine tests
in `tests/evolution/test_isolation.py` cover it, including negative assertions
that writes into operator-owned paths are refused.

**Not implemented:** the executors that would run candidates inside those
sandboxes — the container and worktree backends, per-candidate resource limits
and quotas, and the four evaluation modes above. The `sandbox_runs` and
`agent_runs` tables and their event families exist and project correctly, but
nothing populates them yet.

The Agent Sandbox page reports the backend as disabled with the specific reason,
rather than rendering an inert dashboard of zeros. Native OpenEvolve evaluation
is unaffected and is what every verified run in this repository used.
