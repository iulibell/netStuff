# ============================================================
# Batch write SNMP config into saved eNSP device configs
#   - handles plain vrpcfg.cfg and vrpcfg.zip (vrpcfg.cfg inside)
#   - skips devices that already have 'snmp-agent community'
#   - creates .bak backup next to each file before modifying
#   - RUN ONLY WHEN eNSP IS STOPPED!
# ============================================================
param([string]$TrapHostIP = '192.168.88.10')   # Trap 上报目标 = 监控主机 IP，按实际修改

$ErrorActionPreference = 'Stop'
$root = 'c:\Users\Administrator\Desktop\ENterprise_TOPO'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$trapLine = "snmp-agent target-host trap address udp-domain $TrapHostIP params securityname Monitor@123 v2c"

$cfgDevices = [ordered]@{
  '88DE0DA8-2060-41BB-85BA-EA93A7ACD5AD' = 'BACKBONE1'
  '734DC8D3-9110-4a85-819C-8E75C1A4DB8D' = 'ISP1'
  '6EE597CD-E96D-436a-A135-7F7C16E0E0B9' = 'ISP2'
  '4756FDE7-8738-42ae-B50E-C1331277D815' = 'ISP3'
  '2F8E13AA-443F-4447-846C-264D4C4C2114' = 'ISP4'
  '09504C22-8F43-4D87-A9C5-6B26813314C3' = 'METRO_A1'
  '75FD5566-9464-4410-A90B-D49FFB09BF8D' = 'METRO_A2'
  '8BF3962E-39E5-4E2C-A797-2387A8B049CB' = 'METRO_B1'
  '5E9881B4-6C9A-430D-B9B0-37DD58E5D9B3' = 'METRO_B2'
  '4519A37B-597A-4da1-9D25-A0C663C9E6EF' = 'HEAD_FW1'
  '9B9060EE-B09C-408b-A6C9-A61877740D9F' = 'HEAD_FW2'
  'B352D99B-0235-45fd-88E9-17B99D15ACFC' = 'SERVER_FW'
  '5A4C05D9-926A-4b54-B925-873C009DA3C0' = 'DIVI_FW'
  '65EA401E-E6AA-4dc3-9A96-C2FC2FEDD225' = 'DHCP'
  '69AAA15F-3002-42F1-973F-781444EB8CC2' = 'BR_CORE1'
  'BC9BA740-89E1-4576-B97F-C8E46F3C6EE1' = 'BR_CORE2'
}

$zipDevices = [ordered]@{
  '438F5EDA-C271-454f-9FE5-10701152DEDE' = 'BACKBONE2'
  'F92D65F0-32A7-46a2-8F48-A4540B3923E6' = 'BACKBONE3'
  '06135134-2879-4368-BA26-B0A67529CA45' = 'BACKBONE4'
  'A66553B0-2A84-4abe-A4EA-B4767DA52202' = 'BACKBONE5'
  '1E71BC76-883E-48f0-8137-AE69F9561EE7' = 'HEAD_AC1'
}

function Add-SnmpBlock([string]$text) {
  # 幂等：community / trap-target 缺什么补什么，都有则跳过
  $add = ''
  if ($text -notmatch 'snmp-agent community') {
    $add += "#`nsnmp-agent`nsnmp-agent community read cipher Monitor@123`nsnmp-agent sys-info version v2c`n"
  }
  if ($text -notmatch 'snmp-agent target-host trap') {
    $add += "$trapLine`n"
  }
  if (-not $add) { return $null }
  if ($text -match '(?m)^return\s*$') {
    return [regex]::Replace($text, '(?m)^return\s*$', ($add + "return"), 1)
  } else {
    return $text.TrimEnd() + "`n" + $add
  }
}

$done = @(); $skipped = @(); $failed = @()

foreach ($guid in $cfgDevices.Keys) {
  $name = $cfgDevices[$guid]
  $path = Join-Path $root "$guid\vrpcfg.cfg"
  try {
    if (-not (Test-Path $path)) { $failed += "$name : file missing"; continue }
    $text = [System.IO.File]::ReadAllText($path)
    $new = Add-SnmpBlock $text
    if ($null -eq $new) { $skipped += "$name (community exists)"; continue }
    [System.IO.File]::Copy($path, "$path.bak", $true)
    [System.IO.File]::WriteAllText($path, $new, $utf8NoBom)
    $done += $name
  } catch { $failed += "$name : $($_.Exception.Message)" }
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
foreach ($guid in $zipDevices.Keys) {
  $name = $zipDevices[$guid]
  $path = Join-Path $root "$guid\vrpcfg.zip"
  $zip = $null
  try {
    if (-not (Test-Path $path)) { $failed += "$name : file missing"; continue }
    [System.IO.File]::Copy($path, "$path.bak", $true)
    $zip = [System.IO.Compression.ZipFile]::Open($path, 'Update')
    $entry = $zip.Entries | Where-Object { $_.FullName -match 'vrpcfg\.cfg$' } | Select-Object -First 1
    if (-not $entry) { $zip.Dispose(); $failed += "$name : no vrpcfg.cfg in zip"; continue }
    $sr = New-Object System.IO.StreamReader($entry.Open())
    $text = $sr.ReadToEnd(); $sr.Close()
    $new = Add-SnmpBlock $text
    if ($null -eq $new) { $zip.Dispose(); $skipped += "$name (community exists)"; continue }
    $entry.Delete()
    $newEntry = $zip.CreateEntry('vrpcfg.cfg')
    $sw = New-Object System.IO.StreamWriter($newEntry.Open(), $utf8NoBom)
    $sw.Write($new); $sw.Close()
    $zip.Dispose()
    $done += $name
  } catch { if ($zip) { $zip.Dispose() }; $failed += "$name : $($_.Exception.Message)" }
}

"===== WRITTEN ($($done.Count)) ====="
$done | ForEach-Object { "  $_" }
if ($skipped.Count) { "===== SKIPPED ($($skipped.Count)) ====="; $skipped | ForEach-Object { "  $_" } }
if ($failed.Count)  { "===== FAILED ($($failed.Count)) =====";  $failed  | ForEach-Object { "  $_" } }
