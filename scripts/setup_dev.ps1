$ErrorActionPreference = "Stop"

Set-Location (Resolve-Path "$PSScriptRoot\..")

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & py -3.12 --version *> $null
        $python312ExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorActionPreference
        if ($python312ExitCode -eq 0) {
            return @("py", "-3.12")
        }
        Write-Warning "Python 3.12 was not found via py launcher; falling back to default python."
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }

    throw "No Python interpreter found. Install Python 3.12+ and rerun this script."
}

$python = Get-PythonCommand
$pythonExe = $python[0]
$pythonArgs = @()
if ($python.Length -gt 1) {
    $pythonArgs = $python[1..($python.Length - 1)]
}

if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "Created .env from .env.example. Fill in secrets before live runs."
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    & $pythonExe @pythonArgs -m venv .venv
}

$venvPython = ".\.venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install --ignore-requires-python -e ".[dev]"

Write-Host ""
Write-Host "Development environment is ready."
Write-Host "Use Cursor launch profile 'Agentic Scrum: Serve Web UI' to debug the app."
