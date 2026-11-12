<#
Gjallarhorn: Heimdall's alerts on a trader's screen.

Windows PowerShell 5.1 and WPF, both part of Windows: nothing to install. It reads
the Gjallarhorn folder on the share, pops a card at the bottom right for each new
alert meant for this trader (by Windows user name or desk), keeps every alert
waiting to be acknowledged in a list to go through, and writes acknowledgements
back to the share, where Heimdall records them.

    powershell -NoProfile -STA -ExecutionPolicy Bypass -File gjallarhorn.ps1 -Desk "Equities,Rates"

    -Root      the Gjallarhorn folder (default: the folder above this script's)
    -Desk      the desks this PC is for, comma separated (alerts can be meant for a desk)
    -Open      open the list of alerts (starts Gjallarhorn if it is not running)
    -Test      show a sample pop-up, without touching the share
    -DryRun    no window: print what would pop up as JSON lines (for testing)
    -Once      with -DryRun: one pass, then exit
    -AckId     with -DryRun: acknowledge this alert id, then exit
    -WriteIcon write Gjallarhorn's icon (.ico) to this path, then exit (used by the installer)

Usually started at logon by install-gjallarhorn.ps1. The file is plain ASCII on
purpose: Windows PowerShell reads scripts without a BOM in the local code page.
#>
param(
    [string]$Root = "",
    [string]$Desk = "",
    [int]$PollSeconds = 5,
    [int]$LookbackDays = 3,
    [int]$CatchUpMinutes = 60,
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA "Gjallarhorn"),
    [switch]$Open,
    [switch]$Test,
    [switch]$DryRun,
    [switch]$Once,
    [string]$AckId = "",
    [string]$WriteIcon = "",
    [switch]$NoSound
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }    # the app lives in <root>\app
$User = $env:USERNAME
$Desks = @($Desk -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$AlertsDir = Join-Path $Root "alerts"
$AcksDir = Join-Path $Root "acks"
[void][IO.Directory]::CreateDirectory($StateDir)
$StateFile = Join-Path $StateDir "state.json"
$LogFile = Join-Path $StateDir "gjallarhorn.log"
$Utf8 = New-Object Text.UTF8Encoding $false
$Invariant = [Globalization.CultureInfo]::InvariantCulture

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
    try { [IO.File]::AppendAllText($LogFile, $line + "`r`n", $Utf8) } catch {}
}

# -- reading the share ----------------------------------------------------------------

function Test-ForMe($a) {
    # recipients: the audience with Heimdall's groups opened up into user names.
    # Files written before groups existed only have the audience.
    $who = if ($a.PSObject.Properties["recipients"]) { $a.recipients } else { $a.audience }
    $aud = @($who | Where-Object { $_ })
    if ($aud.Count -eq 0) { return $true }
    foreach ($x in $aud) {
        if ([string]$x -ieq $User) { return $true }
        foreach ($d in $Desks) { if ([string]$x -ieq $d) { return $true } }
    }
    return $false
}

function ConvertTo-Local($v) {
    if ($v -is [datetime]) { return $v.ToLocalTime() }
    try { return [DateTimeOffset]::Parse([string]$v, $Invariant).LocalDateTime } catch { return Get-Date }
}

function ConvertTo-GjItem($a) {
    $rows = $a.rows
    [pscustomobject]@{
        id        = [string]$a.id
        alert     = [string]$a.alert
        group     = [string]$a.group
        kind      = [string]$a.kind
        severity  = $(if (@("info", "warning", "critical") -contains [string]$a.severity) { [string]$a.severity } else { "warning" })
        title     = [string]$a.title
        summary   = [string]$a.summary
        lines     = @($a.lines | ForEach-Object { [string]$_ })
        audience  = @($a.audience | ForEach-Object { [string]$_ })
        columns   = @($(if ($rows) { $rows.columns } else { @() }))
        data      = @($(if ($rows) { $rows.data } else { @() }))
        total     = $(if ($rows -and $rows.total) { [int]$rows.total } else { 0 })
        needs_ack = [bool]$a.needs_ack
        sound     = $(if ($a.sound) { [string]$a.sound } else { "horn:" + $(if (@("info", "warning", "critical") -contains [string]$a.severity) { [string]$a.severity } else { "warning" }) })
        fired     = ConvertTo-Local $a.fired_at
        ackBy     = ""
        ackAt     = $null
        isLocal   = $false
    }
}

$Cache = @{}      # file -> @{ stamp; item }
function Read-Alerts {
    $items = New-Object System.Collections.ArrayList
    $today = [DateTime]::UtcNow
    for ($i = 0; $i -lt $LookbackDays; $i++) {
        $dir = Join-Path $AlertsDir $today.AddDays(-$i).ToString("yyyy-MM-dd", $Invariant)
        if (-not [IO.Directory]::Exists($dir)) { continue }
        foreach ($f in [IO.Directory]::GetFiles($dir, "*.json")) {
            if ([IO.Path]::GetFileName($f).StartsWith(".")) { continue }
            $stamp = [IO.File]::GetLastWriteTimeUtc($f).Ticks
            $c = $Cache[$f]
            if ($c -and $c.stamp -eq $stamp) {
                if ($c.item) { [void]$items.Add($c.item) }
                continue
            }
            $item = $null
            try {
                $a = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8) | ConvertFrom-Json
                if (Test-ForMe $a) { $item = ConvertTo-GjItem $a }
            } catch { Write-Log "unreadable ${f}: $_" }
            $Cache[$f] = @{ stamp = $stamp; item = $item }
            if ($item) { [void]$items.Add($item) }
        }
    }
    return , $items
}

function Update-Acks($items) {
    if (-not [IO.Directory]::Exists($AcksDir)) { return }
    foreach ($it in $items) {
        if ($it.ackBy -or -not $it.needs_ack -or $it.isLocal) { continue }
        $d = Join-Path $AcksDir $it.id
        if (-not [IO.Directory]::Exists($d)) { continue }
        $files = @([IO.Directory]::GetFiles($d, "*.json") | Where-Object { -not [IO.Path]::GetFileName($_).StartsWith(".") } |
                Sort-Object { [IO.File]::GetLastWriteTimeUtc($_) })
        if ($files.Count -eq 0) { continue }
        try {
            $a = [IO.File]::ReadAllText($files[0], [Text.Encoding]::UTF8) | ConvertFrom-Json
            $it.ackBy = [string]$a.user
            $it.ackAt = ConvertTo-Local $a.at
        } catch {
            $it.ackBy = [IO.Path]::GetFileNameWithoutExtension($files[0])
            $it.ackAt = [IO.File]::GetLastWriteTime($files[0])
        }
    }
}

function Send-Ack($it) {
    if (-not $it -or $it.ackBy) { return }
    if (-not $it.isLocal) {
        $d = Join-Path $AcksDir $it.id
        [void][IO.Directory]::CreateDirectory($d)
        $safe = $User -replace "[^A-Za-z0-9._@-]+", "_"
        $path = Join-Path $d "$safe.json"
        $tmp = Join-Path $d (".$safe." + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".tmp")
        $json = [ordered]@{ id = $it.id; user = $User; at = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss+00:00", $Invariant);
            machine = $env:COMPUTERNAME } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($tmp, $json, $Utf8)
        if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
        [IO.File]::Move($tmp, $path)
        Write-Log "acknowledged $($it.id) $($it.title)"
    }
    $it.ackBy = $User
    $it.ackAt = Get-Date
}

# Tell Heimdall this trader runs Gjallarhorn: <root>\users\<user>.json, refreshed every
# few minutes, so Bifrost can offer the user name when building groups.
$PresenceAt = [DateTime]::MinValue
function Write-Presence {
    if (([DateTime]::UtcNow - $script:PresenceAt).TotalMinutes -lt 5) { return }
    $script:PresenceAt = [DateTime]::UtcNow
    try {
        $d = Join-Path $Root "users"
        [void][IO.Directory]::CreateDirectory($d)
        $safe = $User -replace "[^A-Za-z0-9._@-]+", "_"
        $path = Join-Path $d "$safe.json"
        $tmp = Join-Path $d (".$safe." + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".tmp")
        $json = [ordered]@{ user = $User; machine = $env:COMPUTERNAME; desks = @($Desks);
            seen = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss+00:00", $Invariant) } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($tmp, $json, $Utf8)
        if ([IO.File]::Exists($path)) { [IO.File]::Delete($path) }
        [IO.File]::Move($tmp, $path)
    } catch { Write-Log "could not write ${Root}\users: $_" }
}

# -- what has been seen on this PC --------------------------------------------------------

function Load-State {
    $seen = New-Object 'System.Collections.Generic.HashSet[string]'
    if ([IO.File]::Exists($StateFile)) {
        try {
            $s = [IO.File]::ReadAllText($StateFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
            foreach ($x in @($s.seen)) { if ($x) { [void]$seen.Add([string]$x) } }
            return @{ seen = $seen; fresh = $false; sound = ($s.sound -ne $false) }
        } catch { Write-Log "state unreadable, starting fresh: $_" }
    }
    return @{ seen = $seen; fresh = $true; sound = $true }
}

function Save-State {
    $keep = @($Items.Keys)            # only what is still on the share
    $tmp = "$StateFile.tmp"
    [IO.File]::WriteAllText($tmp, (@{ seen = $keep; sound = [bool]$State.sound } | ConvertTo-Json -Compress), $Utf8)
    if ([IO.File]::Exists($StateFile)) { [IO.File]::Delete($StateFile) }
    [IO.File]::Move($tmp, $StateFile)
}

$State = Load-State
$Items = @{}      # id -> item, everything for this trader still on the share
$ShareOk = $true

# One pass: read the share, return the alerts to pop up now.
function Invoke-Poll {
    # A share blip must not empty the list: no folder, no change.
    if (-not [IO.Directory]::Exists($Root)) { throw "the shared folder $Root is unreachable" }
    Write-Presence
    $list = Read-Alerts
    $now = Get-Date
    $fresh = New-Object System.Collections.ArrayList
    $ids = @{}
    foreach ($it in $list) {
        $ids[$it.id] = $true
        if ($Items.ContainsKey($it.id)) { continue }
        $Items[$it.id] = $it
        if ($State.seen.Add($it.id)) {
            $age = ($now - $it.fired).TotalMinutes
            # The first start on this PC lists what is already there without popping it all.
            if (-not ($State.fresh -and $age -gt $CatchUpMinutes)) { [void]$fresh.Add($it) }
        }
    }
    foreach ($k in @($Items.Keys)) {
        if (-not $ids.ContainsKey($k) -and -not $Items[$k].isLocal) { $Items.Remove($k) }
    }
    Update-Acks @($Items.Values)
    $State.fresh = $false
    Save-State
    return , @($fresh | Where-Object { -not $_.ackBy } | Sort-Object fired)
}

function Get-Pending {
    @($Items.Values | Where-Object { $_.needs_ack -and -not $_.ackBy } |
            Sort-Object @{ Expression = { @{ critical = 0; warning = 1; info = 2 }[$_.severity] } }, @{ Expression = { $_.fired }; Descending = $true })
}

function Get-Earlier {
    @($Items.Values | Where-Object { -not ($_.needs_ack -and -not $_.ackBy) } | Sort-Object fired -Descending)
}

# -- dry run: no window, for tests ---------------------------------------------------------

if ($DryRun) {
    function Out-Event($h) { [pscustomobject]$h | ConvertTo-Json -Compress -Depth 4 | Write-Output }
    if ($AckId) {
        [void](Invoke-Poll)
        $it = $Items[$AckId]
        if (-not $it) { Out-Event @{ event = "unknown"; id = $AckId }; exit 1 }
        Send-Ack $it
        Out-Event @{ event = "acked"; id = $AckId; user = $User }
        exit 0
    }
    while ($true) {
        try {
            foreach ($it in (Invoke-Poll)) {
                Out-Event @{ event = "popup"; id = $it.id; severity = $it.severity; title = $it.title; summary = $it.summary;
                    rows = $it.data.Count; total = $it.total; sound = $it.sound }
            }
            $p = Get-Pending
            Out-Event @{ event = "pending"; count = $p.Count; ids = @($p | ForEach-Object { $_.id }) }
        } catch { Out-Event @{ event = "error"; error = "$_" } }
        if ($Once) { break }
        Start-Sleep -Seconds $PollSeconds
    }
    exit 0
}

# -- the horn: Gjallarhorn's icon, drawn here so there is one source for every size ------------

Add-Type -AssemblyName System.Drawing

function New-GjBitmap([int]$size, [bool]$ok = $true) {
    function C([string]$hex) { [System.Drawing.ColorTranslator]::FromHtml($hex) }
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.ScaleTransform($size / 32.0, $size / 32.0)
    $sq = New-Object System.Drawing.Drawing2D.GraphicsPath
    $r = 7
    $sq.AddArc(0, 0, $r * 2, $r * 2, 180, 90); $sq.AddArc(32 - $r * 2, 0, $r * 2, $r * 2, 270, 90)
    $sq.AddArc(32 - $r * 2, 32 - $r * 2, $r * 2, $r * 2, 0, 90); $sq.AddArc(0, 32 - $r * 2, $r * 2, $r * 2, 90, 90)
    $sq.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush (C $(if ($ok) { "#7765EA" } else { "#6B6E80" }))), $sq)
    # A curved war horn: the tip up at the left, the body sweeping down, the bell opening up at the right.
    $horn = New-Object System.Drawing.Drawing2D.GraphicsPath
    $horn.AddBezier(5.0, 5.5, 4.0, 19.0, 15.0, 28.5, 28.0, 15.5)
    $horn.AddLine(28.0, 15.5, 21.5, 9.0)
    $horn.AddBezier(21.5, 9.0, 13.5, 17.5, 7.5, 15.5, 6.6, 5.2)
    $horn.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush (C "#F4EFE3")), $horn)
    $g.SetClip($horn)
    $band = New-Object System.Drawing.Pen((C "#E8C23D"), 1.6)
    $g.DrawLine($band, 9.5, 12.0, 6.0, 15.5)
    $g.DrawLine($band, 15.5, 16.5, 13.0, 21.5)
    $g.ResetClip()
    $st = $g.Save()
    $g.TranslateTransform(24.75, 12.25)
    $g.RotateTransform(45)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush (C "#E8C23D")), -5.2, -2.1, 10.4, 4.2)
    $g.FillEllipse((New-Object System.Drawing.SolidBrush (C "#3B2F9E")), -3.9, -1.25, 7.8, 2.5)
    $g.Restore($st)
    $g.Dispose()
    return , $bmp
}

# An .ico with PNG images inside, as Windows Vista and later read them.
function Save-GjIcon([string]$path) {
    $sizes = @(16, 24, 32, 48, 64, 256)
    $pngs = @()
    foreach ($s in $sizes) {
        $ms = New-Object IO.MemoryStream
        (New-GjBitmap $s).Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $pngs += , $ms.ToArray()
    }
    $out = New-Object IO.MemoryStream
    $w = New-Object IO.BinaryWriter($out)
    $w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $s = $sizes[$i] % 256
        $w.Write([byte]$s); $w.Write([byte]$s); $w.Write([byte]0); $w.Write([byte]0)
        $w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$pngs[$i].Length); $w.Write([uint32]$offset)
        $offset += $pngs[$i].Length
    }
    foreach ($png in $pngs) { $w.Write([byte[]]$png) }
    $w.Flush()
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $path))
    [IO.File]::WriteAllBytes($path, $out.ToArray())
}

if ($WriteIcon) { Save-GjIcon $WriteIcon; exit 0 }

# -- one Gjallarhorn per user: a second start asks the first to open its list ------------------

$created = $false
$Mutex = New-Object System.Threading.Mutex($true, "Local\Heimdall.Gjallarhorn", [ref]$created)
$OpenSignal = New-Object System.Threading.EventWaitHandle($false, [Threading.EventResetMode]::AutoReset, "Local\Heimdall.Gjallarhorn.Open")
$TestSignal = New-Object System.Threading.EventWaitHandle($false, [Threading.EventResetMode]::AutoReset, "Local\Heimdall.Gjallarhorn.Test")
if (-not $created) {
    if ($Test) { [void]$TestSignal.Set() } else { [void]$OpenSignal.Set() }
    exit 0
}

Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Windows.Forms, System.Drawing

# Windows groups taskbar buttons by app: without an identity of its own, Gjallarhorn would sit under
# PowerShell's icon. The call is made through a dynamic P/Invoke, which needs no compiler.
$AppId = "Heimdall.Gjallarhorn"
function Set-AppIdentity {
    try {
        $png = Join-Path $StateDir "gjallarhorn.png"
        (New-GjBitmap 64).Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
        $key = "HKCU:\Software\Classes\AppUserModelId\$AppId"
        New-Item -Path $key -Force | Out-Null
        Set-ItemProperty -Path $key -Name DisplayName -Value "Gjallarhorn"
        Set-ItemProperty -Path $key -Name IconUri -Value $png
        $asm = [AppDomain]::CurrentDomain.DefineDynamicAssembly((New-Object Reflection.AssemblyName "GjallarhornShell"),
            [Reflection.Emit.AssemblyBuilderAccess]::Run)
        $type = $asm.DefineDynamicModule("GjallarhornShell").DefineType("GjallarhornShell.Native", "Public, Class")
        $m = $type.DefinePInvokeMethod("SetCurrentProcessExplicitAppUserModelID", "shell32.dll",
            [Reflection.MethodAttributes]"Public, Static, PinvokeImpl", [Reflection.CallingConventions]::Standard,
            [int], [Type[]]@([string]), [Runtime.InteropServices.CallingConvention]::Winapi, [Runtime.InteropServices.CharSet]::Unicode)
        $m.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
        [void]($type.CreateType())::SetCurrentProcessExplicitAppUserModelID($AppId)
    } catch { Write-Log "app identity not set (the taskbar may show PowerShell's icon): $_" }
}
Set-AppIdentity

function Get-WpfImage([int]$size) {
    $ms = New-Object IO.MemoryStream
    (New-GjBitmap $size).Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $ms.Position = 0
    $img = New-Object System.Windows.Media.Imaging.BitmapImage
    $img.BeginInit(); $img.StreamSource = $ms; $img.CacheOption = "OnLoad"; $img.EndInit(); $img.Freeze()
    return $img
}

# Bifrost's palette: indigo-tinted darks, one violet accent, status colours that mean the same everywhere.
$Sev = @{
    info     = @{ color = "#5AA9E6"; label = "Info"; secs = 8 }
    warning  = @{ color = "#E8A33D"; label = "Warning"; secs = 25 }
    critical = @{ color = "#F06470"; label = "Critical"; secs = 0 }      # stays until acted on
}
$Dot = [char]0x00B7
$Converter = New-Object System.Windows.Media.BrushConverter
function Brush([string]$hex) { $Converter.ConvertFromString($hex) }
function Tint([string]$hex) { Brush ("#29" + $hex.TrimStart("#")) }        # the colour at 16%, for pills

$Styles = @'
<Window.Resources>
  <Style TargetType="Button">
    <Setter Property="Foreground" Value="#E4E5EE"/>
    <Setter Property="Background" Value="#1A1B25"/>
    <Setter Property="BorderBrush" Value="#2C2E3C"/>
    <Setter Property="Padding" Value="14,6"/>
    <Setter Property="Margin" Value="0,0,8,0"/>
    <Setter Property="FontSize" Value="13"/>
    <Setter Property="Cursor" Value="Hand"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="b" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                  BorderThickness="1" CornerRadius="6" Padding="{TemplateBinding Padding}">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="BorderBrush" Value="#7765EA"/></Trigger>
            <Trigger Property="IsPressed" Value="True"><Setter TargetName="b" Property="Opacity" Value="0.85"/></Trigger>
            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="b" Property="Opacity" Value="0.4"/></Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style x:Key="Primary" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
    <Setter Property="Background" Value="#7765EA"/>
    <Setter Property="BorderBrush" Value="#7765EA"/>
    <Setter Property="Foreground" Value="#FFFFFF"/>
  </Style>
  <Style x:Key="Ghost" TargetType="Button">
    <Setter Property="Foreground" Value="#8B8EA0"/>
    <Setter Property="Padding" Value="8,4"/>
    <Setter Property="FontSize" Value="13"/>
    <Setter Property="Cursor" Value="Hand"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="b" Background="Transparent" CornerRadius="6" Padding="{TemplateBinding Padding}">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="b" Property="Background" Value="#22232F"/><Setter Property="Foreground" Value="#E4E5EE"/>
            </Trigger>
            <Trigger Property="IsEnabled" Value="False"><Setter TargetName="b" Property="Opacity" Value="0.35"/></Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style x:Key="Chrome" TargetType="Button">
    <Setter Property="Foreground" Value="#B4B6C6"/>
    <Setter Property="Width" Value="46"/>
    <Setter Property="Height" Value="38"/>
    <Setter Property="FontFamily" Value="Segoe Fluent Icons, Segoe MDL2 Assets"/>
    <Setter Property="FontSize" Value="10"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="b" Background="Transparent">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Background" Value="#22232F"/></Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style x:Key="ChromeClose" TargetType="Button" BasedOn="{StaticResource Chrome}">
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="Button">
          <Border x:Name="b" Background="Transparent">
            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter TargetName="b" Property="Background" Value="#E5484D"/><Setter Property="Foreground" Value="#FFFFFF"/>
            </Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="ListBoxItem">
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="ListBoxItem">
          <Border x:Name="b" Background="Transparent" Padding="10,8" Margin="0,1" CornerRadius="6">
            <ContentPresenter/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Background" Value="#171822"/></Trigger>
            <Trigger Property="IsSelected" Value="True"><Setter TargetName="b" Property="Background" Value="#262735"/></Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="TextBox">
    <Setter Property="Foreground" Value="#E4E5EE"/>
    <Setter Property="CaretBrush" Value="#E4E5EE"/>
    <Setter Property="FontSize" Value="13"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="TextBox">
          <Border x:Name="b" Background="#1A1B25" BorderBrush="#2C2E3C" BorderThickness="1" CornerRadius="6" Padding="8,5">
            <ScrollViewer x:Name="PART_ContentHost"/>
          </Border>
          <ControlTemplate.Triggers>
            <Trigger Property="IsKeyboardFocused" Value="True"><Setter TargetName="b" Property="BorderBrush" Value="#7765EA"/></Trigger>
          </ControlTemplate.Triggers>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
  </Style>
  <Style x:Key="Thumb" TargetType="Thumb">
    <Setter Property="Template">
      <Setter.Value><ControlTemplate TargetType="Thumb"><Border Background="#2C2E3C" CornerRadius="4"/></ControlTemplate></Setter.Value>
    </Setter>
  </Style>
  <Style TargetType="ScrollBar">
    <Setter Property="Width" Value="10"/>
    <Setter Property="MinWidth" Value="10"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="ScrollBar">
          <Track x:Name="PART_Track" IsDirectionReversed="True" Margin="2"><Track.Thumb><Thumb Style="{StaticResource Thumb}"/></Track.Thumb></Track>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
    <Style.Triggers>
      <Trigger Property="Orientation" Value="Horizontal">
        <Setter Property="Width" Value="Auto"/><Setter Property="MinWidth" Value="0"/>
        <Setter Property="Height" Value="10"/><Setter Property="MinHeight" Value="10"/>
        <Setter Property="Template">
          <Setter.Value>
            <ControlTemplate TargetType="ScrollBar">
              <Track x:Name="PART_Track" Margin="2"><Track.Thumb><Thumb Style="{StaticResource Thumb}"/></Track.Thumb></Track>
            </ControlTemplate>
          </Setter.Value>
        </Setter>
      </Trigger>
    </Style.Triggers>
  </Style>
  <Style TargetType="DataGridColumnHeader">
    <Setter Property="Background" Value="#1A1B25"/>
    <Setter Property="Foreground" Value="#B4B6C6"/>
    <Setter Property="Padding" Value="10,7"/>
    <Setter Property="BorderBrush" Value="#2C2E3C"/>
    <Setter Property="BorderThickness" Value="0,0,1,1"/>
    <Setter Property="FontWeight" Value="Medium"/>
  </Style>
  <Style TargetType="DataGridCell">
    <Setter Property="Padding" Value="10,5"/>
    <Setter Property="BorderThickness" Value="0"/>
    <Setter Property="FontFamily" Value="IBM Plex Mono, Consolas"/>
    <Setter Property="Template">
      <Setter.Value>
        <ControlTemplate TargetType="DataGridCell">
          <Border Padding="{TemplateBinding Padding}" Background="{TemplateBinding Background}"><ContentPresenter/></Border>
        </ControlTemplate>
      </Setter.Value>
    </Setter>
    <Style.Triggers>
      <Trigger Property="IsSelected" Value="True">
        <Setter Property="Background" Value="#262735"/><Setter Property="Foreground" Value="#E4E5EE"/>
      </Trigger>
    </Style.Triggers>
  </Style>
</Window.Resources>
'@

function New-Window([string]$xaml) {
    $x = $xaml.Replace("<!--STYLES-->", $Styles)
    [Windows.Markup.XamlReader]::Parse($x)
}

$NS = 'xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"'

# -- one pop-up card at the bottom right, never a stack ------------------------------------------------
# New alerts join one card. It shows the most serious first, with "1 of 4" and arrows to step
# through the others; acknowledging moves on to the next. The cross hides it: everything stays in
# the list, and the count stays on the tray icon.

$CardXaml = @"
<Window $NS WindowStyle="None" AllowsTransparency="True" Background="Transparent" Topmost="True"
        ShowInTaskbar="False" ShowActivated="False" ResizeMode="NoResize" SizeToContent="Height" Width="416"
        FontFamily="IBM Plex Sans, Segoe UI" Foreground="#E4E5EE">
  <!--STYLES-->
  <Border Margin="10" CornerRadius="10" Background="#1A1B25" BorderBrush="#2C2E3C" BorderThickness="1">
    <Border.Effect><DropShadowEffect BlurRadius="22" ShadowDepth="4" Opacity="0.6" Color="#000000"/></Border.Effect>
    <Grid>
      <Grid.ColumnDefinitions><ColumnDefinition Width="4"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
      <Border x:Name="Bar" Grid.Column="0" CornerRadius="10,0,0,10"/>
      <StackPanel Grid.Column="1" Margin="16,12,10,14">
        <DockPanel LastChildFill="True">
          <Button x:Name="Close" DockPanel.Dock="Right" Style="{StaticResource Ghost}" Padding="7,3" Content="&#x2715;"
                  ToolTip="Hide (the alerts stay in the list)"/>
          <TextBlock x:Name="When" DockPanel.Dock="Right" Foreground="#8B8EA0" FontSize="12" Margin="0,0,8,0" VerticalAlignment="Center"/>
          <StackPanel Orientation="Horizontal" VerticalAlignment="Center">
            <Border x:Name="Pill" CornerRadius="4" Padding="7,1"><TextBlock x:Name="Head" FontWeight="SemiBold" FontSize="12"/></Border>
            <TextBlock x:Name="Group" Foreground="#8B8EA0" FontSize="12.5" Margin="8,0,0,0" VerticalAlignment="Center"
                       TextTrimming="CharacterEllipsis"/>
          </StackPanel>
        </DockPanel>
        <TextBlock x:Name="Title" FontSize="15.5" FontWeight="SemiBold" Margin="0,9,0,3" TextTrimming="CharacterEllipsis"/>
        <TextBlock x:Name="Body" Foreground="#A9ABBC" FontSize="13" TextWrapping="Wrap" MaxHeight="38"
                   TextTrimming="CharacterEllipsis"/>
        <DockPanel Margin="0,12,0,0" LastChildFill="False">
          <Button x:Name="Details" DockPanel.Dock="Left" Content="Details"/>
          <Button x:Name="Ack" DockPanel.Dock="Left" Style="{StaticResource Primary}" Content="Acknowledge"/>
          <StackPanel x:Name="Pager" DockPanel.Dock="Right" Orientation="Horizontal" VerticalAlignment="Center">
            <Button x:Name="PrevCard" Style="{StaticResource Ghost}" Content="&#x2039;" FontSize="17" Padding="8,0" ToolTip="Previous"/>
            <TextBlock x:Name="Position" Foreground="#B4B6C6" FontSize="12.5" VerticalAlignment="Center" Margin="2,0"/>
            <Button x:Name="NextCard" Style="{StaticResource Ghost}" Content="&#x203A;" FontSize="17" Padding="8,0" ToolTip="Next"/>
          </StackPanel>
        </DockPanel>
      </StackPanel>
    </Grid>
  </Border>
</Window>
"@

$ConfirmXaml = @"
<Window $NS WindowStyle="None" AllowsTransparency="True" Background="Transparent" ResizeMode="NoResize"
        SizeToContent="WidthAndHeight" WindowStartupLocation="CenterScreen" Topmost="True" ShowInTaskbar="False"
        FontFamily="IBM Plex Sans, Segoe UI" Foreground="#E4E5EE">
  <!--STYLES-->
  <Border Margin="14" Width="420" CornerRadius="10" Background="#1A1B25" BorderBrush="#2C2E3C" BorderThickness="1" Padding="22,18,18,16">
    <Border.Effect><DropShadowEffect BlurRadius="24" ShadowDepth="4" Opacity="0.6" Color="#000000"/></Border.Effect>
    <StackPanel>
      <TextBlock x:Name="CTitle" FontSize="16" FontWeight="SemiBold"/>
      <TextBlock x:Name="CText" Foreground="#A9ABBC" FontSize="13" TextWrapping="Wrap" Margin="0,8,0,18"/>
      <StackPanel Orientation="Horizontal" HorizontalAlignment="Right">
        <Button x:Name="CCancel" Content="Cancel"/>
        <Button x:Name="COk" Style="{StaticResource Primary}" Margin="0"/>
      </StackPanel>
    </StackPanel>
  </Border>
</Window>
"@

# A dark question in Heimdall's style instead of a grey Windows message box.
function Show-Confirm([string]$title, [string]$text, [string]$ok = "OK", [switch]$Danger, [switch]$Info) {
    $w = New-Window $ConfirmXaml
    $w.FindName("CTitle").Text = $title
    $w.FindName("CText").Text = $text
    $okb = $w.FindName("COk")
    $okb.Content = $ok
    if ($Danger) { $okb.Background = Brush "#E5484D"; $okb.BorderBrush = Brush "#E5484D" }
    if ($Info) { $w.FindName("CCancel").Visibility = "Collapsed" }
    $okb.Add_Click({ param($b, $e) [System.Windows.Window]::GetWindow($b).DialogResult = $true })
    $w.FindName("CCancel").Add_Click({ param($b, $e) [System.Windows.Window]::GetWindow($b).DialogResult = $false })
    $w.Add_KeyDown({ param($x, $e) if ($e.Key -eq "Escape") { $x.DialogResult = $false } })
    if ($Inbox -and $Inbox.IsVisible -and $Inbox.WindowState -ne "Minimized") {
        $w.Owner = $Inbox
        $w.WindowStartupLocation = "CenterOwner"
    }
    return [bool]$w.ShowDialog()
}

$Card = $null                                           # the one card window
$Deck = New-Object System.Collections.ArrayList         # the alerts the card steps through
$Pos = 0
$CardTimer = $null
$MutedUntil = [datetime]::MinValue

function Get-Rank($it) { @{ critical = 0; warning = 1; info = 2 }[$it.severity] }

function Set-CardPosition {
    if (-not $Card) { return }
    $wa = [System.Windows.SystemParameters]::WorkArea
    $h = if ($Card.ActualHeight -gt 0) { $Card.ActualHeight } else { 170 }
    $Card.Left = $wa.Right - $Card.Width - 2
    $Card.Top = $wa.Bottom - $h - 2
}

function New-CardWindow {
    $w = New-Window $CardXaml
    $w.FindName("Close").Add_Click({ Hide-Card })
    $w.FindName("Details").Add_Click({
            $it = $Deck[$Pos]
            Hide-Card
            Show-Inbox $(if ($it) { $it.id } else { "" })
        })
    $w.FindName("Ack").Add_Click({
            $it = $Deck[$Pos]
            if (-not $it) { return }
            if ($it.needs_ack) { Invoke-Ack $it } else { Remove-FromDeck $it }
        })
    $w.FindName("PrevCard").Add_Click({ if ($Pos -gt 0) { $script:Pos--; Update-Card } })
    $w.FindName("NextCard").Add_Click({ if ($Pos -lt $Deck.Count - 1) { $script:Pos++; Update-Card } })
    $w.Add_SizeChanged({ Set-CardPosition })
    $w.Add_MouseEnter({ if ($CardTimer) { $CardTimer.Stop() } })        # reading it: keep it
    $w.Add_MouseLeave({ Start-CardTimer })
    $t = New-Object System.Windows.Threading.DispatcherTimer
    $t.Add_Tick({ Hide-Card })
    $script:CardTimer = $t
    $script:Card = $w
}

# The card fades on its own only when nothing in it is critical: after the longest wait among them.
function Start-CardTimer {
    if (-not $CardTimer) { return }
    $CardTimer.Stop()
    if ($Deck.Count -eq 0) { return }
    $secs = 0
    foreach ($it in $Deck) {
        $s = if ($it.needs_ack) { $Sev[$it.severity].secs } else { 8 }
        if ($s -eq 0) { return }                                # something critical: it stays
        $secs = [Math]::Max($secs, $s)
    }
    $CardTimer.Interval = [TimeSpan]::FromSeconds($secs)
    $CardTimer.Start()
}

function Update-Card {
    if ($Deck.Count -eq 0) { Hide-Card; return }
    if (-not $Card) { New-CardWindow }
    $script:Pos = [Math]::Min([Math]::Max($Pos, 0), $Deck.Count - 1)
    $it = $Deck[$Pos]
    $s = $Sev[$it.severity]
    $Card.FindName("Bar").Background = Brush $s.color
    $Card.FindName("Pill").Background = Tint $s.color
    $head = $Card.FindName("Head")
    $head.Foreground = Brush $s.color
    $head.Text = if ($it.kind -eq "recovery") { "Cleared" } elseif ($it.kind -eq "test") { "$($s.label) $Dot test" } else { $s.label }
    $Card.FindName("Group").Text = $it.group
    $Card.FindName("When").Text = $it.fired.ToString("HH:mm")
    $Card.FindName("Title").Text = $it.title
    $Card.FindName("Body").Text = (@($it.summary) + @($it.lines | Select-Object -First 1) | Where-Object { $_ }) -join " $Dot "
    $Card.FindName("Ack").Content = if ($it.needs_ack) { "Acknowledge" } else { "OK" }
    $many = $Deck.Count -gt 1
    $Card.FindName("Pager").Visibility = $(if ($many) { "Visible" } else { "Collapsed" })
    $Card.FindName("Position").Text = "$($Pos + 1) of $($Deck.Count)"
    $Card.FindName("PrevCard").IsEnabled = $Pos -gt 0
    $Card.FindName("NextCard").IsEnabled = $Pos -lt $Deck.Count - 1
    if (-not $Card.IsVisible) {
        $Card.Opacity = 0
        $Card.Show()
        $anim = New-Object System.Windows.Media.Animation.DoubleAnimation(0, 1, (New-Object System.Windows.Duration ([TimeSpan]::FromMilliseconds(220))))
        $Card.BeginAnimation([System.Windows.Window]::OpacityProperty, $anim)
    }
    Set-CardPosition
    if (-not $Card.IsMouseOver) { Start-CardTimer }
}

function Hide-Card {
    $Deck.Clear()
    $script:Pos = 0
    if ($CardTimer) { $CardTimer.Stop() }
    if ($Card) { $Card.Hide() }
}

function Remove-FromDeck($it) {
    $i = $Deck.IndexOf($it)
    if ($i -lt 0) { return }
    $Deck.RemoveAt($i)
    if ($i -lt $Pos) { $script:Pos-- }
    Update-Card
}

$SoundCache = Join-Path $StateDir "sounds"
$Player = $null

# The file to play: copied to this PC first, so a slow share never delays the sound.
function Get-SoundFile([string]$name) {
    if ($name -match '^horn:(info|warning|critical)$') { $src = Join-Path $PSScriptRoot "sounds\horn-$($Matches[1]).wav" }
    elseif ($name -match '^file:([A-Za-z0-9._ -]+\.wav)$') { $src = Join-Path $Root "sounds\$($Matches[1])" }
    else { return $null }
    $dst = Join-Path $SoundCache ($(if ($name -like "file:*") { "custom-" } else { "" }) + [IO.Path]::GetFileName($src))
    try {
        if (-not [IO.File]::Exists($dst) -or [IO.File]::GetLastWriteTimeUtc($src) -gt [IO.File]::GetLastWriteTimeUtc($dst)) {
            [void][IO.Directory]::CreateDirectory($SoundCache)
            [IO.File]::Copy($src, $dst, $true)
        }
        return $dst
    } catch {
        if ([IO.File]::Exists($dst)) { return $dst }
        return $src
    }
}

function Invoke-Sound([string]$name) {
    if ($NoSound -or -not $State.sound -or -not $name -or $name -eq "none") { return }
    try {
        if ($name -match '^system:(asterisk|beep|exclamation|hand|question)$') {
            switch ($Matches[1]) {
                "asterisk" { [System.Media.SystemSounds]::Asterisk.Play() }
                "beep" { [System.Media.SystemSounds]::Beep.Play() }
                "exclamation" { [System.Media.SystemSounds]::Exclamation.Play() }
                "hand" { [System.Media.SystemSounds]::Hand.Play() }
                "question" { [System.Media.SystemSounds]::Question.Play() }
            }
            return
        }
        $f = Get-SoundFile $name
        if (-not $f) { return }
        $script:Player = New-Object System.Media.SoundPlayer $f       # kept, or it could stop mid-call
        $Player.Play()
    } catch {
        Write-Log "sound ${name}: $_"
        [System.Media.SystemSounds]::Exclamation.Play()
    }
}

function Add-PopUps($items) {
    if (-not $items -or @($items).Count -eq 0) { return }
    if ((Get-Date) -lt $MutedUntil) { return }
    $worst = "info"
    $top = $null
    foreach ($it in $items) {
        if ($Deck.Contains($it)) { continue }
        [void]$Deck.Add($it)
        if ($it.severity -eq "critical" -or ($it.severity -eq "warning" -and $worst -eq "info")) { $worst = $it.severity }
        if (-not $top -or (Get-Rank $it) -lt (Get-Rank $top)) { $top = $it }
    }
    if (-not $top) { return }
    # The most serious first, newest first among equals; the card opens on the most serious newcomer.
    $ordered = @($Deck | Sort-Object @{ Expression = { Get-Rank $_ } }, @{ Expression = { $_.fired }; Descending = $true })
    $Deck.Clear()
    foreach ($it in $ordered) { [void]$Deck.Add($it) }
    $script:Pos = $Deck.IndexOf($top)
    Invoke-Sound $top.sound                                # the most serious newcomer's call
    Update-Card
}


# -- the list of alerts, to go through them ----------------------------------------------------------

$InboxXaml = @"
<Window $NS xmlns:shell="clr-namespace:System.Windows.Shell;assembly=PresentationFramework"
        Title="Gjallarhorn" Width="1200" Height="720" MinWidth="860" MinHeight="480"
        WindowStyle="SingleBorderWindow" ResizeMode="CanResize" WindowStartupLocation="CenterScreen"
        Background="#111219" BorderBrush="#2C2E3C" BorderThickness="1" FontFamily="IBM Plex Sans, Segoe UI" Foreground="#E4E5EE">
  <shell:WindowChrome.WindowChrome>
    <shell:WindowChrome CaptionHeight="38" ResizeBorderThickness="6" GlassFrameThickness="0" CornerRadius="0"
                        UseAeroCaptionButtons="False"/>
  </shell:WindowChrome.WindowChrome>
  <!--STYLES-->
  <Grid x:Name="Root">
    <Grid.ColumnDefinitions><ColumnDefinition Width="340"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>

    <!-- the sidebar, as in Bifrost -->
    <Border Grid.Column="0" Background="#0D0E14" BorderBrush="#24262F" BorderThickness="0,0,1,0">
      <DockPanel>
        <StackPanel DockPanel.Dock="Top" Margin="22,18,20,0">
          <StackPanel Orientation="Horizontal">
            <Image x:Name="Logo" Width="30" Height="30" Margin="0,0,10,0" VerticalAlignment="Center"/>
            <TextBlock Text="Gjallarhorn" FontSize="19" FontWeight="SemiBold" VerticalAlignment="Center"/>
          </StackPanel>
          <TextBlock Text="Heimdall's alerts, sounded on this desk" Foreground="#8B8EA0" FontSize="12.5" Margin="0,3,0,0"/>
          <WrapPanel Margin="0,14,0,0">
            <Border x:Name="CountPill" CornerRadius="4" Padding="7,2" Margin="0,0,6,6">
              <TextBlock x:Name="Count" FontSize="12.5" FontWeight="Medium"/>
            </Border>
            <Border x:Name="StatePill" CornerRadius="4" Padding="7,2" Margin="0,0,6,6" Visibility="Collapsed">
              <TextBlock x:Name="State" FontSize="12.5" FontWeight="Medium"/>
            </Border>
          </WrapPanel>
          <Grid Margin="0,8,0,8">
            <TextBox x:Name="Search" ToolTip="Filter by alert, group or text"/>
            <TextBlock x:Name="SearchHint" Text="Search alerts" Foreground="#6E7183" FontSize="13" Margin="10,6,0,0"
                       IsHitTestVisible="False"/>
          </Grid>
        </StackPanel>
        <Border DockPanel.Dock="Bottom" BorderBrush="#24262F" BorderThickness="0,1,0,0" Padding="14,10">
          <DockPanel LastChildFill="False">
            <Button x:Name="Restart" DockPanel.Dock="Left" Style="{StaticResource Ghost}" Content="&#x21BB;  Restart"
                    ToolTip="Restart Gjallarhorn (also picks up a new version from the share)"/>
            <Button x:Name="Quit" DockPanel.Dock="Right" Style="{StaticResource Ghost}" Content="Quit"
                    ToolTip="Stop Gjallarhorn: no more alerts until it is started again"/>
          </DockPanel>
        </Border>
        <ListBox x:Name="List" Background="Transparent" BorderThickness="0" Foreground="#E4E5EE" Margin="12,0,10,6"
                 ScrollViewer.HorizontalScrollBarVisibility="Disabled"/>
      </DockPanel>
    </Border>

    <!-- the alert -->
    <Grid Grid.Column="1">
      <Grid.RowDefinitions><RowDefinition Height="38"/><RowDefinition Height="*"/></Grid.RowDefinitions>
      <StackPanel Grid.Row="0" Orientation="Horizontal" HorizontalAlignment="Right" VerticalAlignment="Top">
        <Button x:Name="WinMin" Style="{StaticResource Chrome}" Content="&#xE921;" ToolTip="Minimize"
                shell:WindowChrome.IsHitTestVisibleInChrome="True"/>
        <Button x:Name="WinMax" Style="{StaticResource Chrome}" Content="&#xE922;" ToolTip="Maximize"
                shell:WindowChrome.IsHitTestVisibleInChrome="True"/>
        <Button x:Name="WinClose" Style="{StaticResource ChromeClose}" Content="&#xE8BB;" ToolTip="Close (Gjallarhorn keeps running)"
                shell:WindowChrome.IsHitTestVisibleInChrome="True"/>
      </StackPanel>
      <Grid Grid.Row="1" Margin="40,2,40,26">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/>
          <RowDefinition Height="Auto"/><RowDefinition Height="*"/><RowDefinition Height="Auto"/>
        </Grid.RowDefinitions>
        <DockPanel Grid.Row="0" LastChildFill="False">
          <StackPanel DockPanel.Dock="Right" Orientation="Horizontal">
            <Button x:Name="AckAll" Content="Acknowledge all"/>
            <Button x:Name="Mute" Margin="0" Content="Pause pop-ups for 1 hour"/>
          </StackPanel>
          <StackPanel x:Name="DMeta" DockPanel.Dock="Left" Orientation="Horizontal" VerticalAlignment="Center">
            <Border x:Name="DPill" CornerRadius="4" Padding="7,2"><TextBlock x:Name="DPillText" FontWeight="SemiBold" FontSize="12.5"/></Border>
            <TextBlock x:Name="DHead" Foreground="#8B8EA0" FontSize="13" Margin="10,0,0,0" VerticalAlignment="Center"/>
          </StackPanel>
        </DockPanel>
        <StackPanel x:Name="DHeader" Grid.Row="1" Margin="0,14,0,0">
          <TextBlock x:Name="DTitle" FontSize="24" FontWeight="SemiBold" TextWrapping="Wrap"/>
          <TextBlock x:Name="DState" Foreground="#8B8EA0" FontSize="13" Margin="0,6,0,0"/>
        </StackPanel>
        <UniformGrid x:Name="DMetrics" Grid.Row="2" Rows="1" Margin="0,20,0,0">
          <StackPanel><TextBlock x:Name="M1L" Foreground="#B4B6C6" FontSize="13"/><TextBlock x:Name="M1V" FontSize="28" Margin="0,2,0,0"/></StackPanel>
          <StackPanel><TextBlock x:Name="M2L" Foreground="#B4B6C6" FontSize="13"/><TextBlock x:Name="M2V" FontSize="28" Margin="0,2,0,0"/></StackPanel>
          <StackPanel><TextBlock x:Name="M3L" Foreground="#B4B6C6" FontSize="13"/><TextBlock x:Name="M3V" FontSize="28" Margin="0,2,0,0" TextTrimming="CharacterEllipsis"/></StackPanel>
        </UniformGrid>
        <TextBlock x:Name="DLines" Grid.Row="3" Foreground="#A9ABBC" FontSize="13.5" TextWrapping="Wrap" Margin="0,18,0,14"/>
        <Border x:Name="DRowsBox" Grid.Row="4" BorderBrush="#2C2E3C" BorderThickness="1" CornerRadius="6" ClipToBounds="True">
          <DataGrid x:Name="DRows" AutoGenerateColumns="True" IsReadOnly="True" HeadersVisibility="Column"
                    Background="#111219" RowBackground="#111219" AlternatingRowBackground="#14151E" Foreground="#E4E5EE"
                    BorderThickness="0" GridLinesVisibility="Horizontal" HorizontalGridLinesBrush="#23252F"
                    CanUserAddRows="False" SelectionUnit="CellOrRowHeader" ClipboardCopyMode="IncludeHeader" FontSize="13"/>
        </Border>
        <DockPanel Grid.Row="5" Margin="0,16,0,0" LastChildFill="False">
          <Button x:Name="Ack" DockPanel.Dock="Left" Style="{StaticResource Primary}" Content="Acknowledge"/>
          <Button x:Name="Copy" DockPanel.Dock="Left" Content="Copy rows" ToolTip="Paste into Excel"/>
          <TextBlock x:Name="DCount" DockPanel.Dock="Left" Foreground="#8B8EA0" VerticalAlignment="Center" Margin="6,0,0,0"/>
          <Button x:Name="Next" DockPanel.Dock="Right" Margin="0" Content="Next  &#x203A;"/>
          <Button x:Name="Prev" DockPanel.Dock="Right" Content="&#x2039;  Previous"/>
        </DockPanel>
        <StackPanel x:Name="Empty" Grid.RowSpan="6" HorizontalAlignment="Center" VerticalAlignment="Center" Visibility="Collapsed">
          <TextBlock Text="All quiet." FontSize="22" FontWeight="SemiBold" HorizontalAlignment="Center"/>
          <TextBlock Text="Nothing waits to be acknowledged." Foreground="#8B8EA0" FontSize="13.5" Margin="0,6,0,0"
                     HorizontalAlignment="Center"/>
        </StackPanel>
      </Grid>
    </Grid>
  </Grid>
</Window>
"@

$Inbox = $null
$Selected = ""
$Quitting = $false
$Filling = $false

function Get-InboxControl([string]$n) { $Inbox.FindName($n) }

function New-ListRow($it) {
    $s = $Sev[$it.severity]
    $g = New-Object System.Windows.Controls.Grid
    foreach ($w in "Auto", "*", "Auto") {
        $cd = New-Object System.Windows.Controls.ColumnDefinition
        $cd.Width = (New-Object System.Windows.GridLengthConverter).ConvertFromString($w)
        $g.ColumnDefinitions.Add($cd)
    }
    $marker = New-Object System.Windows.Shapes.Ellipse      # not $dot: PowerShell names ignore case, and $Dot is the separator
    $marker.Width = 8; $marker.Height = 8; $marker.Margin = New-Object System.Windows.Thickness(0, 6, 10, 0)
    $marker.VerticalAlignment = [System.Windows.VerticalAlignment]::Top
    $marker.Fill = Brush $(if ($it.kind -eq "recovery") { "#3ECF8E" } else { $s.color })
    [System.Windows.Controls.Grid]::SetColumn($marker, 0)
    $sp = New-Object System.Windows.Controls.StackPanel
    $t1 = New-Object System.Windows.Controls.TextBlock
    $t1.Text = $it.title; $t1.FontWeight = [System.Windows.FontWeights]::Medium; $t1.FontSize = 13.5
    $t1.TextTrimming = [System.Windows.TextTrimming]::CharacterEllipsis; $t1.Foreground = Brush "#E4E5EE"
    $t2 = New-Object System.Windows.Controls.TextBlock
    $bits = @($it.group, $it.summary) | Where-Object { $_ }
    if ($it.ackBy) { $bits += "acknowledged by $($it.ackBy)" }
    elseif ($it.kind -eq "recovery") { $bits += "cleared" }
    $t2.Text = $bits -join " $Dot "; $t2.Foreground = Brush "#8B8EA0"; $t2.FontSize = 12; $t2.Margin = New-Object System.Windows.Thickness(0, 2, 0, 0)
    $t2.TextTrimming = [System.Windows.TextTrimming]::CharacterEllipsis
    [void]$sp.Children.Add($t1); [void]$sp.Children.Add($t2)
    [System.Windows.Controls.Grid]::SetColumn($sp, 1)
    $t3 = New-Object System.Windows.Controls.TextBlock
    $t3.Text = if ($it.fired.Date -eq (Get-Date).Date) { $it.fired.ToString("HH:mm") } else { $it.fired.ToString("ddd HH:mm") }
    $t3.Foreground = Brush "#8B8EA0"; $t3.FontSize = 12; $t3.Margin = New-Object System.Windows.Thickness(8, 1, 0, 0)
    [System.Windows.Controls.Grid]::SetColumn($t3, 2)
    [void]$g.Children.Add($marker); [void]$g.Children.Add($sp); [void]$g.Children.Add($t3)
    $li = New-Object System.Windows.Controls.ListBoxItem
    $li.Content = $g
    $li.Tag = $it.id
    if (-not ($it.needs_ack -and -not $it.ackBy)) { $li.Opacity = 0.6 }
    return $li
}

function New-ListHeader([string]$text) {
    $li = New-Object System.Windows.Controls.ListBoxItem
    $t = New-Object System.Windows.Controls.TextBlock
    $t.Text = $text; $t.FontSize = 13; $t.FontWeight = [System.Windows.FontWeights]::SemiBold; $t.Foreground = Brush "#B4B6C6"
    $t.Margin = New-Object System.Windows.Thickness(0, 8, 0, 0)
    $li.Content = $t
    $li.IsEnabled = $false
    $li.Focusable = $false
    return $li
}

function Test-Match($it, [string]$q) {
    if (-not $q) { return $true }
    $hay = ($it.title, $it.group, $it.summary, ($it.lines -join " ")) -join " "
    return $hay.IndexOf($q, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Update-Inbox {
    if (-not $Inbox) { return }
    $q = (Get-InboxControl "Search").Text.Trim()
    $pending = @(Get-Pending | Where-Object { Test-Match $_ $q })
    $earlier = @(Get-Earlier | Where-Object { Test-Match $_ $q })
    $list = Get-InboxControl "List"
    $script:Filling = $true
    $list.Items.Clear()
    [void]$list.Items.Add((New-ListHeader "To acknowledge"))
    foreach ($it in $pending) { [void]$list.Items.Add((New-ListRow $it)) }
    if ($earlier.Count) {
        [void]$list.Items.Add((New-ListHeader "Earlier"))
        foreach ($it in $earlier) { [void]$list.Items.Add((New-ListRow $it)) }
    }
    $all = @(Get-Pending).Count
    $worst = @(Get-Pending | Sort-Object @{ Expression = { Get-Rank $_ } } | Select-Object -First 1)
    $colour = if ($all) { $Sev[$worst[0].severity].color } else { "#3ECF8E" }
    (Get-InboxControl "CountPill").Background = Tint $colour
    (Get-InboxControl "Count").Foreground = Brush $colour
    (Get-InboxControl "Count").Text = if ($all) { "$all to acknowledge" } else { "All acknowledged" }
    $state = if (-not $ShareOk) { "Shared folder unreachable" } elseif ((Get-Date) -lt $MutedUntil) { "Pop-ups paused until $($MutedUntil.ToString('HH:mm'))" } else { "" }
    (Get-InboxControl "StatePill").Visibility = $(if ($state) { "Visible" } else { "Collapsed" })
    (Get-InboxControl "StatePill").Background = Tint "#E8A33D"
    (Get-InboxControl "State").Foreground = Brush "#E8A33D"
    (Get-InboxControl "State").Text = $state
    (Get-InboxControl "AckAll").IsEnabled = $all -gt 0
    (Get-InboxControl "Mute").Content = if ((Get-Date) -lt $MutedUntil) { "Resume pop-ups" } else { "Pause pop-ups for 1 hour" }
    $pick = $null
    foreach ($li in $list.Items) { if ($li.IsEnabled -and $li.Tag -eq $Selected) { $pick = $li } }
    if (-not $pick) { foreach ($li in $list.Items) { if ($li.IsEnabled) { $pick = $li; break } } }
    $script:Filling = $false
    $list.SelectedItem = $pick
    if ($pick) { $list.ScrollIntoView($pick) }
    Show-Detail $(if ($pick) { $Items[$pick.Tag] } else { $null })
}

function New-Table($it) {
    $dt = New-Object System.Data.DataTable
    $cols = @($it.columns)
    $numeric = @()
    for ($i = 0; $i -lt $cols.Count; $i++) {
        $isNum = $true; $any = $false
        foreach ($r in $it.data) {
            $v = @($r)[$i]
            if ($null -eq $v) { continue }
            $any = $true
            if (-not ($v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal])) { $isNum = $false; break }
        }
        $numeric += ($isNum -and $any)
        $type = if ($isNum -and $any) { [double] } else { [string] }
        [void]$dt.Columns.Add("c$i", $type)        # safe names; the real ones go in the headers
    }
    foreach ($r in $it.data) {
        $row = $dt.NewRow()
        $vals = @($r)
        for ($i = 0; $i -lt $cols.Count; $i++) {
            $v = $vals[$i]
            if ($null -eq $v) { $row[$i] = [DBNull]::Value }
            elseif ($numeric[$i]) { $row[$i] = [double]$v }
            else { $row[$i] = [string]$v }
        }
        $dt.Rows.Add($row)
    }
    return , $dt          # the comma: a DataTable returned bare would be unrolled into its rows
}

function Show-Detail($it) {
    $has = [bool]$it
    foreach ($n in "DMeta", "DHeader", "DMetrics", "DLines", "DRowsBox") {
        (Get-InboxControl $n).Visibility = $(if ($has) { "Visible" } else { "Collapsed" })
    }
    (Get-InboxControl "Empty").Visibility = $(if ($has) { "Collapsed" } else { "Visible" })
    $script:Selected = if ($has) { $it.id } else { "" }
    $ids = @(Get-NavIds)
    $pos = [array]::IndexOf($ids, $Selected)
    foreach ($n in "Ack", "Copy", "Prev", "Next") { (Get-InboxControl $n).Visibility = $(if ($has) { "Visible" } else { "Collapsed" }) }
    (Get-InboxControl "Prev").IsEnabled = $has -and $pos -gt 0
    (Get-InboxControl "Next").IsEnabled = $has -and $pos -ge 0 -and $pos -lt $ids.Count - 1
    (Get-InboxControl "Ack").IsEnabled = $has -and $it.needs_ack -and -not $it.ackBy
    (Get-InboxControl "Copy").IsEnabled = $has -and $it.data.Count -gt 0
    if (-not $has) { (Get-InboxControl "DCount").Text = ""; return }
    $s = $Sev[$it.severity]
    $colour = if ($it.kind -eq "recovery") { "#3ECF8E" } else { $s.color }
    (Get-InboxControl "DPill").Background = Tint $colour
    $pill = Get-InboxControl "DPillText"
    $pill.Foreground = Brush $colour
    $pill.Text = if ($it.kind -eq "recovery") { "Cleared" } elseif ($it.kind -eq "test") { "$($s.label) $Dot test" } else { $s.label }
    (Get-InboxControl "DHead").Text = (@($it.group, $it.fired.ToString("ddd dd MMM HH:mm:ss")) | Where-Object { $_ }) -join "  $Dot  "
    (Get-InboxControl "DTitle").Text = $it.title
    (Get-InboxControl "DState").Text = if ($it.ackBy) {
        "Acknowledged by $($it.ackBy) at $($it.ackAt.ToString('HH:mm'))"
    } elseif ($it.needs_ack) { "Waiting for acknowledgement" } else { "Nothing to acknowledge" }
    # Three numbers, as Bifrost's overview shows them.
    $shown = $it.data.Count
    (Get-InboxControl "M1L").Text = "Rows"
    (Get-InboxControl "M1V").Text = if ($it.total -gt $shown) { "$shown of $($it.total)" } else { "$($it.total)" }
    (Get-InboxControl "M2L").Text = "Fired at"
    (Get-InboxControl "M2V").Text = $it.fired.ToString("HH:mm:ss")
    if ($it.ackBy) {
        (Get-InboxControl "M3L").Text = "Acknowledged by"
        (Get-InboxControl "M3V").Text = $it.ackBy
    } else {
        $mins = [int][Math]::Floor(((Get-Date) - $it.fired).TotalMinutes)
        (Get-InboxControl "M3L").Text = if ($it.needs_ack) { "Waiting for" } else { "Ago" }
        (Get-InboxControl "M3V").Text = if ($mins -lt 1) { "just now" } elseif ($mins -lt 120) { "$mins min" } else { "$([int]($mins / 60)) h" }
    }
    (Get-InboxControl "DLines").Text = ($it.lines | Where-Object { $_ }) -join "`n"
    $grid = Get-InboxControl "DRows"
    $grid.Tag = @($it.columns)
    $grid.ItemsSource = $null
    if ($shown) {
        (Get-InboxControl "DRowsBox").Visibility = "Visible"
        $grid.ItemsSource = (New-Table $it).DefaultView
        (Get-InboxControl "DCount").Text = if ($it.total -gt $shown) { "the first $shown of $($it.total) rows" } else { "" }
    } else {
        (Get-InboxControl "DRowsBox").Visibility = "Collapsed"
        (Get-InboxControl "DCount").Text = ""
    }
}

function Get-NavIds {
    $list = Get-InboxControl "List"
    @($list.Items | Where-Object { $_.IsEnabled } | ForEach-Object { [string]$_.Tag })
}

function Select-Id([string]$id) {
    $script:Selected = $id
    Update-Inbox
}

function Move-Selection([int]$step) {
    $ids = @(Get-NavIds)
    $pos = [array]::IndexOf($ids, $Selected)
    $to = $pos + $step
    if ($to -ge 0 -and $to -lt $ids.Count) { Select-Id $ids[$to] }
}

function Invoke-Ack($it) {
    try {
        Send-Ack $it
    } catch {
        Write-Log "acknowledging $($it.id) failed: $_"
        [void](Show-Confirm "Not acknowledged" "Could not write the acknowledgement to the shared folder: $_" -Info)
        return
    }
    Remove-FromDeck $it
    Update-Tray
}

function New-Inbox {
    $w = New-Window $InboxXaml
    $script:Inbox = $w
    (Get-InboxControl "WinMin").Add_Click({ $Inbox.WindowState = "Minimized" })
    (Get-InboxControl "WinMax").Add_Click({
            $Inbox.WindowState = $(if ($Inbox.WindowState -eq "Maximized") { "Normal" } else { "Maximized" })
        })
    (Get-InboxControl "WinClose").Add_Click({ $Inbox.Close() })
    $w.Add_StateChanged({
            $max = $Inbox.WindowState -eq "Maximized"
            # A maximized borderless window reaches past the screen edge by the resize border.
            (Get-InboxControl "Root").Margin = $(if ($max) { New-Object System.Windows.Thickness(7) } else { New-Object System.Windows.Thickness(0) })
            (Get-InboxControl "WinMax").Content = [string][char]$(if ($max) { 0xE923 } else { 0xE922 })
            (Get-InboxControl "WinMax").ToolTip = $(if ($max) { "Restore" } else { "Maximize" })
        })
    $list = Get-InboxControl "List"
    $list.Add_SelectionChanged({
            if ($Filling) { return }
            $li = (Get-InboxControl "List").SelectedItem
            if ($li -and $li.IsEnabled) { $script:Selected = [string]$li.Tag; Show-Detail $Items[$li.Tag] }
        })
    (Get-InboxControl "Search").Add_TextChanged({
            (Get-InboxControl "SearchHint").Visibility = $(if ((Get-InboxControl "Search").Text) { "Collapsed" } else { "Visible" })
            Update-Inbox
        })
    $w.Icon = Get-WpfImage 64
    (Get-InboxControl "Logo").Source = Get-WpfImage 96
    (Get-InboxControl "DRows").Add_AutoGeneratingColumn({
            param($g, $e)
            $i = [int]$e.PropertyName.Substring(1)
            # A TextBlock, not the bare name: WPF would read "_" in "limit_up" as a keyboard shortcut.
            $h = New-Object System.Windows.Controls.TextBlock
            $h.Text = [string]@($g.Tag)[$i]
            $e.Column.Header = $h
        })
    (Get-InboxControl "Prev").Add_Click({ Move-Selection -1 })
    (Get-InboxControl "Next").Add_Click({ Move-Selection 1 })
    (Get-InboxControl "Ack").Add_Click({
            $it = $Items[$Selected]
            if (-not $it) { return }
            # Then the next one still to acknowledge, so a busy morning goes quickly.
            $pending = @(Get-Pending | ForEach-Object { $_.id })
            $pos = [array]::IndexOf($pending, $it.id)
            Invoke-Ack $it
            $rest = @(Get-Pending | ForEach-Object { $_.id })
            if ($rest.Count) { $script:Selected = $rest[[Math]::Min([Math]::Max($pos, 0), $rest.Count - 1)] }
            Update-Inbox
        })
    (Get-InboxControl "AckAll").Add_Click({
            $p = @(Get-Pending)
            if (-not $p.Count) { return }
            if (-not (Show-Confirm "Acknowledge all $($p.Count) alerts?" "Each one is marked as acknowledged by you, for every trader and in Heimdall." "Acknowledge all")) { return }
            foreach ($it in $p) { Invoke-Ack $it }
            Update-Inbox
        })
    (Get-InboxControl "Copy").Add_Click({
            $it = $Items[$Selected]
            if (-not $it) { return }
            $lines = @(($it.columns) -join "`t")
            foreach ($r in $it.data) { $lines += (@($r) | ForEach-Object { if ($null -eq $_) { "" } else { "$_" -replace "[`t`r`n]", " " } }) -join "`t" }
            [System.Windows.Clipboard]::SetText($lines -join "`r`n")
        })
    (Get-InboxControl "Mute").Add_Click({ Switch-Mute; Update-Inbox })
    (Get-InboxControl "Restart").Add_Click({ Stop-Gjallarhorn -Restart })
    (Get-InboxControl "Quit").Add_Click({ Stop-Gjallarhorn })
    $w.Add_KeyDown({
            param($x, $e)
            if ($e.Key -eq "Escape") { $x.WindowState = "Minimized" }
        })
    # Closing the window only minimizes it: alerts keep coming. Quit is in the window and the tray menu.
    $w.Add_Closing({ param($x, $e) if (-not $Quitting) { $e.Cancel = $true; $x.WindowState = "Minimized" } })
    return $w
}

function Show-Inbox([string]$id) {
    if (-not $Inbox) { [void](New-Inbox) }
    if ($id) { $script:Selected = $id }
    Update-Inbox
    $Inbox.Show()
    if ($Inbox.WindowState -eq "Minimized") { $Inbox.WindowState = "Normal" }
    # Windows keeps a background process from taking the focus: on top for a moment, then normal.
    $Inbox.Topmost = $true
    [void]$Inbox.Activate()
    $Inbox.Topmost = $false
}

function Switch-Mute {
    if ((Get-Date) -lt $MutedUntil) { $script:MutedUntil = [datetime]::MinValue }
    else {
        $script:MutedUntil = (Get-Date).AddHours(1)
        Hide-Card
    }
    Update-Tray
}

# -- the tray icon: the count of alerts to acknowledge --------------------------------------------------

$IconCache = @{}
function Get-TrayIcon([int]$count, [string]$worst, [bool]$ok) {
    $label = if ($count -gt 9) { "9+" } elseif ($count -gt 0) { "$count" } else { "" }
    $key = "$label|$worst|$ok"
    if ($IconCache.ContainsKey($key)) { return $IconCache[$key] }
    $bmp = New-GjBitmap 32 $ok
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.TextRenderingHint = "AntiAliasGridFit"
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = "Center"; $fmt.LineAlignment = "Center"
    if ($label) {
        $red = if ($worst -eq "critical" -or $worst -eq "warning") { [System.Drawing.Color]::FromArgb(255, 0xF0, 0x64, 0x70) } else { [System.Drawing.Color]::FromArgb(255, 0x5A, 0xA9, 0xE6) }
        $g.FillEllipse((New-Object System.Drawing.SolidBrush $red), 14, 0, 18, 18)
        $small = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
        $g.DrawString($label, $small, [System.Drawing.Brushes]::White, (New-Object System.Drawing.RectangleF(14, 0, 18, 18)), $fmt)
    }
    $g.Dispose()
    $icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
    $IconCache[$key] = $icon
    return $icon
}

$Tray = New-Object System.Windows.Forms.NotifyIcon
function Update-Tray {
    $p = @(Get-Pending)
    $worst = if ($p | Where-Object { $_.severity -eq "critical" }) { "critical" } elseif ($p | Where-Object { $_.severity -eq "warning" }) { "warning" } else { "info" }
    $Tray.Icon = Get-TrayIcon $p.Count $worst $ShareOk
    $text = "Gjallarhorn - " + $(if ($p.Count) { "$($p.Count) to acknowledge" } else { "all acknowledged" })
    if (-not $ShareOk) { $text = "Gjallarhorn - shared folder unreachable" }
    elseif ((Get-Date) -lt $MutedUntil) { $text += " (paused)" }
    $Tray.Text = $text.Substring(0, [Math]::Min(63, $text.Length))
    $MuteItem.Text = if ((Get-Date) -lt $MutedUntil) { "Resume pop-ups" } else { "Pause pop-ups for 1 hour" }
    $SoundItem.Checked = [bool]$State.sound
}

function Show-TestPopUp {
    $id = "local-test-" + [DateTime]::Now.Ticks
    $it = [pscustomobject]@{
        id = $id; alert = "Gjallarhorn test"; group = "Test"; kind = "test"; severity = "warning"
        title = "Gjallarhorn test"; summary = "3 rows"
        lines = @("A sample pop-up shown on this PC only.", "Acknowledging it writes nothing to the share.")
        audience = @(); columns = @("sym", "last", "limit"); data = @(, @("AAPL", 231.4, 230.0)) + @(, @("MSFT", 418.2, 415.0)) + @(, @("7203.T", 2875.0, 2850.0))
        total = 3; needs_ack = $true; fired = Get-Date; ackBy = ""; ackAt = $null; isLocal = $true; sound = "horn:warning"
    }
    $Items[$id] = $it
    $MutedUntil0 = $MutedUntil
    $script:MutedUntil = [datetime]::MinValue
    Add-PopUps @($it)
    $script:MutedUntil = $MutedUntil0
    Update-Tray
    if ($Inbox -and $Inbox.IsVisible) { Update-Inbox }
}

$Menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$Menu.Items.Add("Open Gjallarhorn", $null, { Show-Inbox "" })
$MuteItem = $Menu.Items.Add("Pause pop-ups for 1 hour", $null, { Switch-Mute; if ($Inbox -and $Inbox.IsVisible) { Update-Inbox } })
[void]$Menu.Items.Add("Show a test pop-up", $null, { Show-TestPopUp })
$SoundItem = $Menu.Items.Add("Sounds", $null, {
        $State.sound = -not $State.sound
        Save-State
        Update-Tray
        if ($State.sound) { Invoke-Sound "horn:info" }
    })
[void]$Menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void]$Menu.Items.Add("Restart Gjallarhorn", $null, { Stop-Gjallarhorn -Restart })
[void]$Menu.Items.Add("Quit Gjallarhorn", $null, { Stop-Gjallarhorn })

# Quit (after asking: no alerts until it is started again), or restart: a new copy from the share
# with the same settings, which also picks up a new version.
function Stop-Gjallarhorn([switch]$Restart) {
    if (-not $Restart) {
        $sure = Show-Confirm "Quit Gjallarhorn?" ("No alerts will pop up on this PC until it is started again: " +
            "Start menu > Gjallarhorn, or the next logon. To get it out of the way instead, just close the window.") "Quit" -Danger
        if (-not $sure) { return }
    }
    $script:Quitting = $true
    Write-Log $(if ($Restart) { "restarting" } else { "quit by $User" })
    $Tray.Visible = $false
    if ($Restart) {
        try { $Mutex.ReleaseMutex(); $Mutex.Dispose() } catch {}
        $argv = "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Root `"$Root`" " +
            "-PollSeconds $PollSeconds -LookbackDays $LookbackDays -StateDir `"$StateDir`""
        if ($Desk) { $argv += " -Desk `"$Desk`"" }
        if ($NoSound) { $argv += " -NoSound" }
        Start-Process powershell.exe -ArgumentList $argv -WindowStyle Hidden
    }
    $App.Shutdown()
}
$Tray.ContextMenuStrip = $Menu
$Tray.Add_MouseClick({ param($x, $e) if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Show-Inbox "" } })

# -- run ----------------------------------------------------------------------------------------------

function Invoke-UiPoll {
    try {
        $fresh = Invoke-Poll
        if (-not $ShareOk) { Write-Log "shared folder reachable again" }
        $script:ShareOk = [IO.Directory]::Exists($Root)
        Add-PopUps $fresh
    } catch {
        if ($ShareOk) { Write-Log "poll failed (shared folder unreachable?): $_" }
        $script:ShareOk = $false
    }
    # Acknowledged elsewhere (another trader, or Heimdall): it leaves the card.
    foreach ($it in @($Deck)) { if ($it.needs_ack -and $it.ackBy) { Remove-FromDeck $it } }
    Update-Tray
    if ($Inbox -and $Inbox.IsVisible) { Update-Inbox }
}

$App = New-Object System.Windows.Application
$App.ShutdownMode = [System.Windows.ShutdownMode]::OnExplicitShutdown
$App.Add_DispatcherUnhandledException({
        param($x, $e)
        $ex = $e.Exception
        while ($ex) {
            $where = if ($ex.ErrorRecord) { " at " + ($ex.ErrorRecord.ScriptStackTrace -replace "`r?`n", " < ") } else { "" }
            Write-Log "unexpected: $($ex.GetType().Name): $($ex.Message)$where"
            $ex = $ex.InnerException
        }
        $e.Handled = $true                                   # keep running whatever happens
    })

$Tray.Visible = $true
Write-Log "started for $User (desks: $($Desks -join ', ')) on $Root"
Invoke-UiPoll

$PollTimer = New-Object System.Windows.Threading.DispatcherTimer
$PollTimer.Interval = [TimeSpan]::FromSeconds([Math]::Max(2, $PollSeconds))
$PollTimer.Add_Tick({ Invoke-UiPoll })
$PollTimer.Start()

$SignalTimer = New-Object System.Windows.Threading.DispatcherTimer
$SignalTimer.Interval = [TimeSpan]::FromMilliseconds(400)
$SignalTimer.Add_Tick({
        if ($OpenSignal.WaitOne(0)) { Show-Inbox "" }
        if ($TestSignal.WaitOne(0)) { Show-TestPopUp }
    })
$SignalTimer.Start()

if ($Test) { Show-TestPopUp }
if ($Open) { Show-Inbox "" }

[void]$App.Run()
$Tray.Visible = $false
$Tray.Dispose()
Write-Log "stopped"