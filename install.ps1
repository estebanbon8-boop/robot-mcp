<#
  Installe robot-mcp et le déclare dans Claude Desktop et/ou Claude Code.
  Usage :  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-Client both|claude-desktop|claude] [-WhatIf]
  Prérequis : Windows, Robot Structural Analysis installé et lancé au moins une fois, uv (winget install astral-sh.uv).
  Fermer Claude Desktop avant (sinon il peut écraser sa configuration en se fermant).
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("both", "claude-desktop", "claude")]
    [string]$Client = "both"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$pyX64 = "cpython-3.12-windows-x86_64-none"   # Robot est x64 : Python x64 obligatoire (même sur Windows ARM)

function Save-Json($obj, $path) {
    $json = $obj | ConvertTo-Json -Depth 100
    [IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding $false))
}

function Add-Server($path) {
    if (Test-Path $path) {
        Copy-Item $path "$path.bak-robotmcp" -Force
        $j = Get-Content $path -Raw | ConvertFrom-Json
    } else {
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        $j = [pscustomobject]@{}
    }
    if (-not $j.mcpServers) { $j | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force }
    $srv = [pscustomobject]@{ command = $py; args = @("-m", "robot_mcp") }
    $j.mcpServers | Add-Member -NotePropertyName "robot-mcp" -NotePropertyValue $srv -Force
    Save-Json $j $path
    Write-Host "  [config] robot-mcp ajouté -> $path" -ForegroundColor Green
}

Write-Host "== robot-mcp : installation ==" -ForegroundColor Cyan

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv introuvable. Installe-le : winget install --id astral-sh.uv -e  puis rouvre PowerShell."
}
if (-not (Test-Path "Registry::HKEY_CLASSES_ROOT\Robot.Application")) {
    Write-Warning "Robot non détecté dans le registre. Lance Robot une fois depuis le menu Démarrer, puis relance ce script."
}
if (Get-Process -Name "claude" -ErrorAction SilentlyContinue) {
    Write-Warning "Claude Desktop semble ouvert : quitte-le (icône près de l'horloge > Quitter) pour que la config ne soit pas écrasée."
}

if ($PSCmdlet.ShouldProcess($root, "Créer .venv (Python x64) et installer robot-mcp")) {
    uv python install $pyX64
    uv venv --python $pyX64 (Join-Path $root ".venv")
    uv pip install --python $py -e $root
    & $py -m robot_mcp --selfcheck
    if ($LASTEXITCODE -ne 0) { throw "Selfcheck en échec." }
}

if ($Client -in @("both", "claude-desktop")) {
    $targets = @()
    $targets += Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Filter claude_desktop_config.json -Recurse -ErrorAction SilentlyContinue | ForEach-Object FullName
    $targets += Get-ChildItem "$env:LOCALAPPDATA\Packages\AnthropicPBC.Claude_*\LocalCache\Roaming\Claude" -Filter claude_desktop_config.json -ErrorAction SilentlyContinue | ForEach-Object FullName
    $targets += "$env:APPDATA\Claude\claude_desktop_config.json"
    foreach ($t in ($targets | Select-Object -Unique)) {
        if ($PSCmdlet.ShouldProcess($t, "Déclarer robot-mcp (Claude Desktop)")) {
            try { Add-Server $t } catch { Write-Warning "Ignoré (JSON illisible) : $t" }
        }
    }
}

if ($Client -in @("both", "claude")) {
    $cc = Join-Path $HOME ".claude.json"
    if ($PSCmdlet.ShouldProcess($cc, "Déclarer robot-mcp (Claude Code)")) { Add-Server $cc }
}

Write-Host ""
Write-Host "Terminé. Rouvre Claude Desktop, ouvre Robot, puis demande : 'Appelle robot_status'." -ForegroundColor Cyan
Write-Host "Sauvegardes des configs : *.bak-robotmcp" -ForegroundColor DarkGray
