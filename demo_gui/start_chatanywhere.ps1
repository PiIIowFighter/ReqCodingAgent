param(
    [Parameter(Mandatory = $true)]
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'

if (-not $env:CHATANYWHERE_API_KEY) {
    Write-Error 'CHATANYWHERE_API_KEY is not set. Export it in your shell before starting the demo.'
}

$Root = Split-Path -Parent $PSScriptRoot
$Config = Join-Path $Root 'configs/agent/demo-chatanywhere.json'

$env:OPENAI_BASE_URL = 'https://api.chatanywhere.tech/v1'
$env:OPENAI_API_KEY = $env:CHATANYWHERE_API_KEY

& py -3.11 (Join-Path $Root 'demo_gui/server.py') --host 127.0.0.1 --workspace $Workspace --config $Config @Args
exit $LASTEXITCODE
