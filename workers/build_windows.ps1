<#
  铃枢连接器 · Windows 单文件构建脚本（在 desktop-01 这类 Windows 机器上跑一次即可）

  它做六件事（全程打印进度，最后一定有 DONE 或 FAILED 标记）：
    1. 找可用的 Python（优先 3.13/3.12/3.11 —— PyInstaller 对新版本 Python 支持常常滞后）
    2. 确保该解释器里有 PyInstaller（没有就装到用户目录，不需要管理员）
    3. 下载连接器源码
    4. 打成单文件 exe（--onefile）
    5. 写版本旁文件 + 算 sha256 与大小
    6. 把 exe 传到公共文件站，打印可下载 URL（供服务器取回）

  用法（PowerShell）：
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\bellhub\build_windows.ps1
#>

param([string]$DeviceConfig = "")

$ErrorActionPreference = "Continue"
$Dir = Join-Path $env:USERPROFILE "bellhub"   # 用户目录：普通权限即可建，不需要管理员
$SrcUrl = "https://paste.rs/JHcKI"          # 连接器源码（服务器用 paste.rs 分发）
$Exe = Join-Path $Dir "dist-connector\bellhub-connector.exe"
$Log = Join-Path $Dir "build.log"

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
Set-Location $Dir

function Say($m) { Write-Output $m; Add-Content -Path $Log -Value $m -Encoding UTF8 }

Say "=== 1) 找可用的 Python ==="
$py = $null
foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    $parts = $cand.Split(" ")
    try {
        if ($parts.Count -eq 2) { $v = & $parts[0] $parts[1] --version 2>&1 } else { $v = & $parts[0] --version 2>&1 }
        if ($LASTEXITCODE -eq 0) { $py = $cand; Say ("  ✅ 用 $cand → $v"); break }
    } catch { }
}
if (-not $py) { Say "FAILED 没找到可用的 Python（请先安装 Python 3.12/3.13 并勾选 Add to PATH）"; exit 1 }
if (-not $py.Contains(" ")) { $pv = & $py --version 2>&1 } else { $p = $py.Split(" "); $pv = & $p[0] $p[1] --version 2>&1 }
Say "  解释器：$py（$pv）"

Say "=== 2) 确保 PyInstaller 可用 ==="
$pi = $null
foreach ($name in @($py, "py -3.12", "py -3.13")) {
    $parts = $name.Split(" ")
    try {
        if ($parts.Count -eq 2) { $r = & $parts[0] $parts[1] -m PyInstaller --version 2>&1 } else { $r = & $parts[0] -m PyInstaller --version 2>&1 }
        if ($LASTEXITCODE -eq 0) { $pi = $name; Say "  ✅ $name 已有 PyInstaller $r"; break }
    } catch { }
}
if (-not $pi) {
    Say "  安装 PyInstaller 到用户目录（可能需要 1-3 分钟）…"
    $parts = $py.Split(" ")
    if ($parts.Count -eq 2) { & $parts[0] $parts[1] -m pip install --user --disable-pip-version-check pyinstaller 2>&1 | Tee-Object -FilePath $Log -Append }
    else { & $parts[0] -m pip install --user --disable-pip-version-check pyinstaller 2>&1 | Tee-Object -FilePath $Log -Append }
    $parts = $py.Split(" ")
    if ($parts.Count -eq 2) { $r = & $parts[0] $parts[1] -m PyInstaller --version 2>&1 } else { $r = & $parts[0] -m PyInstaller --version 2>&1 }
    if ($LASTEXITCODE -ne 0) {
        Say "FAILED PyInstaller 安装/运行失败（$r）"
        Say "       若是 Python 版本太新（例如 3.14），请另装 Python 3.12/3.13 后重跑本脚本"
        exit 2
    }
    $pi = $py
    Say "  ✅ 安装完成：PyInstaller $r"
}

Say "=== 3) 下载连接器源码 ==="
try {
    Invoke-WebRequest -UseBasicParsing -Uri $SrcUrl -OutFile (Join-Path $Dir "bellhub_connector.py")
} catch {
    Say "FAILED 源码下载失败：$_"
    exit 3
}
$src = Join-Path $Dir "bellhub_connector.py"
$ver = (Select-String -Path $src -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Say ("  ✅ 源码就绪，版本 " + $ver + "（" + (Get-Item $src).Length + " 字节）")

Say "=== 4) 打包单文件 exe ==="
Remove-Item -Recurse -Force (Join-Path $Dir "build"), (Join-Path $Dir "dist-connector") -ErrorAction SilentlyContinue
$parts = $pi.Split(" ")
if ($parts.Count -eq 2) {
    & $parts[0] $parts[1] -m PyInstaller --onefile --name bellhub-connector --distpath (Join-Path $Dir "dist-connector") --workpath (Join-Path $Dir "build") --specpath $Dir --console $src 2>&1 | Tee-Object -FilePath $Log -Append
} else {
    & $parts[0] -m PyInstaller --onefile --name bellhub-connector --distpath (Join-Path $Dir "dist-connector") --workpath (Join-Path $Dir "build") --specpath $Dir --console $src 2>&1 | Tee-Object -FilePath $Log -Append
}
if (-not (Test-Path $Exe)) { Say "FAILED 没有产出 exe，请看 $Log"; exit 4 }

Say "=== 5) 版本旁文件 + 校验值 ==="
[System.IO.File]::WriteAllText((Join-Path $Dir "dist-connector\bellhub-connector.exe.version"), "$ver`n", (New-Object System.Text.UTF8Encoding $false))
$sha = (Get-FileHash $Exe -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $Exe).Length
Say "  exe：$Exe"
Say "  版本：$ver"
Say "  大小：$size 字节"
Say "  sha256：$sha"

Say "=== 6) 把 exe 交回服务器 ==="
$devCfg = $null
$devCfgPath = ""
foreach ($c in @($DeviceConfig, "D:\16650\Documents\ling shu\config.json", (Join-Path $Dir "config.json"))) {
    if ($c -and (Test-Path $c)) {
        try { $devCfg = Get-Content $c -Raw -Encoding UTF8 | ConvertFrom-Json; $devCfgPath = $c; break }
        catch { Say "  设备配置解析失败（跳过）：$c" }
    }
}
$sent = $false
if ($devCfg -and $devCfg.name -and $devCfg.token -and $devCfg.hub) {
    Say "  用设备自身凭据回传（$devCfgPath → $($devCfg.hub)/device/artifact）"
    $ep = "$($devCfg.hub.TrimEnd('/'))/device/artifact"
    $resp = & curl.exe -s -X POST `
        -H "X-Device-Auth: $($devCfg.name):$($devCfg.token)" `
        -H "X-Artifact-Name: bellhub-connector.exe" `
        -H "X-Artifact-Sha256: $sha" `
        --data-binary "@$Exe" $ep 2>&1 | Out-String
    Say "  服务器回执：$($resp.Trim())"
    if ($resp -match '"ok"\s*:\s*true') {
        $sent = $true
        # 版本旁文件也一起交回（服务器据此在下载页标注产物版本）
        $side = Join-Path $Dir "dist-connector\bellhub-connector.exe.version"
        $r2 = & curl.exe -s -X POST `
            -H "X-Device-Auth: $($devCfg.name):$($devCfg.token)" `
            -H "X-Artifact-Name: bellhub-connector.exe.version" `
            --data-binary "@$side" $ep 2>&1 | Out-String
        Say "  版本旁文件回执：$($r2.Trim())"
    }
} else {
    Say "  没找到设备 config.json，跳过设备通道"
}

$url = ""
if (-not $sent) {
    Say "  改走公共文件站（备用通道）…"
    try { $url = (& curl.exe -s -A "bellhub-build/1.0" -F "fileToUpload=@$Exe" https://catbox.moe/user/api.php 2>&1 | Out-String).Trim() } catch { }
    if (-not $url -or $url -notmatch '^https') {
        Say "  catbox 不可用，改试 0x0.st…"
        try { $url = (& curl.exe -s -A "bellhub-build/1.0" -F "file=@$Exe" https://0x0.st 2>&1 | Out-String).Trim() } catch { }
    }
    if ($url -and $url -match '^https') { Say "  ✅ 下载地址：$url" }
    else { Say "  ⚠ 两条通道都没成功：请手动把 $Exe 发回服务器（并把下面的 SHA256 一起给）" }
}

Say ""
Say "SHA256=$sha"
Say "VERSION=$ver"
Say "SIZE=$size"
Say "URL=$url"
Say "SENT=$sent"
Say "DONE"
