# BrainPort acceptance gates.
#
# Every gate here runs a real check. An earlier version of this script
# hardcoded eight of them to pass -- `$passes += Gate "..." $true` with nothing
# behind it -- and printed ALL GATES PASSED while a third of the criteria had
# never been evaluated. That is the one thing this project's ground rules say
# not to do (CLAUDE.md rule 4), so each gate below names the pytest node that
# backs it and fails when that node fails.
#
# A criterion with no automated check does not get a green PASS. It is reported
# as UNVERIFIED and counted separately, because "we did not check" and "we
# checked and it was fine" are different claims.
#
# Usage:  pwsh scripts/verify-brainport-acceptance.ps1 [-Python <path>]

[CmdletBinding()]
param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if (-not $Python) {
    foreach ($candidate in @(".venv/Scripts/python.exe", ".venv/bin/python")) {
        if (Test-Path (Join-Path $repo $candidate)) { $Python = Join-Path $repo $candidate; break }
    }
}
if (-not $Python) { $Python = "python" }

Write-Host "=== BrainPort acceptance gates ===" -ForegroundColor Cyan
Write-Host "python: $Python`n" -ForegroundColor DarkGray

# Each gate is: a name, and the pytest node ids that establish it.
$gates = @(
    @{ Name = "no NVIDIA model IDs in core";                    Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_hardcoded_provider_strings") }
    @{ Name = "no OpenRouter IDs in core";                      Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_hardcoded_provider_strings") }
    @{ Name = "no OpenCode Zen IDs in core";                    Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_hardcoded_provider_strings") }
    @{ Name = "no provider endpoint URLs in core";              Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_hardcoded_provider_strings") }
    @{ Name = "no provider API key env vars in core";           Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_provider_env_vars") }
    @{ Name = "no role->provider matrix in core";               Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_core_contains_no_hardcoded_role_to_provider_matrix") }
    @{ Name = "policy modes replace role->model matrix";        Nodes = @("tests/brain/test_brainport_acceptance.py::test_policy_modes_replace_roles") }
    @{ Name = "legacy provider stack is quarantined";           Nodes = @("tests/brain/test_brainport_acceptance.py::test_legacy_adapter_is_isolated") }
    @{ Name = "changing model requires zero source changes";    Nodes = @("tests/brain/test_brainport_acceptance.py::test_brain_request_never_requires_vendor_model_id") }
    @{ Name = "capability negotiation, not model names";        Nodes = @("tests/brain/test_brainport_acceptance.py::test_capability_negotiation_not_model_name") }
    @{ Name = "BrainPort interface is the contract";            Nodes = @("tests/brain/test_brainport_acceptance.py::test_brainport_interface_exists") }
    @{ Name = "one brain can run a lifecycle end to end";       Nodes = @("tests/brain/test_worker_integration.py::test_worker_lifecycle_direct") }
    @{ Name = "a run needs no API key";                         Nodes = @("tests/brain/test_worker_integration.py::test_no_api_key_required") }
    @{ Name = "worker answers the hello handshake";             Nodes = @("tests/brain/test_brainport_acceptance.py::test_worker_answers_the_hello_handshake",
                                                                          "tests/brain/test_worker_integration.py::test_worker_hello_subprocess") }
    @{ Name = "worker declares every RPC the plugin calls";     Nodes = @("tests/brain/test_brainport_acceptance.py::test_worker_declares_every_rpc_the_plugin_calls") }
    @{ Name = "plugin exposes start/inspect/stop/resume/apply"; Nodes = @("tests/brain/test_brainport_acceptance.py::test_plugin_exposes_every_evolution_tool") }
    @{ Name = "brain.mode defaults to inherit";                 Nodes = @("tests/brain/test_brain_evolution.py::test_brain_mode_inherit_default") }
    @{ Name = "archives, gates and search policies still work"; Nodes = @("tests/oe_max/test_gates.py", "tests/oe_max/test_archives.py") }
    @{ Name = "duplicates rejected before expensive eval";      Nodes = @("tests/brain/test_brain_evolution.py::test_content_cache_prevents_rerun",
                                                                          "tests/brain/test_brain_evolution.py::test_funnel_cheap_death") }
    @{ Name = "content cache is honoured";                      Nodes = @("tests/brain/test_brain_evolution.py::test_content_cache_basic") }
    @{ Name = "cancellation stops a run";                       Nodes = @("tests/brain/test_brain_evolution.py::test_cancellation_via_timeout") }
    @{ Name = "crash/resume restores from checkpoint";          Nodes = @("tests/brain/test_brain_evolution.py::test_checkpoint_crash_resume") }
    @{ Name = "budget exhaustion halts work";                   Nodes = @("tests/brain/test_brain_evolution.py::test_budgets_exhaustion") }
    @{ Name = "candidates are isolated from the active tree";   Nodes = @("tests/brain/test_brain_evolution.py::test_isolation_does_not_corrupt_real_tree") }
    @{ Name = "promotion into the tree is explicit";            Nodes = @("tests/brain/test_worker_integration.py::test_isolation_explicit_promotion_direct") }
    @{ Name = "the engine tree is still byte-identical";        Nodes = @("tests/evolution/test_patch_surface.py") }
)

$passed = 0
$failed = 0
$failedNames = @()

foreach ($gate in $gates) {
    $args = @("-m", "pytest", "-q", "-p", "no:cacheprovider") + $gate.Nodes
    & $Python @args *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("[PASS] " + $gate.Name) -ForegroundColor Green
        $passed++
    } else {
        Write-Host ("[FAIL] " + $gate.Name) -ForegroundColor Red
        Write-Host ("       " + ($gate.Nodes -join " ")) -ForegroundColor DarkGray
        $failed++
        $failedNames += $gate.Name
    }
}

# Criteria that no automated check establishes. These are listed rather than
# silently omitted: an acceptance report that hides its own gaps is worse than
# one that admits them.
$unverified = @(
    @{ Name = "reproducible benchmark evidence";
       Why  = "benchmarks/results.json is a recorded artefact, not a check -- it proves a run happened once, not that it reproduces" }
    @{ Name = "a real OpenCode model serves a full evolution run";
       Why  = "needs a live OpenCode host; every gate above runs against NullBrainPort or the stdio worker" }
)

Write-Host ""
foreach ($item in $unverified) {
    Write-Host ("[UNVERIFIED] " + $item.Name) -ForegroundColor Yellow
    Write-Host ("             " + $item.Why) -ForegroundColor DarkGray
}

Write-Host ""
Write-Host ("=== $passed passed, $failed failed, " + $unverified.Count + " unverified ===") -ForegroundColor Cyan

if ($failed -gt 0) {
    Write-Host "FAILED GATES:" -ForegroundColor Red
    foreach ($name in $failedNames) { Write-Host "  - $name" -ForegroundColor Red }
    exit 1
}

Write-Host "All automated gates passed. $($unverified.Count) criteria remain unverified (listed above)." -ForegroundColor Green
exit 0
