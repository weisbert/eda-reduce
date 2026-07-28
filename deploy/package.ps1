<#
.SYNOPSIS
  黄区（Windows，有网）一键打包 —— eda-reduce 离线气隙包。package.py 的薄包装。

  做的事：① 找一个 python3 ② full 模式先软探 PyPI 可达
          ③ 调 package.py（读 committed blob + 交叉下轮子 + 审计 glibc + 冻 lock
             + 写 MANIFEST + sha256 + 打 tar）④ 列出要传给隔离区的文件。

  取 Python 版本用「无引号」探测，刻意避开 Windows PowerShell 5.1 给原生程序
  传「含双引号参数」会被吞引号的坑。

.PARAMETER Mode
  full（默认）= 完整包，带轮子，首次部署 / 依赖改动时用。
  incremental = 仅代码，几十 KB，首次 full 之后只改了代码时用。

.EXAMPLE
  .\deploy\package.ps1
.EXAMPLE
  .\deploy\package.ps1 -Mode incremental
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\package.ps1
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'incremental')]
    [string]$Mode = 'full',
    [string]$Out = 'dist',
    [switch]$SkipNetCheck
)
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# --- 找 python
$Py = $null
foreach ($c in @('python', 'py')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $Py = $cmd.Source; break }
}
if (-not $Py) { throw "找不到 python。装一个 3.8+ 再来。" }
$ver = & $Py -c 'import sys; print(sys.version_info.major*100+sys.version_info.minor)'
if ([int]$ver -lt 306) { throw "python 太老（$ver），要 3.6+。" }
Write-Host "python : $Py  (3.$([int]$ver - 300))"

# --- 工作树脏了要大声说：包里是 HEAD 的内容，未提交的改动进不去
$dirty = & git status --porcelain
if ($dirty) {
    Write-Host ""
    Write-Host "!! 工作树有未提交改动。打包读的是 committed blob（git archive HEAD），" -ForegroundColor Yellow
    Write-Host "   下面这些改动**不会**进包：" -ForegroundColor Yellow
    $dirty | ForEach-Object { Write-Host "     $_" -ForegroundColor Yellow }
    Write-Host ""
}

# --- full 模式先软探 PyPI（依赖为空时其实用不到，但探一下省得半路失败）
if ($Mode -eq 'full' -and -not $SkipNetCheck) {
    try {
        $null = Invoke-WebRequest -Uri 'https://pypi.org/simple/' -Method Head -TimeoutSec 8 -UseBasicParsing
        Write-Host "PyPI   : 可达"
    } catch {
        Write-Host "PyPI   : 不可达 —— 依赖为空时无所谓，有依赖就会失败" -ForegroundColor Yellow
    }
}

& $Py (Join-Path $PSScriptRoot 'package.py') $Mode --out $Out
if ($LASTEXITCODE -ne 0) { throw "package.py 失败（退出码 $LASTEXITCODE）" }

Write-Host ""
Write-Host "要传到隔离区的文件："
Get-ChildItem $Out -File | Where-Object { $_.Name -like '*.tar.gz*' } |
    ForEach-Object { Write-Host ("  {0}  ({1:N1} KB)" -f $_.Name, ($_.Length / 1KB)) }
Write-Host ""
Write-Host "隔离区上（注意用 bash 调，不要 ./）："
if ($Mode -eq 'full') {
    Write-Host "  mkdir ~/eda_reduce; cd ~/eda_reduce"
    Write-Host "  tar xzf eda_reduce_full.tar.gz; bash bootstrap.sh"
    Write-Host "  (就地装，不需要 root。要装别处: bash bootstrap.sh <路径>)"
} else {
    Write-Host "  mkdir -p ~/upd; tar xzf eda_reduce_incremental.tar.gz -C ~/upd"
    Write-Host "  bash ~/upd/update.sh ~/eda_reduce"
    Write-Host "  (增量包要解到安装目录外面,否则 app/ 会在备份前被盖掉)"
}
