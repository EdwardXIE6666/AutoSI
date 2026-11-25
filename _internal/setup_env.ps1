# AutoSI 一键环境配置工具
# 用途：在新电脑上自动检查/安装本项目运行所需的全部环境。
# 前提：目标电脑已安装与开发机相同版本的 ADS / SIwave。
# 用法：双击 配置环境.bat（会自动请求管理员权限并调用本脚本）。

$ErrorActionPreference = 'Continue'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ((Split-Path $scriptDir -Leaf) -eq '_internal') {
    $base = Split-Path $scriptDir -Parent
} else {
    $base = $scriptDir
}
$pkg = Join-Path $base '_internal\环境安装包'
if (-not (Test-Path $pkg)) { $pkg = Join-Path $base '环境安装包' }
$results = @()
function Add-Result($name, $ok, $note) {
    $script:results += [pscustomobject]@{
        项目 = $name; 状态 = $(if ($ok) { '[ OK ]' } else { '[失败]' }); 说明 = $note }
}

Write-Host ''
Write-Host '====== AutoSI 环境一键配置 ======' -ForegroundColor Cyan
Write-Host ''

# ---------- 1. ADS ----------
# 与 src/ads/ads_runtime.py 同一套门槛：目录里要有 bin\ads.exe，
# 还要有本机 keysight.ads.ael 原生库。只「有个 ADS 文件夹」不算通过。
function Test-AdsRoot([string]$p) {
    if (-not $p) { return $false }
    return Test-Path (Join-Path $p 'bin\ads.exe')
}
function Get-AdsAelInfo([string]$p) {
    $aelDir = Join-Path $p 'tools\python\packages\keysight\ads\ael'
    $init = Join-Path $aelDir '__init__.py'
    $pyd = Get-ChildItem $aelDir -Filter '_ael*.pyd' -ErrorAction SilentlyContinue | Select-Object -First 1
    return [pscustomobject]@{
        Ok   = (Test-Path $init) -and ($null -ne $pyd)
        Init = $init
        Pyd  = $(if ($pyd) { $pyd.Name } else { '' })
        Dir  = $aelDir
    }
}
function Broadcast-Environment {
    try {
        if (-not ('Win32Env.Native' -as [type])) {
            Add-Type -Namespace Win32Env -Name Native -MemberDefinition @'
[DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(
    IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
    uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        [void][Win32Env.Native]::SendMessageTimeout(
            [IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, 'Environment', 2, 3000, [ref]$result)
    } catch {}
}

$adsFound = $false; $adsPath = ''

# 方法1: HPEESOF_DIR，但必须真是 ADS 根（有 ads.exe）。光目录存在不够。
if ($env:HPEESOF_DIR -and (Test-AdsRoot $env:HPEESOF_DIR)) {
    $adsFound = $true; $adsPath = $env:HPEESOF_DIR
}

# 方法2: 注册表（支持 Keysight / Agilent 品牌，32/64 位）
if (-not $adsFound) {
    foreach ($rp in @(
        'HKLM:\SOFTWARE\Keysight\ADS',
        'HKLM:\SOFTWARE\WOW6432Node\Keysight\ADS',
        'HKLM:\SOFTWARE\Agilent\ADS',
        'HKLM:\SOFTWARE\WOW6432Node\Agilent\ADS'
    )) {
        if (Test-Path $rp -ErrorAction SilentlyContinue) {
            foreach ($ver in @(Get-ChildItem $rp -ErrorAction SilentlyContinue)) {
                $d = (Get-ItemProperty $ver.PSPath -ErrorAction SilentlyContinue).InstallDir
                if ($d -and (Test-AdsRoot $d)) { $adsFound = $true; $adsPath = $d; break }
            }
            if ($adsFound) { break }
        }
    }
}

# 方法3: 常见根目录下搜 ADS*（取带 ads.exe 的最新目录，不要拿到空壳文件夹）
if (-not $adsFound) {
    foreach ($root in @(
        'C:\Program Files\Keysight',   'C:\Program Files (x86)\Keysight',
        'C:\Keysight',                  'D:\Program Files\Keysight',
        'D:\Keysight',                  'E:\Program Files\Keysight',
        'E:\Keysight'
    )) {
        if (Test-Path $root -ErrorAction SilentlyContinue) {
            $hits = @(Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'ADS*' -and (Test-AdsRoot $_.FullName) } |
                Sort-Object Name -Descending)
            if ($hits.Count) { $adsFound = $true; $adsPath = $hits[0].FullName; break }
        }
    }
}

# 方法4: PATH 里的 hpeesofde.exe → 其上级才是 ADS 根（不要把 bin 写成 HPEESOF_DIR）
if (-not $adsFound) {
    $exe = Get-Command 'hpeesofde.exe' -ErrorAction SilentlyContinue
    if ($exe) {
        $bin = Split-Path $exe.Source -Parent
        $root = Split-Path $bin -Parent
        if (Test-AdsRoot $root) { $adsFound = $true; $adsPath = $root }
    }
}

Add-Result 'ADS' $adsFound $(if ($adsFound) { $adsPath } else { '未找到带 bin\ads.exe 的 ADS 安装，请确认已安装 Keysight ADS' })

$aelOk = $false
if ($adsFound) {
    $ael = Get-AdsAelInfo $adsPath
    $aelOk = [bool]$ael.Ok
    Add-Result 'ADS Python接口' $aelOk $(
        if ($aelOk) { "$($ael.Pyd) @ $($ael.Dir)" }
        else { "缺少 keysight.ads.ael（__init__.py / _ael*.pyd）。功能3 会 ImportError，请重装/修复 ADS" }
    )
} else {
    Add-Result 'ADS Python接口' $false '未找到 ADS，无法检查 keysight.ads.ael'
}

# ---------- 1b. 写入 HPEESOF_DIR 环境变量 ----------
if ($adsFound) {
    $cur = [Environment]::GetEnvironmentVariable('HPEESOF_DIR', 'Machine')
    if ($cur -ne $adsPath) {
        [Environment]::SetEnvironmentVariable('HPEESOF_DIR', $adsPath, 'Machine')
        $env:HPEESOF_DIR = $adsPath
        Broadcast-Environment
        Add-Result 'HPEESOF_DIR' $true "已写入机器级并广播刷新: $adsPath（请关掉已打开的 AutoSI 再开）"
    } else {
        $env:HPEESOF_DIR = $adsPath
        Broadcast-Environment
        Add-Result 'HPEESOF_DIR' $true "已存在且正确: $adsPath"
    }
} else {
    Add-Result 'HPEESOF_DIR' $false '无法设置：未检测到完整 ADS 安装路径'
}

# ---------- 2. SIwave ----------
$siw = $false
try {
    $null = Get-ItemProperty 'Registry::HKEY_CLASSES_ROOT\SIwave.Application' -ErrorAction Stop
    $siw = $true
} catch {}
Add-Result 'SIwave' $siw $(if ($siw) { 'COM 接口 SIwave.Application 已注册' } else { '未注册, 请先安装 SIwave' })

# ---------- 3. Python ----------
function Get-PyVersion {
    try {
        $v = & python --version 2>&1
        if ("$v" -match 'Python 3\.\d+') { return "$v".Trim() }
    } catch {}
    return $null
}
$pyv = Get-PyVersion
if (-not $pyv) {
    $inst = Get-ChildItem $pkg -Filter 'python-3*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($inst) {
        Write-Host '正在静默安装 Python (约 1-2 分钟, 请稍候)...' -ForegroundColor Yellow
        Start-Process $inst.FullName -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_tcltk=1' -Wait
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
        $pyv = Get-PyVersion
    }
}
Add-Result 'Python' ($null -ne $pyv) $(if ($pyv) { $pyv } else { '未安装且缺少安装包: 环境安装包\python-3.12.10-amd64.exe' })

# ---------- 4. pywin32 (功能2 SIwave 抽取必需) ----------
$pw = $false
if ($pyv) {
    & python -c 'import win32com' 2>$null
    if ($LASTEXITCODE -eq 0) { $pw = $true }
    if (-not $pw) {
        Write-Host '正在安装 pywin32...' -ForegroundColor Yellow
        & python -m pip install --no-index --find-links "$pkg" pywin32 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '离线包不匹配, 尝试在线安装...' -ForegroundColor Yellow
            & python -m pip install pywin32
        }
        & python -c 'import win32com' 2>$null
        if ($LASTEXITCODE -eq 0) { $pw = $true }
    }
}
Add-Result 'pywin32' $pw $(if ($pw) { 'win32com 可用' } else { '安装失败, 请联网后执行: python -m pip install pywin32' })

# ---------- 5. Graphviz (路径图 PDF 渲染必需) ----------
$dot = $null
try { $dot = (Get-Command dot.exe -ErrorAction Stop).Source } catch {}
if (-not $dot) {
    $gvzip = Get-ChildItem $pkg -Filter 'Graphviz*.zip' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($gvzip) {
        Write-Host '正在安装 Graphviz (解压)...' -ForegroundColor Yellow
        $dest = 'C:\Program Files\Graphviz'
        $tmp = Join-Path $env:TEMP 'gvx_extract'
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive -Path $gvzip.FullName -DestinationPath $tmp -Force
        $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
        if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
        Move-Item $inner.FullName $dest
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        $mp = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        if ($mp -notlike "*$dest\bin*") {
            [Environment]::SetEnvironmentVariable('Path', $mp + ';' + "$dest\bin", 'Machine')
        }
        $env:Path += ";$dest\bin"
        try { $dot = (Get-Command dot.exe -ErrorAction Stop).Source } catch {}
        if ($dot) { & dot -c }   # 首次安装需注册渲染插件
    }
}
Add-Result 'Graphviz' ($null -ne $dot) $(if ($dot) { $dot } else { '未安装且缺少安装包: 环境安装包\Graphviz-win64.zip' })

# ---------- 6. 运行时骨架 ----------
$internal = Join-Path $base '_internal'
if (-not (Test-Path $internal)) { New-Item -ItemType Directory -Path $internal | Out-Null }
Add-Result '工作目录' $true '工作区由 AutoSI 界面创建，不再预建编号空目录'

# ---------- 7. 必要文件 ----------
$files = @(
    'MAIN.exe',
    '配置环境.bat',
    '_internal\de_sim.cfg',
    '_internal\hpeesofsim.cfg',
    '_internal\fix_snp.ael',
    '_internal\src\siwave_extractor\__main__.py',
    '_internal\src\rf\segments.py'
)
$missing = @()
foreach ($f in $files) {
    $here = Join-Path $base $f
    $legacy = Join-Path $base ($f -replace '^_internal\\', '')
    if (-not (Test-Path $here) -and -not (Test-Path $legacy)) { $missing += $f }
}
Add-Result '必要文件' ($missing.Count -eq 0) $(if ($missing.Count) { '缺失: ' + ($missing -join ', ') } else { '齐全' })

# ---------- 汇总 ----------
Write-Host ''
Write-Host '=============== 配置结果 ===============' -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-String | Write-Host
$fail = @($results | Where-Object 状态 -eq '[失败]')
if ($fail.Count) {
    Write-Host '存在未通过项, 请按"说明"列处理后重新运行本工具。' -ForegroundColor Yellow
} else {
    Write-Host '检查通过。请先关掉已打开的 AutoSI，再双击 MAIN.exe（刚写入的环境变量对已打开的程序不可见）。' -ForegroundColor Green
    Write-Host '功能3 用的是本机 ADS，不是 exe 里自带的接口；请用重新打包后的 MAIN.exe。' -ForegroundColor Green
}
