param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path "$PSScriptRoot\..")

function Test-PortListening {
    param([int]$Port)

    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $connect = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ready = $connect.AsyncWaitHandle.WaitOne(500)
        if ($ready -and $client.Connected) {
            $client.EndConnect($connect)
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {
        # Port is not accepting connections yet.
    }
    return $false
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Running setup first..."
    & "$PSScriptRoot\setup_dev.ps1"
} else {
    # Reinstall if pyproject deps were added since the last setup (e.g. python-docx).
    & "$PSScriptRoot\ensure_dev.ps1"
}

$port = 8765
$env:PYTHONPATH = (Resolve-Path ".\src").Path
$venvPython = ".\.venv\Scripts\python.exe"

# Browser LLM mode: interactive login once before serve (scheduler-safe silent refresh afterward).
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $venvPython -c "from agenticscrum.config import load_settings; from agenticscrum.llm.auth import auth_record_exists; s=load_settings(); raise SystemExit(2 if s.llm_auth_mode == 'browser' and not auth_record_exists() else 0)"
$llmLoginCheck = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($llmLoginCheck -eq 2) {
    Write-Host ""
    Write-Host "LLM browser login required (LLM_AUTH_MODE=browser)."
    Write-Host "A browser window will open so you can sign in with your Microsoft / Entra account."
    Write-Host ""
    & $venvPython -m agenticscrum login
    if ($LASTEXITCODE -ne 0) {
        throw "LLM browser login failed. Fix .env or network access, then relaunch."
    }
}

if (Test-PortListening -Port $port) {
    Write-Host ""
    Write-Host "Agentic Scrum is already running on http://127.0.0.1:$port/"
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:$port/"
    }
    exit 0
}

Write-Host ""
Write-Host "Starting Agentic Scrum..."
Write-Host "First launch can take up to a minute while Python loads dependencies."
Write-Host "Leave this window open while the app is running."
Write-Host ""

if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param([int]$ListenPort, [int]$TimeoutSeconds)

        function Test-PortOpen {
            param([int]$Port)
            try {
                $client = New-Object System.Net.Sockets.TcpClient
                $connect = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
                $ready = $connect.AsyncWaitHandle.WaitOne(500)
                if ($ready -and $client.Connected) {
                    $client.EndConnect($connect)
                    $client.Close()
                    return $true
                }
                $client.Close()
            } catch {
            }
            return $false
        }

        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            if (Test-PortOpen -Port $ListenPort) {
                Start-Process "http://127.0.0.1:$ListenPort/"
                return
            }
            Start-Sleep -Seconds 1
        }
    } -ArgumentList $port, 120 | Out-Null
}

& $venvPython -u -m agenticscrum serve
