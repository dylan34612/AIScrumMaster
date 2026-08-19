$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path "$PSScriptRoot\..")

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Virtual environment not found. Running setup first..."
    & "$PSScriptRoot\setup_dev.ps1"
} else {
    & "$PSScriptRoot\ensure_dev.ps1"
}

$env:PYTHONPATH = (Resolve-Path ".\src").Path
$venvPython = ".\.venv\Scripts\python.exe"

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

Write-Host ""
Write-Host "Agentic Scrum debug server is starting on 127.0.0.1:5678."
Write-Host "In Cursor, run: Agentic Scrum: Attach To Debug Launcher"
Write-Host "The web UI will open after the debugger attaches."
Write-Host ""

Start-Job -ScriptBlock {
    Start-Sleep -Seconds 8
    Start-Process "http://127.0.0.1:8765/"
} | Out-Null

& $venvPython -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m agenticscrum serve
