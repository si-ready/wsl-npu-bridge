# install.ps1 -- one-time setup of the WSL2 NPU bridge on the Windows host.
#
# What it does:
#   1. Verifies the machine actually has a Ryzen AI (XDNA) NPU and a new
#      enough AMD NPU driver. Refuses to "succeed" on the wrong hardware.
#   2. Downloads the pinned Lemonade embeddable build (SHA-256 checked)
#      into %LOCALAPPDATA%\wsl-npu-bridge\lemonade -- no admin, no MSI,
#      nothing written outside your user profile.
#   3. Installs the NPU backend and pulls the default models.
#   4. Makes a test WAV with Windows speech synthesis for the bench tool.
#
# Pure ASCII on purpose: Windows PowerShell 5.1 reads BOM-less files as
# ANSI, and a stray UTF-8 em dash once corrupted a script of ours.
#
# Run from PowerShell:
#   powershell -ExecutionPolicy Bypass -File install.ps1
# Optional: -LlmModel <name> -SttModel <name> to change the defaults.

param(
    [string]$LlmModel = "Llama-3.2-1B-Instruct-Hybrid",
    [string]$SttModel = "Whisper-Large-v3-Turbo",
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"

# Pinned upstream artifact. Bump version + hash together, deliberately.
$LemonadeVersion = "2026.44.1"
$LemonadeUrl = "https://github.com/lemonade-sdk/lemonade/releases/download/v$LemonadeVersion/lemonade-embeddable-$LemonadeVersion-windows-x64.zip"
$LemonadeSha256 = "7ff6c5d6ab612c33690411a7e53d6d0dd62eb515a073375c35015b0778a5a344"

$Root = Join-Path $env:LOCALAPPDATA "wsl-npu-bridge"
$LemDir = Join-Path $Root "lemonade"
$BinDir = Join-Path $LemDir "lemonade-embeddable-$LemonadeVersion-windows-x64"
$Lemonade = Join-Path $BinDir "lemonade.exe"

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# --- 1. Hardware + driver gate -------------------------------------------
Step "Checking for a Ryzen AI NPU"
$npu = Get-PnpDevice | Where-Object { $_.InstanceId -match "VEN_1022&DEV_17F0" }
if (-not $npu) {
    throw "No XDNA NPU found (looking for PCI VEN_1022 DEV_17F0). This bridge needs a Ryzen AI 300/400-series machine."
}
$driver = Get-CimInstance Win32_PnPSignedDriver |
    Where-Object { $_.DeviceID -match "DEV_17F0" -and $_.DriverVersion -match "^3" } |
    Select-Object -First 1
if ($driver) {
    Write-Host "NPU driver: $($driver.DriverVersion)"
    # FastFlowLM / Ryzen AI current builds want 32.0.203.314 or newer in
    # practice; warn (do not block) below that, the runtime will say more.
    if ([version]$driver.DriverVersion -lt [version]"32.0.203.314") {
        Write-Warning "NPU driver is older than 32.0.203.314. Consider updating via Windows Update / AMD before benchmarking."
    }
} else {
    Write-Warning "Could not read the NPU driver version; continuing."
}

# --- 2. Lemonade embeddable, hash-gated -----------------------------------
Step "Installing Lemonade $LemonadeVersion (embeddable, user-local)"
New-Item -ItemType Directory -Force -Path $LemDir | Out-Null
if (-not (Test-Path $Lemonade)) {
    $zip = Join-Path $env:TEMP "lemonade-embed-$LemonadeVersion.zip"
    Invoke-WebRequest -UseBasicParsing -Uri $LemonadeUrl -OutFile $zip
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $LemonadeSha256) {
        throw "SHA-256 mismatch on the Lemonade download. Expected $LemonadeSha256, got $actual. Refusing to extract."
    }
    Expand-Archive -Force -Path $zip -DestinationPath $LemDir
    Remove-Item $zip
}
Write-Host "Lemonade CLI: $Lemonade"

# --- 3. Backend + models ---------------------------------------------------
if (-not $SkipModels) {
    # All three NPU backends: a model pull downloads model files ONLY --
    # loading fails until the matching backend is installed (learned
    # from a live "RyzenAI-Server failed to start"; see TROUBLESHOOTING).
    Step "Installing the NPU backends (FLM, Ryzen AI, whisper.cpp)"
    & $Lemonade backends install flm:npu
    & $Lemonade backends install ryzenai-llm:npu
    & $Lemonade backends install whispercpp:npu

    Step "Pulling default models (LLM: $LlmModel, STT: $SttModel)"
    & $Lemonade pull $LlmModel
    & $Lemonade pull $SttModel

    # FLM keeps its own model store; pull its starter model with flm.
    $flmExe = Join-Path $env:USERPROFILE ".cache\lemonade\bin\flm\npu\flm.exe"
    if (Test-Path $flmExe) {
        Step "Pulling the FLM starter model (llama3.2:1b)"
        & $flmExe pull llama3.2:1b
    }
} else {
    Write-Host "Skipping backend/model setup (-SkipModels)."
}

# --- 4. Bench fixture -------------------------------------------------------
Step "Creating the bench test WAV"
$wav = Join-Path $Root "bench-sample.wav"
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($wav)
$synth.Speak("The quick brown fox jumps over the lazy dog. " +
             "This sample exists so the bridge benchmark can measure " +
             "speech to text on the neural processing unit, end to end, " +
             "from inside Windows Subsystem for Linux.")
$synth.Dispose()
Write-Host "Wrote $wav"

Step "Done"
Write-Host "Start the service with:  host\serve.ps1"
Write-Host "Then, inside WSL:        python3 client\wsl_npu.py doctor"
