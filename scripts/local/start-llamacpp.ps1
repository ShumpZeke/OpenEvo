# llama.cpp serving the SAME model file Ollama and LM Studio use.
#
# One 14.26 GB GGUF on disk, three runtimes pointing at it. Ollama owns the
# blob; LM Studio reaches it through a hardlink; this script reads it directly.
# Nothing is copied, so switching runtime costs no disk.
#
#   .\scripts\local\start-llamacpp.ps1
#   .\scripts\local\start-llamacpp.ps1 -GpuLayers 28 -Ctx 4096   # if it OOMs
#
# MEASURED on the machine this was tuned for -- i5-12400F (6 cores), RTX 3050
# 8 GB, 16 GB RAM:
#
#   * The model is 65 layers and 14.26 GB. Roughly 32 of those layers fit in
#     8 GB alongside the KV cache and CUDA/Vulkan working buffers. The rest run
#     on the CPU, and that is what sets the speed.
#   * This llama.cpp build (winget ggml.llamacpp) is a **Vulkan** build, not
#     CUDA -- `llama-server --list-devices` reports `Vulkan0`. It works, and
#     Vulkan is normally slower than CUDA on NVIDIA. Ollama uses CUDA, so
#     prefer Ollama as the primary endpoint and treat this as the alternative.

[CmdletBinding()]
param(
    # 32 of 65 measured to fit. Lower it if the server dies during load, which
    # is what running out of VRAM looks like here.
    [int]$GpuLayers = 32,
    # Not the model's 262144. KV cache scales with context, and on this box
    # every megabyte of KV evicts a megabyte of weights to system RAM.
    [int]$Ctx = 8192,
    # Physical cores, not logical. Hyperthreads contend for the same vector
    # units and measurably hurt token generation.
    [int]$Threads = 6,
    [int]$Port = 8080,
    [string]$ModelPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $ModelPath) {
    $ModelPath = Join-Path $env:USERPROFILE ".ollama\models\blobs\sha256-ce18b852ff0f7f7fc3cbe3467b4a87d3b27e7b3e611bf41e0a529220604aa79f"
}
if (-not (Test-Path $ModelPath)) {
    Write-Error "Model not found: $ModelPath`nPull it with: ollama pull orcarouter/Qwen3.8-27B-Uncensored:iq4_xs"
    exit 1
}

Write-Host "model     : $ModelPath"
Write-Host ("size      : {0:N2} GB" -f ((Get-Item $ModelPath).Length / 1GB))
Write-Host "gpu layers: $GpuLayers of 65"
Write-Host "context   : $Ctx"
Write-Host "threads   : $Threads"
Write-Host "endpoint  : http://127.0.0.1:$Port/v1"
Write-Host ""

# --reasoning-budget 0 turns thinking off. This model advertises a `thinking`
# capability, and a reasoning model that thinks before answering will spend the
# whole token budget on hidden reasoning and emit an empty diff -- the failure
# documented in docs/gotchas.md. Off is the right default for mutation work.
#
# -fa on plus q8_0 K/V roughly halves the KV cache. On a box this tight that is
# not a micro-optimisation: it is the difference between ~28 and ~32 layers on
# the GPU.
& llama-server `
    --model $ModelPath `
    --alias "qwen-evo" `
    --host 127.0.0.1 `
    --port $Port `
    --n-gpu-layers $GpuLayers `
    --ctx-size $Ctx `
    --threads $Threads `
    --flash-attn on `
    --cache-type-k q8_0 `
    --cache-type-v q8_0 `
    --jinja `
    --reasoning-budget 0 `
    --parallel 1 `
    --temp 0.7 `
    --top-p 0.8 `
    --top-k 20 `
    --repeat-penalty 1.05 `
    --no-warmup
