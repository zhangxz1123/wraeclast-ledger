$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$minimumCheck = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    & $pyLauncher.Source -3 -c $minimumCheck 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $pyLauncher.Source -3 -m poe_advisor serve --open
        exit $LASTEXITCODE
    }
}

foreach ($commandName in @("python", "python3")) {
    $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        continue
    }
    & $pythonCommand.Source -c $minimumCheck 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $pythonCommand.Source -m poe_advisor serve --open
        exit $LASTEXITCODE
    }
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path -LiteralPath $bundledPython -PathType Leaf) {
    & $bundledPython -c $minimumCheck 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $bundledPython -m poe_advisor serve --open
        exit $LASTEXITCODE
    }
}

Write-Error "Wraeclast Ledger requires Python 3.11 or newer. Install Python from https://www.python.org/downloads/ and run this script again."
exit 1
