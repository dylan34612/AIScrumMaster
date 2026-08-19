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

function Invoke-Python {
    param(
        [string[]]$PythonCommand,
        [string[]]$Arguments
    )

    $pythonExe = $PythonCommand[0]
    $pythonArgs = @()
    if ($PythonCommand.Length -gt 1) {
        $pythonArgs = $PythonCommand[1..($PythonCommand.Length - 1)]
    }
    & $pythonExe @pythonArgs @Arguments
}

if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
    Write-Host "Created .env from .env.example. Fill in secrets before live runs."
}

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    $python = Get-PythonCommand
    Invoke-Python -PythonCommand $python -Arguments @("-m", "venv", ".venv")
}

$venvPython = ".\.venv\Scripts\python.exe"
$env:PYTHONPATH = (Resolve-Path ".\src").Path

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $venvPython -c "import agenticscrum, debugpy, sqlalchemy, fastapi, docx" 2>$null
$importExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($importExitCode -ne 0) {
    Write-Host "Installing missing development dependencies..."
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install --ignore-requires-python -e ".[dev]"
}
