# 把铃枢连接器打包成单文件 .exe（免 Python、双击即用）。
#
# 在 Windows 上运行（例如 desktop-01）。PyInstaller 不做交叉编译：
# 要出 Linux 二进制就在 Linux 上跑 build_connector.sh。
#
# 用法（PowerShell）：powershell -ExecutionPolicy Bypass -File workers\build_connector.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少 pyinstaller，请先安装：python -m pip install pyinstaller" -ForegroundColor Red
    exit 1
}

Remove-Item -Recurse -Force build, dist-connector, bellhub-connector.spec -ErrorAction SilentlyContinue
python -m PyInstaller --onefile --name bellhub-connector `
    --distpath dist-connector `
    --console bellhub_connector.py

$exe = Join-Path $PSScriptRoot "dist-connector\bellhub-connector.exe"
Write-Host ""
Write-Host "✅ 产物：$exe" -ForegroundColor Green
if (Test-Path $exe) {
    # 版本旁文件：面板读不到 exe 内部的 VERSION，靠它告诉连接器"这份产物是哪一版"
    $m = Select-String -Path (Join-Path $PSScriptRoot "bellhub_connector.py") `
                       -Pattern '^VERSION\s*=\s*"([^"]+)"'
    if ($m) {
        $ver = $m.Matches[0].Groups[1].Value
        $sidecar = Join-Path $PSScriptRoot "dist-connector\bellhub-connector.exe.version"
        [System.IO.File]::WriteAllText($sidecar, "$ver`n", (New-Object System.Text.UTF8Encoding $false))
        Write-Host "版本旁文件：$sidecar = $ver"
    }
    (Get-FileHash $exe -Algorithm SHA256).Hash
    Write-Host "用法：双击后按提示；或在此目录执行 .\bellhub-connector.exe pair"
    Write-Host "发布前请核对 sha256，并把哈希连同版本号写进面板下载说明。"
}
