param(
  [string]$OpenDesignRoot,
  [string]$CodexConfig,
  [string]$ServerName = "open-design",
  [string]$NodePath,
  [string]$Namespace = "default",
  [string]$DaemonUrl,
  [switch]$NoHealthCheck,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Resolve-ExistingPath($PathValue, $Label) {
  if (-not $PathValue) {
    return $null
  }
  $item = Get-Item -LiteralPath $PathValue -ErrorAction Stop
  return $item.FullName
}

function Find-OpenDesignRoot {
  param([string]$RequestedRoot)

  $candidates = New-Object System.Collections.Generic.List[string]
  if ($RequestedRoot) {
    [void]$candidates.Add($RequestedRoot)
  }

  $current = (Get-Location).Path
  while ($current) {
    [void]$candidates.Add($current)
    $parent = Split-Path -Parent $current
    if (-not $parent -or $parent -eq $current) {
      break
    }
    $current = $parent
  }

  [void]$candidates.Add("D:\open-design\open-design-run")
  [void]$candidates.Add("D:\open-design\open-design")

  foreach ($candidate in $candidates) {
    if (-not $candidate) {
      continue
    }
    if (-not (Test-Path -LiteralPath $candidate)) {
      continue
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    $cliPath = Join-Path $resolved "apps\daemon\dist\cli.js"
    if (Test-Path -LiteralPath $cliPath) {
      return $resolved
    }
  }

  throw "Could not find an Open Design root with apps\daemon\dist\cli.js. Pass -OpenDesignRoot."
}

function Find-Node {
  param([string]$RequestedNode)

  if ($RequestedNode) {
    return (Get-Item -LiteralPath $RequestedNode -ErrorAction Stop).FullName
  }

  $cmd = Get-Command node.exe -ErrorAction SilentlyContinue
  if (-not $cmd) {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
  }
  if (-not $cmd) {
    throw "Node.js was not found on PATH. Pass -NodePath."
  }
  return $cmd.Source
}

function Default-CodexConfigPath {
  if ($CodexConfig) {
    return $CodexConfig
  }
  if ($env:CODEX_HOME) {
    return (Join-Path $env:CODEX_HOME "config.toml")
  }
  return (Join-Path $env:USERPROFILE ".codex\config.toml")
}

function TomlLiteral {
  param([string]$Value)
  if ($Value -match "'") {
    throw "TOML literal string cannot contain a single quote: $Value"
  }
  return "'$Value'"
}

function Is-TargetTable {
  param([string]$Line, [string]$Name)
  $escapedName = [regex]::Escape($Name)
  return ($Line -match "^\s*\[mcp_servers\.`"$escapedName`"\]\s*$" -or
          $Line -match "^\s*\[mcp_servers\.$escapedName\]\s*$" -or
          $Line -match "^\s*\[mcp_servers\.`"$escapedName`"\.env\]\s*$" -or
          $Line -match "^\s*\[mcp_servers\.$escapedName\.env\]\s*$")
}

function Remove-ExistingServerBlocks {
  param([string[]]$Lines, [string]$Name)

  $result = New-Object System.Collections.Generic.List[string]
  $skip = $false
  foreach ($line in $Lines) {
    if (Is-TargetTable -Line $line -Name $Name) {
      $skip = $true
      continue
    }
    if ($skip -and $line -match "^\s*\[") {
      $skip = $false
    }
    if (-not $skip) {
      [void]$result.Add($line)
    }
  }
  return $result.ToArray()
}

function Build-McpBlock {
  param(
    [string]$Name,
    [string]$Node,
    [string]$Cli,
    [string]$DataDir,
    [string]$PipePath,
    [string]$HealthUrl
  )

  $lines = New-Object System.Collections.Generic.List[string]
  [void]$lines.Add("")
  [void]$lines.Add("[mcp_servers.`"$Name`"]")
  [void]$lines.Add("type = `"stdio`"")
  [void]$lines.Add("command = $(TomlLiteral $Node)")
  [void]$lines.Add("args = [$(TomlLiteral $Cli), 'mcp']")
  [void]$lines.Add("startup_timeout_sec = 120")
  [void]$lines.Add("")
  [void]$lines.Add("[mcp_servers.`"$Name`".env]")
  [void]$lines.Add("OD_DATA_DIR = $(TomlLiteral $DataDir)")
  [void]$lines.Add("OD_SIDECAR_IPC_PATH = $(TomlLiteral $PipePath)")
  if ($HealthUrl) {
    [void]$lines.Add("OD_DAEMON_URL = $(TomlLiteral $HealthUrl)")
  }
  return $lines.ToArray()
}

if ($ServerName -notmatch "^[A-Za-z0-9_-]+$") {
  throw "ServerName must contain only letters, digits, underscore, or hyphen."
}

$root = Find-OpenDesignRoot -RequestedRoot $OpenDesignRoot
$node = Find-Node -RequestedNode $NodePath
$config = Default-CodexConfigPath
$configDir = Split-Path -Parent $config
if (-not (Test-Path -LiteralPath $configDir)) {
  New-Item -ItemType Directory -Path $configDir | Out-Null
}
if (-not (Test-Path -LiteralPath $config)) {
  "[mcp_servers]`r`n" | Set-Content -LiteralPath $config -Encoding UTF8
}

$cli = Join-Path $root "apps\daemon\dist\cli.js"
$dataDir = Join-Path $root ".od"
$pipePath = "\\.\pipe\open-design-$Namespace-daemon"

& $node $cli mcp --help *> $null

if ($DaemonUrl -and -not $NoHealthCheck) {
  $health = $DaemonUrl.TrimEnd("/") + "/api/health"
  $response = Invoke-WebRequest -UseBasicParsing -Uri $health -TimeoutSec 15
  if ($response.StatusCode -ne 200) {
    throw "Open Design daemon health check failed: HTTP $($response.StatusCode)"
  }
}

$original = Get-Content -LiteralPath $config -ErrorAction Stop
$withoutServer = Remove-ExistingServerBlocks -Lines $original -Name $ServerName
$block = Build-McpBlock -Name $ServerName -Node $node -Cli $cli -DataDir $dataDir -PipePath $pipePath -HealthUrl $DaemonUrl
$updated = @($withoutServer) + $block

if ($DryRun) {
  Write-Output "Would update: $config"
  Write-Output "Open Design root: $root"
  Write-Output "Node: $node"
  Write-Output "Server name: $ServerName"
  Write-Output "Block:"
  $block | ForEach-Object { Write-Output $_ }
  exit 0
}

$timestamp = Get-Date -Format "yyyyMMddHHmmss"
$backup = "$config.bak-open-design-mcp-$timestamp"
Copy-Item -LiteralPath $config -Destination $backup
[System.IO.File]::WriteAllText($config, (($updated -join "`r`n") + "`r`n"), [System.Text.UTF8Encoding]::new($false))

Write-Output "Registered Open Design MCP server '$ServerName'."
Write-Output "Config: $config"
Write-Output "Backup: $backup"
Write-Output "Open Design root: $root"
Write-Output "Restart Codex or open a new Codex session to load the MCP server."
