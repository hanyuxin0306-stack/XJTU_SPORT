param(
    [string]$PythonCommand = "python",
    [switch]$SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$venvDir = Join-Path $projectDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

Push-Location -LiteralPath $projectDir
try {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $PythonCommand -m venv --system-site-packages $venvDir
        if ($LASTEXITCODE -ne 0) { throw "创建本地 Python 环境失败。" }
    }
    if (-not $SkipInstall) {
        & $venvPython -m pip install -r (Join-Path $projectDir "requirements-build.txt")
        if ($LASTEXITCODE -ne 0) { throw "安装打包依赖失败。" }
    }
    & $venvPython -m PyInstaller --noconfirm --distpath (Join-Path $projectDir "dist") --workpath (Join-Path $projectDir "build") (Join-Path $projectDir "sport.spec")
    if ($LASTEXITCODE -ne 0) { throw "构建 sport.exe 失败。" }

    $builtExePath = Join-Path $projectDir "dist\sport.exe"
    $releaseExePath = Join-Path $projectDir "sport.exe"
    if (-not (Test-Path -LiteralPath $builtExePath)) { throw "未找到 dist\sport.exe。" }
    try {
        Copy-Item -LiteralPath $builtExePath -Destination $releaseExePath -Force
    } catch {
        $releaseExePath = Join-Path $projectDir "sport_updated.exe"
        try {
            Copy-Item -LiteralPath $builtExePath -Destination $releaseExePath -Force
        } catch {
            $releaseExePath = Join-Path $projectDir ("sport_updated_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".exe")
            Copy-Item -LiteralPath $builtExePath -Destination $releaseExePath -ErrorAction Stop
        }
        Write-Host "原 sport.exe 正在使用或无法替换，新版已另存，现有任务继续运行。"
    }
    Write-Host "已生成：$releaseExePath"
} finally {
    Pop-Location
}
