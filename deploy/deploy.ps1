<#
Deploy (or update) the assistant on your Oracle Cloud VM.

  .\deploy\deploy.ps1 -Server ubuntu@<VM-PUBLIC-IP> -Domain <you>.duckdns.org -Key C:\path\to\ssh-key.key

Uses .env.server (your production settings) - never your local .env.
#>
param(
  [Parameter(Mandatory)][string]$Server,
  [Parameter(Mandatory)][string]$Domain,
  [Parameter(Mandatory)][string]$Key
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path ".env.server")) { throw ".env.server not found. Copy .env.example to .env.server and fill in the production values." }
if (git status --porcelain) { Write-Warning "You have uncommitted changes; only committed code is deployed." }

$tar = Join-Path $env:TEMP "assistant.tar.gz"
git archive --format=tar.gz -o $tar HEAD
$ssh = @("-i", $Key, "-o", "StrictHostKeyChecking=accept-new")
Write-Host "==> Uploading"
scp @ssh $tar "${Server}:/tmp/assistant.tar.gz"
scp @ssh ".env.server" "${Server}:/tmp/assistant.env"
scp @ssh "deploy/setup_server.sh" "${Server}:/tmp/setup_server.sh"
Write-Host "==> Installing on server"
ssh @ssh $Server "bash /tmp/setup_server.sh $Domain"
if ($LASTEXITCODE -ne 0) { throw "Server setup failed (see output above)" }
Write-Host "`nDeployed: https://$Domain" -ForegroundColor Green
