param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'

if (-not $env:OPENAI_API_KEY) {
    Write-Error 'OPENAI_API_KEY is not set. Export it in your shell before starting the demo.'
}

$Root = Split-Path -Parent $PSScriptRoot
$Config = Join-Path $Root 'configs/agent/demo-openai.json'

Push-Location -LiteralPath $Root
try {
    & py -3.11 -m demo_gui.server --host 127.0.0.1 --workspace $Workspace --config $Config @Args
    $ServerExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $ServerExitCode
