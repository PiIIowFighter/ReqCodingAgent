param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace,
    [string]$Config = ''
)

$ErrorActionPreference = 'Stop'

if (-not $env:OPENAI_BASE_URL -or -not $env:OPENAI_API_KEY) {
    Write-Error 'OPENAI_BASE_URL and OPENAI_API_KEY must be configured in this same PowerShell process before starting the demo.'
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $Root 'configs/agent/demo-openai.json' }

Push-Location -LiteralPath $Root
try {
    & py -3.11 -m demo_gui.server --host 127.0.0.1 --workspace $Workspace --config $Config @Args
    $ServerExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $ServerExitCode
