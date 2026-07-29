# 常用软件自动下载工具 (PowerShell 版)
# 零依赖：仅需 Windows 自带的 PowerShell 5.1+，无需安装 Python
#
# 使用示例:
#   .\download.ps1                        # 交互模式，选择软件并下载
#   .\download.ps1 -Ids wechat,qq         # CLI模式，下载指定软件
#   .\download.ps1 -List                  # 列出所有可用软件
#   .\download.ps1 -Exclude chrome,edge   # CLI模式，排除指定软件

param(
    [string[]]$Ids = @(),
    [string[]]$Exclude = @(),
    [switch]$List,
    [string]$Dir = ""
)

# ---------- 常量 ----------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ScriptDir "config.json"
$UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
$ChunkSize = 1MB

# 启用 TLS 1.2（Win7/旧版 .NET 默认不开启）
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

# 控制台输出使用 UTF-8，避免中文乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8


function Load-Config {
    if (-not (Test-Path $ConfigFile)) {
        Write-Host "[错误] 配置文件不存在: $ConfigFile" -ForegroundColor Red
        exit 1
    }
    return Get-Content -Raw -Encoding UTF8 $ConfigFile | ConvertFrom-Json
}

function Get-ByKeyPath($data, [string]$keyPath) {
    # 通过点分隔的路径从嵌套对象中取值，如 'data.downloadUrl'
    foreach ($k in $keyPath.Split('.')) {
        if ($null -eq $data) { return $null }
        $data = $data.$k
    }
    return $data
}

function Get-Domains($softwareList) {
    # 从软件列表中提取所有下载域名
    $domains = New-Object System.Collections.Generic.HashSet[string]
    foreach ($sw in $softwareList) {
        $url = switch ($sw.type) {
            "fixed_url"  { $sw.url }
            "api"        { $sw.api_url }
            "page_parse" { $sw.download_page }
            default      { "" }
        }
        if ($url) {
            try {
                $hostname = ([System.Uri]$url).Host
                if ($hostname) { [void]$domains.Add($hostname) }
            } catch {}
        }
    }
    return $domains | Sort-Object
}

function Test-TcpPort([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 3000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($TargetHost, $Port)
        if ($task.Wait($TimeoutMs) -and $client.Connected) { return $true }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Check-Network($softwareList) {
    # 检查网络环境和各软件下载源的可达性
    Write-Host "`n[网络检查] 正在检查网络环境..."

    # 1. 基础连通性：DNS 53端口
    $dnsOk = $false
    foreach ($dns in @("8.8.8.8", "114.114.114.114")) {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if (Test-TcpPort $dns 53) {
            Write-Host ("  [基础] DNS 连通性: √ 正常 ({0}:53, 延迟 {1:0}ms)" -f $dns, $sw.Elapsed.TotalMilliseconds)
            $dnsOk = $true
            break
        }
    }
    if (-not $dnsOk) {
        Write-Host "  [基础] DNS 连通性: × 异常，可能无法下载" -ForegroundColor Yellow
    }

    # 2. 各软件来源域名可达性
    $reachableCount = 0
    $domains = @(Get-Domains $softwareList)
    foreach ($domain in $domains) {
        $reachable = Test-TcpPort $domain 443
        if ($reachable) {
            $reachableCount++
            Write-Host ("  [来源] {0,-30} -> √ 可达" -f $domain)
        } else {
            Write-Host ("  [来源] {0,-30} -> × 不可达" -f $domain) -ForegroundColor Yellow
        }
    }
    Write-Host "  网络检查完成: $reachableCount/$($domains.Count) 个来源可达`n"
}

function Select-Interactive($softwareList, [string]$defaultDir) {
    # 交互式选择：↑↓ 移动光标，空格勾选/取消，A 全选/取消全选，回车确认
    Write-Host ""
    Write-Host ("=" * 60)
    Write-Host "  常用软件自动下载工具"
    Write-Host ("=" * 60)
    Write-Host "  ↑↓ 移动 | 空格 勾选 | A 全选 | 回车 确认"
    Write-Host ("=" * 60)
    Write-Host ""

    $count = $softwareList.Count
    $cursor = 0
    $checked = New-Object bool[] $count

    # 计算字符串的显示宽度（中文等宽字符占 2 列）
    function Get-DisplayWidth([string]$text) {
        $w = 0
        foreach ($ch in $text.ToCharArray()) {
            if ([int]$ch -ge 0x2E80) { $w += 2 } else { $w += 1 }
        }
        return $w
    }

    # 按显示宽度填充整行，用空格覆盖残留字符；
    # 预留 2 列余量，避免宽字符导致实际显示宽度达到窗口宽度而意外换行
    function Pad-ToFullWidth([string]$text) {
        $pad = [Console]::WindowWidth - 2 - (Get-DisplayWidth $text)
        if ($pad -lt 0) { $pad = 0 }
        return $text + (' ' * $pad)
    }

    # 可视窗口起点（列表超出窗口高度时分页显示）
    $script:viewStart = 0

    # 渲染一帧（列表比窗口高时自动分页，光标移动带动可视窗口滑动）
    function Render-Menu {
        # 菜单区可用行数：窗口高度 - 起始行 - 状态行 - 1 行安全余量
        $avail = [Console]::WindowHeight - $script:menuTop - 2
        if ($avail -lt 4) { $avail = 4 }
        $paging = $count -gt $avail
        $indicatorRows = if ($paging) { 2 } else { 0 }   # 分页时上下各留 1 行溢出指示
        $itemRows = $avail - $indicatorRows
        if ($itemRows -lt 1) { $itemRows = 1 }
        if ($itemRows -gt $count) { $itemRows = $count }

        # 调整可视窗口起点，使光标项始终可见
        $maxStart = $count - $itemRows
        if ($maxStart -lt 0) { $maxStart = 0 }
        if ($cursor -lt $script:viewStart) { $script:viewStart = $cursor }
        if ($cursor -ge $script:viewStart + $itemRows) { $script:viewStart = $cursor - $itemRows + 1 }
        if ($script:viewStart -gt $maxStart) { $script:viewStart = $maxStart }
        if ($script:viewStart -lt 0) { $script:viewStart = 0 }

        # 防止起始行超出缓冲区（窗口被极度压缩时）
        if ($script:menuTop -gt [Console]::BufferHeight - 2) { $script:menuTop = [Console]::BufferHeight - 2 }
        if ($script:menuTop -lt 0) { $script:menuTop = 0 }
        [Console]::SetCursorPosition(0, $script:menuTop)

        # 顶部溢出指示（分页时固定占 1 行，保证每次渲染高度一致）
        if ($paging) {
            if ($script:viewStart -gt 0) {
                Write-Host (Pad-ToFullWidth ("    ↑ 上方还有 {0} 项" -f $script:viewStart)) -ForegroundColor DarkGray
            } else {
                Write-Host (Pad-ToFullWidth "")
            }
        }

        for ($i = $script:viewStart; $i -lt $script:viewStart + $itemRows; $i++) {
            $isCursor = ($i -eq $cursor)
            $isChecked = $checked[$i]
            $pointer = if ($isCursor) { " > " } else { "   " }
            # 已选: 实心绿色圆；未选: 空心圆（宽度一致，保持名称列对齐）
            $mark = if ($isChecked) { "●" } else { "○" }
            $name = $softwareList[$i].name

            $baseColor = if ($isCursor) { [ConsoleColor]::Cyan } else { [ConsoleColor]::Gray }
            $markColor = if ($isChecked) { [ConsoleColor]::Green } else { $baseColor }

            # 分段着色输出：指针、圆圈、名称+填充
            Write-Host $pointer -NoNewline -ForegroundColor $baseColor
            Write-Host $mark -NoNewline -ForegroundColor $markColor
            $rest = " $name"
            $pad = [Console]::WindowWidth - 2 - 4 - (Get-DisplayWidth $rest)
            if ($pad -lt 0) { $pad = 0 }
            Write-Host ($rest + (' ' * $pad)) -ForegroundColor $baseColor
        }

        # 底部溢出指示
        if ($paging) {
            $below = $count - ($script:viewStart + $itemRows)
            if ($below -gt 0) {
                Write-Host (Pad-ToFullWidth ("    ↓ 下方还有 {0} 项" -f $below)) -ForegroundColor DarkGray
            } else {
                Write-Host (Pad-ToFullWidth "")
            }
        }

        $selCount = ($checked | Where-Object { $_ }).Count
        Write-Host (Pad-ToFullWidth "  已选 $selCount / $count")

        # 滚屏自校正：本帧若引发滚屏（窗口过矮），立即修正起始行，避免后续重绘错位、列表重复
        $rowsRendered = $indicatorRows + $itemRows + 1
        $scrolled = ($script:menuTop + $rowsRendered) - [Console]::CursorTop
        if ($scrolled -gt 0) {
            $script:menuTop -= $scrolled
            if ($script:menuTop -lt 0) { $script:menuTop = 0 }
        }
    }

    # 记录菜单起始行并首次渲染
    $script:menuTop = [Console]::CursorTop
    Render-Menu

    # 键盘循环
    while ($true) {
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq [ConsoleKey]::UpArrow) {
            $cursor = ($cursor - 1 + $count) % $count
        } elseif ($key.Key -eq [ConsoleKey]::DownArrow) {
            $cursor = ($cursor + 1) % $count
        } elseif ($key.Key -eq [ConsoleKey]::Spacebar) {
            $checked[$cursor] = -not $checked[$cursor]
        } elseif ($key.Key -eq [ConsoleKey]::A -or $key.KeyChar -eq 'a' -or $key.KeyChar -eq 'A') {
            # 全选/取消全选切换
            $allChecked = ($checked | Where-Object { -not $_ }).Count -eq 0
            for ($i = 0; $i -lt $count; $i++) { $checked[$i] = -not $allChecked }
        } elseif ($key.Key -eq [ConsoleKey]::Enter) {
            break
        } elseif ($key.Key -eq [ConsoleKey]::Escape) {
            Write-Host "`n未选择任何软件，退出。"
            exit 0
        }
        Render-Menu
    }

    # 收集选中项
    $selected = @()
    for ($i = 0; $i -lt $count; $i++) {
        if ($checked[$i]) { $selected += $softwareList[$i] }
    }

    if ($selected.Count -eq 0) {
        Write-Host "`n未选择任何软件，退出。"
        exit 0
    }

    Write-Host ("`n已选择: " + (($selected | ForEach-Object { $_.name }) -join ", "))

    $dirInput = Read-Host "请输入下载目录（回车使用默认: $defaultDir）"
    $downloadDir = if ([string]::IsNullOrWhiteSpace($dirInput)) { $defaultDir } else { $dirInput.Trim() }
    New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

    Write-Host "下载目录: $downloadDir`n"
    return @{ Selected = $selected; Dir = $downloadDir }
}

function Resolve-ApiUrl($sw) {
    # 策略2: 调用API获取最新版下载链接和版本号
    $uri = $sw.api_url
    if ($sw.params) {
        $query = ($sw.params.PSObject.Properties | ForEach-Object {
            "$($_.Name)=$([System.Uri]::EscapeDataString([string]$_.Value))"
        }) -join "&"
        $uri = if ($uri.Contains("?")) { "$uri&$query" } else { "$uri`?$query" }
    }

    try {
        $data = Invoke-RestMethod -Uri $uri -UserAgent $UserAgent -TimeoutSec 15
    } catch {
        throw "API请求失败: $($_.Exception.Message)"
    }

    if ($sw.asset_name_pattern) {
        # GitHub Releases 风格：在 assets 数组中按资产名正则匹配，取其下载地址
        $downloadUrl = $null
        foreach ($asset in @($data.assets)) {
            if ($asset.name -match $sw.asset_name_pattern) {
                $downloadUrl = $asset.browser_download_url
                break
            }
        }
        if (-not $downloadUrl) {
            throw "asset_name_pattern '$($sw.asset_name_pattern)' 未匹配到任何资产"
        }
    } else {
        $downloadUrl = Get-ByKeyPath $data $sw.link_key
        if (-not $downloadUrl) {
            throw "API返回中未找到链接字段 '$($sw.link_key)'"
        }

        # 如果结果是一个数组，通过 link_filter (regex:) 筛选
        if ($downloadUrl -is [System.Array] -and $sw.link_filter) {
            if (-not $sw.link_filter.StartsWith("regex:")) {
                throw "link_filter 仅支持 'regex:' 前缀"
            }
            $pattern = $sw.link_filter.Substring(6)
            $matched = $null
            foreach ($item in $downloadUrl) {
                $candidate = $item.browser_download_url
                if (-not $candidate) { $candidate = $item.downloadUrl }
                if (-not $candidate) { $candidate = $item.url }
                if ($candidate -and ($candidate -match $pattern)) {
                    $matched = $candidate
                    break
                }
            }
            if (-not $matched) { throw "link_filter '$($sw.link_filter)' 未匹配到任何项" }
            $downloadUrl = $matched
        }

        if ($downloadUrl -isnot [string]) {
            throw "无法从 '$($sw.link_key)' 提取有效下载链接"
        }
    }

    $version = ""
    if ($sw.version_key) {
        $version = [string](Get-ByKeyPath $data $sw.version_key)
    }

    $filename = $sw.filename
    if ($filename.Contains("{version}")) {
        $filename = $filename.Replace("{version}", $(if ($version) { $version } else { "latest" }))
    }

    return @{ Url = $downloadUrl; Filename = $filename }
}

function Resolve-PageParse($sw) {
    # 策略3: 解析HTML页面获取下载链接（支持 link_pattern 正则）
    try {
        $resp = Invoke-WebRequest -Uri $sw.download_page -UserAgent $UserAgent -TimeoutSec 15 -UseBasicParsing
    } catch {
        throw "页面请求失败: $($_.Exception.Message)"
    }

    $downloadUrl = $null
    if ($sw.link_pattern) {
        $m = [regex]::Match($resp.Content, $sw.link_pattern)
        if ($m.Success) { $downloadUrl = $m.Value }
    }

    if (-not $downloadUrl) {
        throw "无法从页面解析下载链接: $($sw.download_page)"
    }

    return @{ Url = $downloadUrl; Filename = $sw.filename }
}

function Resolve-DownloadUrl($sw) {
    # 根据软件类型解析下载链接
    Write-Host "  [$($sw.name)] 正在获取最新版下载链接..."
    switch ($sw.type) {
        "fixed_url"  { return @{ Url = $sw.url; Filename = $sw.filename } }
        "api"        { return Resolve-ApiUrl $sw }
        "page_parse" { return Resolve-PageParse $sw }
        default      { throw "未知的下载类型: $($sw.type)" }
    }
}

function Download-File([string]$url, [string]$filepath, [string]$name) {
    # 下载文件，带进度条和重试
    $maxRetries = 3
    for ($attempt = 1; $attempt -le $maxRetries; $attempt++) {
        $respStream = $null; $fileStream = $null; $resp = $null
        try {
            $req = [System.Net.HttpWebRequest]::Create($url)
            $req.UserAgent = $UserAgent
            $req.Timeout = 30000
            $req.ReadWriteTimeout = 30000
            $req.AllowAutoRedirect = $true
            $resp = $req.GetResponse()

            $totalSize = $resp.ContentLength   # 未知时为 -1
            $respStream = $resp.GetResponseStream()
            $fileStream = [System.IO.File]::Create($filepath)

            $buffer = New-Object byte[] $ChunkSize
            $downloaded = 0
            $timer = [System.Diagnostics.Stopwatch]::StartNew()
            $lastPrint = 0.0

            while (($read = $respStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $fileStream.Write($buffer, 0, $read)
                $downloaded += $read

                # 每 200ms 刷新一次进度，避免频繁输出拖慢下载
                if ($totalSize -gt 0 -and ($timer.Elapsed.TotalMilliseconds - $lastPrint) -gt 200) {
                    $lastPrint = $timer.Elapsed.TotalMilliseconds
                    $percent = $downloaded / $totalSize * 100
                    $speed = if ($timer.Elapsed.TotalSeconds -gt 0) { $downloaded / $timer.Elapsed.TotalSeconds / 1MB } else { 0 }
                    $barLen = 30
                    $filled = [int]($barLen * $downloaded / $totalSize)
                    $bar = ("#" * $filled) + ("-" * ($barLen - $filled))
                    Write-Host ("`r  [{0}] [{1}] {2:0.0}% ({3:0.0}/{4:0.0}MB, {5:0.0}MB/s)   " -f `
                        $name, $bar, $percent, ($downloaded / 1MB), ($totalSize / 1MB), $speed) -NoNewline
                }
            }
            Write-Host ""
            return $true

        } catch {
            Write-Host "`n  [$name] 第${attempt}次下载失败: $($_.Exception.Message)" -ForegroundColor Yellow
            if ($attempt -lt $maxRetries) {
                Write-Host "  [$name] 等待3秒后重试..."
                Start-Sleep -Seconds 3
            } else {
                return $false
            }
        } finally {
            if ($fileStream) { $fileStream.Close() }
            if ($respStream) { $respStream.Close() }
            if ($resp) { $resp.Close() }
        }
    }
    return $false
}

function Verify-File([string]$filepath, [string]$name) {
    # 验证下载的文件是否有效
    if (-not (Test-Path $filepath)) { return $false }
    $sizeMB = (Get-Item $filepath).Length / 1MB
    if ($sizeMB -lt 0.5) {
        Write-Host ("  [{0}] 警告: 文件过小 ({1:0.00}MB)，可能下载不完整" -f $name, $sizeMB) -ForegroundColor Yellow
        return $false
    }
    Write-Host ("  [{0}] 下载完成，文件大小: {1:0.00}MB" -f $name, $sizeMB) -ForegroundColor Green
    return $true
}

function Process-Software($sw, [string]$downloadDir) {
    # 处理单个软件的下载流程
    $name = $sw.name
    try {
        $resolved = Resolve-DownloadUrl $sw
        $shortUrl = if ($resolved.Url.Length -gt 80) { $resolved.Url.Substring(0, 80) + "..." } else { $resolved.Url }
        Write-Host "  [$name] 链接: $shortUrl"
        Write-Host "  [$name] 文件名: $($resolved.Filename)"

        $filepath = Join-Path $downloadDir $resolved.Filename
        Write-Host "  [$name] 开始下载..."
        if (-not (Download-File $resolved.Url $filepath $name)) {
            Write-Host "  [$name] 下载失败!" -ForegroundColor Red
            return $false
        }

        return (Verify-File $filepath $name)

    } catch {
        Write-Host "  [$name] 错误: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

# ---------- 主流程 ----------
$config = Load-Config
$allSoftware = @($config.software | Where-Object { $_.enabled -ne $false })
$defaultDir = Join-Path $ScriptDir $config.download_dir

# -List：列出软件后退出
if ($List) {
    Write-Host "`n可用软件列表:"
    Write-Host ("-" * 60)
    foreach ($sw in $config.software) {
        $status = if ($sw.enabled -ne $false) { "[启用]" } else { "[禁用]" }
        Write-Host ("  {0,-20} {1} {2} ({3})" -f $sw.id, $status, $sw.name, $sw.type)
    }
    Write-Host ("-" * 60)
    exit 0
}

if ($Ids.Count -gt 0 -or $Exclude.Count -gt 0) {
    # CLI 模式（支持逗号分隔：-Ids wechat,qq 或 -Ids wechat -Ids qq）
    $idSet = @($Ids | ForEach-Object { $_.Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $exSet = @($Exclude | ForEach-Object { $_.Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $selected = @($allSoftware | Where-Object {
        ($idSet.Count -eq 0 -or $idSet -contains $_.id) -and ($exSet -notcontains $_.id)
    })
    $downloadDir = if ($Dir) { $Dir } else { $defaultDir }
    New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null

    if ($selected.Count -eq 0) {
        Write-Host "没有需要下载的软件。使用 -List 查看可用列表。"
        exit 0
    }

    Write-Host ""
    Write-Host ("=" * 60)
    Write-Host "准备下载 $($selected.Count) 个软件到: $downloadDir"
    Write-Host ("=" * 60)

    Check-Network $selected
} else {
    # 交互模式
    if ($allSoftware.Count -eq 0) {
        Write-Host "没有可用的软件。"
        exit 0
    }
    $result = Select-Interactive $allSoftware $defaultDir
    $selected = $result.Selected
    $downloadDir = $result.Dir

    Check-Network $selected
}

# 开始并发下载
$maxWorkers = if ($config.max_workers) { [int]$config.max_workers } else { 3 }
Write-Host "[下载] 并发数: $maxWorkers，共 $($selected.Count) 个任务`n"

# 构建 Runspace 池
$pool = [RunspaceFactory]::CreateRunspacePool(1, $maxWorkers)
$pool.Open()

# 每个任务的脚本块（在独立 Runspace 中执行）
$jobScript = {
    param($sw, $downloadDir, $UserAgent, $ChunkSize, $logQueue)

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls11 -bor [Net.SecurityProtocolType]::Tls

    # 实时日志：直接入队，由主线程即时打印，避免下载/重试期间控制台无反馈
    function Send-Log([string]$text, [string]$color = "") {
        $logQueue.Enqueue(@{ Text = $text; Color = $color })
    }

    function Get-ByKeyPath($data, [string]$keyPath) {
        foreach ($k in $keyPath.Split('.')) {
            if ($null -eq $data) { return $null }
            $data = $data.$k
        }
        return $data
    }

    $name = $sw.name

    try {
        # 解析下载链接
        Send-Log "  [$name] 正在获取最新版下载链接..."
        $downloadUrl = $null; $filename = $null

        switch ($sw.type) {
            "fixed_url" {
                $downloadUrl = $sw.url
                $filename = $sw.filename
            }
            "api" {
                $uri = $sw.api_url
                if ($sw.params) {
                    $query = ($sw.params.PSObject.Properties | ForEach-Object {
                        "$($_.Name)=$([System.Uri]::EscapeDataString([string]$_.Value))"
                    }) -join "&"
                    $uri = if ($uri.Contains("?")) { "$uri&$query" } else { "$uri`?$query" }
                }
                $data = Invoke-RestMethod -Uri $uri -UserAgent $UserAgent -TimeoutSec 15
                if ($sw.asset_name_pattern) {
                    # GitHub Releases 风格：按资产名正则匹配，取其下载地址
                    $downloadUrl = $null
                    foreach ($asset in @($data.assets)) {
                        if ($asset.name -match $sw.asset_name_pattern) { $downloadUrl = $asset.browser_download_url; break }
                    }
                    if (-not $downloadUrl) { throw "asset_name_pattern '$($sw.asset_name_pattern)' 未匹配到任何资产" }
                } else {
                    $downloadUrl = Get-ByKeyPath $data $sw.link_key
                    if ($downloadUrl -is [System.Array] -and $sw.link_filter -and $sw.link_filter.StartsWith("regex:")) {
                        $pattern = $sw.link_filter.Substring(6)
                        foreach ($item in $downloadUrl) {
                            $c = $item.browser_download_url; if (-not $c) { $c = $item.downloadUrl }; if (-not $c) { $c = $item.url }
                            if ($c -and ($c -match $pattern)) { $downloadUrl = $c; break }
                        }
                    }
                }
                $version = ""; if ($sw.version_key) { $version = [string](Get-ByKeyPath $data $sw.version_key) }
                $filename = $sw.filename
                if ($filename.Contains("{version}")) { $filename = $filename.Replace("{version}", $(if ($version) { $version } else { "latest" })) }
            }
            "page_parse" {
                $resp = Invoke-WebRequest -Uri $sw.download_page -UserAgent $UserAgent -TimeoutSec 15 -UseBasicParsing
                if ($sw.link_pattern) {
                    $m = [regex]::Match($resp.Content, $sw.link_pattern)
                    if ($m.Success) { $downloadUrl = $m.Value }
                }
                $filename = $sw.filename
            }
            default { throw "未知的下载类型: $($sw.type)" }
        }

        if (-not $downloadUrl) { throw "无法解析下载链接" }
        Send-Log "  [$name] 文件名: $filename"

        # 下载（带重试）
        $filepath = Join-Path $downloadDir $filename
        $ok = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $respStream = $null; $fileStream = $null; $resp = $null
            try {
                $req = [System.Net.HttpWebRequest]::Create($downloadUrl)
                $req.UserAgent = $UserAgent
                $req.Timeout = 30000
                $req.ReadWriteTimeout = 30000
                $req.AllowAutoRedirect = $true
                $resp = $req.GetResponse()
                # 防呆：链接失效/被反爬时服务器常返回 HTML 页面而非安装包，直接报错而不是保存垃圾文件
                if ($resp.ContentType -match 'html') {
                    throw "下载链接返回的是 HTML 页面（链接可能已失效或需要人机验证）"
                }
                $totalSize = $resp.ContentLength
                if ($attempt -eq 1) {
                    if ($totalSize -gt 0) {
                        Send-Log ("  [$name] 开始下载，文件大小约 {0:0.0}MB" -f ($totalSize / 1MB))
                    } else {
                        Send-Log "  [$name] 开始下载（大小未知）..."
                    }
                }
                $respStream = $resp.GetResponseStream()
                $fileStream = [System.IO.File]::Create($filepath)
                $buffer = New-Object byte[] $ChunkSize
                $downloaded = 0
                $nextReportPct = 20
                $nextReportMB = 30
                while (($read = $respStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $fileStream.Write($buffer, 0, $read)
                    $downloaded += $read
                    # 分段进度反馈：已知大小每 20% 报一次；未知大小每 30MB 报一次
                    if ($totalSize -gt 0) {
                        $pct = [int]($downloaded * 100 / $totalSize)
                        if ($pct -ge $nextReportPct) {
                            Send-Log ("  [$name] 下载中 {0}% ({1:0.0}/{2:0.0}MB)" -f $pct, ($downloaded / 1MB), ($totalSize / 1MB)) "DarkGray"
                            $nextReportPct += 20
                        }
                    } elseif ($downloaded -ge $nextReportMB * 1MB) {
                        Send-Log ("  [$name] 已下载 {0:0.0}MB ..." -f ($downloaded / 1MB)) "DarkGray"
                        $nextReportMB += 30
                    }
                }
                $ok = $true
                break
            } catch {
                $errMsg = $_.Exception.Message
                if ($attempt -lt 3) {
                    Send-Log "  [$name] 第 $attempt 次下载失败: $errMsg" "Yellow"
                    Send-Log "  [$name] 3 秒后进行第 $($attempt + 1) 次重试..." "Yellow"
                    Start-Sleep -Seconds 3
                } else {
                    Send-Log "  [$name] 3 次尝试均失败: $errMsg" "Red"
                }
            } finally {
                if ($fileStream) { $fileStream.Close() }
                if ($respStream) { $respStream.Close() }
                if ($resp) { $resp.Close() }
            }
        }

        if (-not $ok) { return @{ Name = $name; Success = $false } }

        # 校验
        $sizeMB = (Get-Item $filepath).Length / 1MB
        if ($sizeMB -lt 0.5) {
            Send-Log "  [$name] 警告: 文件过小 ($('{0:0.00}' -f $sizeMB)MB)，可能下载不完整" "Yellow"
            return @{ Name = $name; Success = $false }
        }
        Send-Log "  [$name] 下载完成 ($('{0:0.00}' -f $sizeMB)MB)" "Green"
        return @{ Name = $name; Success = $true }

    } catch {
        Send-Log "  [$name] 错误: $($_.Exception.Message)" "Red"
        return @{ Name = $name; Success = $false }
    }
}

# 实时日志队列（线程安全：各下载任务入队，主线程出队打印）
$logQueue = [System.Collections.Concurrent.ConcurrentQueue[hashtable]]::new()

function Flush-JobLogs {
    # 将队列中的任务日志即时输出，返回输出条数
    $n = 0
    $item = $null
    while ($logQueue.TryDequeue([ref]$item)) {
        $n++
        if ($item.Color) { Write-Host $item.Text -ForegroundColor $item.Color }
        else { Write-Host $item.Text }
    }
    return $n
}

# 提交所有任务
$jobs = @()
foreach ($sw in $selected) {
    $ps = [PowerShell]::Create()
    $ps.RunspacePool = $pool
    [void]$ps.AddScript($jobScript).AddArgument($sw).AddArgument($downloadDir).AddArgument($UserAgent).AddArgument($ChunkSize).AddArgument($logQueue)
    $handle = $ps.BeginInvoke()
    $jobs += @{ PS = $ps; Handle = $handle; Name = $sw.name }
}

# 收集结果（实时输出任务日志 + 完成状态）
$successCount = 0
$failCount = 0
$pending = @($jobs)
$heartbeat = [System.Diagnostics.Stopwatch]::StartNew()
while ($pending.Count -gt 0) {
    if ((Flush-JobLogs) -gt 0) { $heartbeat.Restart() }

    $done = @()
    foreach ($j in $pending) {
        if ($j.Handle.IsCompleted) {
            [void](Flush-JobLogs)   # 先输出该任务的收尾日志，再打印成败状态
            $result = $j.PS.EndInvoke($j.Handle)
            $j.PS.Dispose()
            if ($result) {
                if ($result.Success) {
                    Write-Host "  [$($result.Name)] √ 成功" -ForegroundColor Green
                    $successCount++
                } else {
                    Write-Host "  [$($result.Name)] × 失败" -ForegroundColor Red
                    $failCount++
                }
            }
            $done += $j
            $heartbeat.Restart()
        }
    }
    if ($done.Count -gt 0) {
        $pending = @($pending | Where-Object { $done -notcontains $_ })
    } else {
        # 超过 10 秒无任何输出时给出心跳提示，避免误以为程序卡死
        if ($heartbeat.Elapsed.TotalSeconds -ge 10) {
            $heartbeat.Restart()
            $activeNames = ($pending | ForEach-Object { $_.Name }) -join ", "
            Write-Host "  [下载] 仍在进行，请稍候... (剩余: $activeNames)" -ForegroundColor DarkGray
        }
        Start-Sleep -Milliseconds 200
    }
}
[void](Flush-JobLogs)

$pool.Close()
$pool.Dispose()

Write-Host ""
Write-Host ("=" * 60)
Write-Host "下载完成: 成功 $successCount 个, 失败 $failCount 个"
Write-Host "文件保存位置: $downloadDir"
Write-Host ("=" * 60)
