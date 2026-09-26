param(
    [ValidateRange(1024, 65535)]
    [int]$DebugPort = 0
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$desktopDir = Join-Path $projectRoot "desktop"
$electronExe = Join-Path $desktopDir "node_modules\electron\dist\electron.exe"

if (-not (Test-Path -LiteralPath $electronExe -PathType Leaf)) {
    Write-Error "找不到 Electron。请先在 desktop 目录运行 npm install。"
    exit 1
}

if ($DebugPort -gt 0) {
    $oldDebugPort = $env:YACHIYO_DEBUG_PORT
    try {
        $env:YACHIYO_DEBUG_PORT = "$DebugPort"
        Start-Process -FilePath $electronExe -ArgumentList @(".") -WorkingDirectory $desktopDir
    }
    finally {
        if ($null -eq $oldDebugPort) {
            Remove-Item Env:YACHIYO_DEBUG_PORT -ErrorAction SilentlyContinue
        }
        else {
            $env:YACHIYO_DEBUG_PORT = $oldDebugPort
        }
    }
}
else {
    Start-Process -FilePath $electronExe -ArgumentList @(".") -WorkingDirectory $desktopDir
}
