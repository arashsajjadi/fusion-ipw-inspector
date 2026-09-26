<#
.SYNOPSIS
    Optional one-click installer for Fusion IPW Inspector on Windows.

.DESCRIPTION
    Copies the FusionIPWInspector add-in folder from this repository into the
    Fusion user add-ins folder:

        %APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\FusionIPWInspector

    No administrator rights are needed and nothing outside that folder is
    touched. Prints the installed version. Run with -Uninstall to remove it
    again. Restart Fusion (or use Utilities > Add-Ins) after installing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
#>
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'FusionIPWInspector'
$addins = Join-Path $env:APPDATA 'Autodesk\Autodesk Fusion 360\API\AddIns'
$target = Join-Path $addins 'FusionIPWInspector'

if ($Uninstall) {
    if (Test-Path $target) {
        $old = (Get-Content -LiteralPath (Join-Path $target 'FusionIPWInspector.manifest') -Raw | ConvertFrom-Json).version
        Remove-Item -LiteralPath $target -Recurse -Force
        Write-Host "Removed Fusion IPW Inspector $old from $target"
    } else {
        Write-Host 'Fusion IPW Inspector is not installed.'
    }
    return
}

if (-not (Test-Path (Join-Path $source 'FusionIPWInspector.manifest'))) {
    throw "Add-in folder not found next to this script: $source"
}
if (-not (Test-Path $addins)) {
    New-Item -ItemType Directory -Path $addins -Force | Out-Null
}
if (Test-Path $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
# Byte-code caches from development are not needed in the installed copy.
Get-ChildItem -LiteralPath $target -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

$manifest = Get-Content -LiteralPath (Join-Path $target 'FusionIPWInspector.manifest') -Raw | ConvertFrom-Json
Write-Host "Installed Fusion IPW Inspector $($manifest.version) to $target"
Write-Host 'Restart Fusion, open the Manufacture workspace and look for "IPW Inspector" in the Inspect panel.'
Write-Host 'If Fusion is already running: Utilities > Add-Ins > Scripts and Add-Ins, select FusionIPWInspector, Run.'
