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
#   * The model is 65 layers and 14.26 GB, so most of it cannot be on an 8 GB
#     card and the CPU layers set the speed. How many actually fit is smaller
#     than the arithmetic suggests -- see the note on -GpuLayers below, where
#     forcing the number up was measured to make throughput monotonically
#     worse rather than better.
#   * This llama.cpp build (winget ggml.llamacpp) is a **Vulkan** build, not
#     CUDA -- `llama-server --list-devices` reports `Vulkan0`. It works, and
#     Vulkan is normally slower than CUDA on NVIDIA. Ollama uses CUDA, so
#     prefer Ollama as the primary endpoint and treat this as the alternative.

[CmdletBinding()]
param(
    # TUNE THIS DOWNWARD, and measure -- do not assume higher is faster.
    #
    # Measured on Ollama with the same model and card, forcing more layers onto
    # the GPU was monotonically WORSE: 39% resident gave 3.26 tok/s, 54% gave
    # 2.56, 60% gave 2.08, and it kept falling. Past what genuinely fits, the
    # driver spills into Windows shared GPU memory -- system RAM over PCIe --
    # which is slower than running those layers on the CPU.
    #
    # llama.cpp has no auto-split: -ngl defaults to 0, so a number is required.
    # 28 is a deliberately conservative start for an 8 GB card holding a 14 GB
    # model. Raise it a few at a time and keep the value where tok/s stops
    # improving; if the server dies during load you have gone well past it.
    #
    # NOT measured for this build -- the sweep above was Ollama/CUDA, and this
    # is Vulkan. The method transfers; the number may not.
    [int]$GpuLayers = 28,
    # Not the model's 262144. KV cache scales with context, and on this box
    # every megabyte of KV evicts a megabyte of weights to system RAM.
    [int]$Ctx = 8192,
    # All twelve logical cores, not the six physical ones.
    #
    # An earlier version of this file said the opposite -- "hyperthreads contend
    # for the same vector units and measurably hurt token generation" -- which
    # is the usual advice and was written here without being measured. Measured
    # on Ollama with this model it is wrong, and monotonically so: 4 threads
    # 3.14 tok/s, 6 threads 3.26, 8 threads 3.38, 12 threads 3.41.
    #
    # The advice holds for workloads that saturate the vector units. Roughly
    # 60% of this model runs on the CPU and is memory-bandwidth bound, where a
    # stalled thread leaves units idle for its sibling to use.
    [int]$Threads = 12,
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
