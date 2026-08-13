<# Install AI-Kit from a self-contained .ai directory into a project. #>
[CmdletBinding()]
param(
    [string]$Target = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [switch]$Force,
    [switch]$DryRun
)

$aiRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$targetPath = (Resolve-Path $Target).Path
$projectRoot = (Split-Path -Parent $aiRoot)
$configTemplateRoot = Join-Path $aiRoot 'install/config'
$configFiles = @('runners.yaml', 'automation.yaml', 'registry.yaml', 'contexts.yaml', 'epics.yaml', 'rules.yaml', 'kit.yaml')
if ($targetPath -eq $aiRoot) { throw 'Target cannot be the .ai directory.' }

$entries = New-Object System.Collections.Generic.List[object]
function Add-Entry([string]$Source, [string]$Destination) {
    $entries.Add([pscustomobject]@{ Source = $Source; Destination = $Destination })
}

# The complete .ai tree is the install source, including install/config so
# the installed kit stays self-contained for future installation runs.
Get-ChildItem -LiteralPath $aiRoot -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($aiRoot.Length).TrimStart('\','/')
    $destination = Join-Path (Join-Path $targetPath '.ai') $relative
    Add-Entry $_.FullName $destination
}

Add-Entry (Join-Path $aiRoot 'install/AGENTS.md') (Join-Path $targetPath 'AGENTS.md')
$templateRoot = Join-Path $aiRoot 'install/templates'
Get-ChildItem -LiteralPath $templateRoot -Recurse -File -Force | ForEach-Object {
    $relative = $_.FullName.Substring($templateRoot.Length).TrimStart('\','/')
    Add-Entry $_.FullName (Join-Path $targetPath $relative)
}

foreach ($name in $configFiles) {
    Add-Entry (Join-Path $configTemplateRoot $name) (Join-Path (Join-Path $targetPath '.ai-config') $name)
}

$conflicts = New-Object System.Collections.Generic.List[string]
foreach ($entry in $entries) {
    if ($entry.Destination.StartsWith((Join-Path $targetPath '.ai-config') + [IO.Path]::DirectorySeparatorChar) -and (Test-Path -LiteralPath $entry.Destination)) { continue }
    if (-not (Test-Path -LiteralPath $entry.Destination)) { continue }
    $samePath = [IO.Path]::GetFullPath($entry.Source) -eq [IO.Path]::GetFullPath($entry.Destination)
    if ($samePath) { continue }
    if ((Get-FileHash -LiteralPath $entry.Source).Hash -ne (Get-FileHash -LiteralPath $entry.Destination).Hash) {
        $relative = $entry.Destination.Substring($targetPath.Length).TrimStart('\','/')
        $conflicts.Add($relative)
    }
}
if ($conflicts.Count -gt 0 -and -not $Force) {
    $conflicts | ForEach-Object { Write-Error "conflict: $_" }
    throw 'Installation stopped. Re-run with -Force to replace managed files.'
}

foreach ($entry in $entries) {
    if ($entry.Destination.StartsWith((Join-Path $targetPath '.ai-config') + [IO.Path]::DirectorySeparatorChar) -and (Test-Path -LiteralPath $entry.Destination)) { continue }
    $relative = $entry.Destination.Substring($targetPath.Length).TrimStart('\','/')
    if ($DryRun) { Write-Output "copy: $relative"; continue }
    if ([IO.Path]::GetFullPath($entry.Source) -eq [IO.Path]::GetFullPath($entry.Destination)) { continue }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $entry.Destination) | Out-Null
    Copy-Item -LiteralPath $entry.Source -Destination $entry.Destination -Force
}

if (-not $DryRun) {
    $ignore = Join-Path $targetPath '.gitignore'
    $marker = '# AI-Kit runtime state'
    if (-not (Test-Path -LiteralPath $ignore) -or -not (Select-String -LiteralPath $ignore -SimpleMatch $marker -Quiet)) {
        Add-Content -LiteralPath $ignore -Value "`n$marker"
    }
    if (-not (Select-String -LiteralPath $ignore -Pattern '^\.ai-work/$' -Quiet)) {
        Add-Content -LiteralPath $ignore -Value '.ai-work/'
    }
    if (-not (Select-String -LiteralPath $ignore -SimpleMatch '.visualizer/*.json' -Quiet)) {
        Add-Content -LiteralPath $ignore -Value '.visualizer/*.json'
    }
}

Write-Output "AI-Kit installed into $targetPath"
Write-Output 'Next: bash .ai/scripts/bootstrap.sh; bash .ai/scripts/doctor.sh'
