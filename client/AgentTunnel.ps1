[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Start', 'Stop', 'Status', 'Run')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$SshAlias,
    [string]$SshConfig = (Join-Path $env:USERPROFILE '.ssh\config'),
    [ValidateRange(1024, 65535)][int]$ViewerPort = 16080,
    [ValidateRange(1024, 65535)][int]$McpPort = 18931,
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'onePVE\tunnels')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-AgentProcessArgument([string]$Value) {
    # Start-Process joins ArgumentList; use the Windows argv quoting rules.
    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) { return $Value }
    return '"' + ([regex]::Replace(
        ([regex]::Replace($Value, '(\\*)"', '$1$1\"')), '(\\+)$', '$1$1')) + '"'
}

function Get-AgentTunnelNames([string]$AliasName, [string]$ConfigPath, [int]$ViewPort,
                              [int]$BrowserPort, [string]$Root) {
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        $value = "$AliasName`n$ConfigPath`n$ViewPort`n$BrowserPort`n$Root"
        $suffix = -join ($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($value)) |
            ForEach-Object { $_.ToString('x2') })
    } finally { $hash.Dispose() }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    return @{ Directory = (Join-Path $Root $suffix.Substring(0, 16))
              Stop = "Local\onePVE-tunnel-$sid-$suffix-stop"
              Gate = "Local\onePVE-tunnel-$sid-$suffix-gate"
              Run = "Local\onePVE-tunnel-$sid-$suffix-run" }
}

function Test-AgentTunnelRunning([string]$EventName) {
    try {
        $control = [Threading.EventWaitHandle]::OpenExisting($EventName)
        $control.Dispose()
        return $true
    } catch [Threading.WaitHandleCannotBeOpenedException] { return $false }
}

function Get-AgentTunnelStatus([hashtable]$Names) {
    $running = Test-AgentTunnelRunning $Names.Stop
    $statePath = Join-Path $Names.Directory 'status.json'
    $state = $null
    if (Test-Path -LiteralPath $statePath) {
        try { $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json }
        catch { } # A status file is diagnostic; it never authorizes process termination.
    }
    return [pscustomobject]@{ Running = $running; State = $state
                             StateDirectory = $Names.Directory }
}

function Write-AgentTunnelStatus([hashtable]$Names, [string]$Phase, [string]$RunId,
                                  [int]$ChildId = 0, [string]$Detail = '') {
    $state = @{ phase = $Phase; run_id = $RunId; supervisor_pid = $PID
                ssh_pid = $ChildId; updated_utc = [DateTime]::UtcNow.ToString('o')
                detail = $Detail }
    $temporary = Join-Path $Names.Directory "status-$RunId.tmp"
    [IO.File]::WriteAllText($temporary, ($state | ConvertTo-Json),
        (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination (Join-Path $Names.Directory 'status.json') -Force
}

function Get-AgentTunnelSshArguments([string]$AliasName, [string]$ConfigPath,
                                      [int]$ViewPort, [int]$BrowserPort) {
    return @('-F', $ConfigPath, '-N', '-T', '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=10',
        '-o', 'GatewayPorts=no', '-o', 'ClearAllForwardings=no',
        '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
        '-o', 'PermitLocalCommand=no',
        '-L', "127.0.0.1:${ViewPort}:127.0.0.1:6080",
        '-L', "127.0.0.1:${BrowserPort}:127.0.0.1:8931", $AliasName)
}

function Assert-AgentTunnelAlias([string]$SshPath, [string]$AliasName, [string]$ConfigPath) {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Configure the SSH alias in $ConfigPath first."
    }
    $effective = @(& $SshPath -G -T -F $ConfigPath -o BatchMode=yes `
        -o StrictHostKeyChecking=yes -o PermitLocalCommand=no $AliasName 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH could not read the configured alias.' }
    if (-not ($effective -match '^hostname \S+') -or -not ($effective -match '^user \S+')) {
        throw 'The SSH alias must resolve to a hostname and user.'
    }
    if ($effective -match '^(localforward|remoteforward|dynamicforward) ') {
        throw 'Use an SSH alias without configured forwards; this helper supplies its own loopback forwards.'
    }
}

function Assert-AgentTunnelPorts([int]$ViewPort, [int]$BrowserPort) {
    foreach ($port in @($ViewPort, $BrowserPort)) {
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $port)
        try {
            $listener.Server.ExclusiveAddressUse = $true
            $listener.Start()
        } catch { throw "Local port $port is occupied. Stop its owner or choose another port." }
        finally { $listener.Stop() }
    }
}

function Invoke-AgentTunnelLoop([hashtable]$Names, [string]$SshPath, [string[]]$SshArguments) {
    $runLock = New-Object Threading.Mutex($false, $Names.Run)
    $ownsLock = $false
    $control = $null
    $child = $null
    $runId = [guid]::NewGuid().ToString('N')
    try {
        try { $ownsLock = $runLock.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $ownsLock = $true }
        if (-not $ownsLock) { throw 'This tunnel already has a reconnect process.' }
        $control = New-Object Threading.EventWaitHandle($false,
            [Threading.EventResetMode]::ManualReset, $Names.Stop)
        Write-AgentTunnelStatus $Names 'starting' $runId
        $argumentLine = ($SshArguments | ForEach-Object { ConvertTo-AgentProcessArgument $_ }) -join ' '
        while (-not $control.WaitOne(0)) {
            try {
                $child = Start-Process -FilePath $SshPath -ArgumentList $argumentLine `
                    -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput (Join-Path $Names.Directory 'ssh.stdout.log') `
                    -RedirectStandardError (Join-Path $Names.Directory 'ssh.stderr.log')
                Write-AgentTunnelStatus $Names 'ssh-running' $runId $child.Id
                while (-not $child.WaitForExit(250)) {
                    if ($control.WaitOne(0)) { break }
                }
                if ($control.WaitOne(0)) { break }
                $exitCode = $child.ExitCode
                Write-AgentTunnelStatus $Names 'retrying' $runId 0 "ssh exit $exitCode"
            } catch {
                Write-AgentTunnelStatus $Names 'retrying' $runId 0 $_.Exception.Message
            } finally {
                if ($null -ne $child) {
                    # Only the object returned by this loop can be terminated.
                    if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(5000) }
                    $child.Dispose()
                    $child = $null
                }
            }
            if ($control.WaitOne(3000)) { break }
        }
    } finally {
        if ($null -ne $child) {
            # This Process object came from our own Start-Process, never from a saved PID.
            if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(5000) }
            $child.Dispose()
        }
        try { if ($ownsLock) { Write-AgentTunnelStatus $Names 'stopped' $runId } }
        finally {
            if ($null -ne $control) { $control.Dispose() }
            if ($ownsLock) { $runLock.ReleaseMutex() }
            $runLock.Dispose()
        }
    }
}

function Invoke-AgentTunnel {
    if ($env:OS -ne 'Windows_NT') { throw 'This helper requires Windows OpenSSH and PowerShell.' }
    if ($ViewerPort -eq $McpPort) { throw 'ViewerPort and McpPort must differ.' }
    $configPath = [IO.Path]::GetFullPath($SshConfig)
    $rootPath = [IO.Path]::GetFullPath($StateRoot)
    $names = Get-AgentTunnelNames $SshAlias $configPath $ViewerPort $McpPort $rootPath
    if ($Action -eq 'Status') { return Get-AgentTunnelStatus $names }
    if ($Action -eq 'Stop') {
        try { $control = [Threading.EventWaitHandle]::OpenExisting($names.Stop) }
        catch [Threading.WaitHandleCannotBeOpenedException] { return Get-AgentTunnelStatus $names }
        $null = $control.Set()
        $control.Dispose()
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if (-not (Test-AgentTunnelRunning $names.Stop)) { return Get-AgentTunnelStatus $names }
            Start-Sleep -Milliseconds 250
        }
        throw 'Stop was requested but the reconnect process has not exited; no unrelated process was terminated.'
    }

    $sshPath = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    Assert-AgentTunnelAlias $sshPath $SshAlias $configPath
    if ($Action -eq 'Run') {
        $sshArguments = Get-AgentTunnelSshArguments $SshAlias $configPath $ViewerPort $McpPort
        Invoke-AgentTunnelLoop $names $sshPath $sshArguments
        return
    }

    $gate = New-Object Threading.Mutex($false, $names.Gate)
    $ownsGate = $false
    try {
        try { $ownsGate = $gate.WaitOne(10000) }
        catch [Threading.AbandonedMutexException] { $ownsGate = $true }
        if (-not $ownsGate) { throw 'Another tunnel start is in progress.' }
        if (Test-AgentTunnelRunning $names.Stop) { return Get-AgentTunnelStatus $names }
        Assert-AgentTunnelPorts $ViewerPort $McpPort
        $null = New-Item -ItemType Directory -Force -Path $names.Directory
        $shellName = if ($PSVersionTable.PSEdition -eq 'Desktop') { 'powershell.exe' } else { 'pwsh.exe' }
        $shellPath = Join-Path $PSHOME $shellName
        $launchArguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', $PSCommandPath, '-Action', 'Run', '-SshAlias', $SshAlias,
            '-SshConfig', $configPath, '-ViewerPort', "$ViewerPort", '-McpPort', "$McpPort",
            '-StateRoot', $rootPath)
        $argumentLine = ($launchArguments | ForEach-Object { ConvertTo-AgentProcessArgument $_ }) -join ' '
        # Shell execution detaches from a caller's captured pipes on Windows.
        $supervisor = Start-Process -FilePath $shellPath -ArgumentList $argumentLine -WindowStyle Hidden -PassThru
        try {
            for ($attempt = 0; $attempt -lt 100; $attempt++) {
                if (Test-AgentTunnelRunning $names.Stop) { return Get-AgentTunnelStatus $names }
                if ($supervisor.HasExited) { throw "Reconnect process exited; inspect $($names.Directory)." }
                Start-Sleep -Milliseconds 100
            }
            throw "Reconnect startup timed out; inspect $($names.Directory) and run Stop."
        } finally { $supervisor.Dispose() }
    } finally {
        if ($ownsGate) { $gate.ReleaseMutex() }
        $gate.Dispose()
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-AgentTunnel }
