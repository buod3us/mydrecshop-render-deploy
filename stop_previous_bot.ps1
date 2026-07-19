[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDirectory
)

$ErrorActionPreference = "Stop"
$projectPath = [IO.Path]::GetFullPath($ProjectDirectory).TrimEnd("\")

$botProcesses = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -and
        $_.CommandLine.Contains($projectPath) -and
        $_.CommandLine.Contains("-m mydrecshop")
    }
)

if ($botProcesses.Count -eq 0) {
    exit 0
}

Write-Host "[INFO] Stopping previous bot instance..."
$oldWindowIds = @()

foreach ($botProcess in $botProcesses) {
    $parent = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($botProcess.ParentProcessId)" `
        -ErrorAction SilentlyContinue
    if (
        $parent -and
        $parent.Name -eq "cmd.exe" -and
        $parent.CommandLine -and
        $parent.CommandLine.Contains("start_bot.bat")
    ) {
        $oldWindowIds += $parent.ProcessId
    }
}

foreach ($botProcess in $botProcesses) {
    Stop-Process -Id $botProcess.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 500

foreach ($windowId in ($oldWindowIds | Sort-Object -Unique)) {
    Stop-Process -Id $windowId -Force -ErrorAction SilentlyContinue
}
