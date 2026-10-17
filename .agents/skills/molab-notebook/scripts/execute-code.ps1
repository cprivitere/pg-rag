# Execute code in a running marimo session's scratchpad (Windows PowerShell port).
# No marimo installation, no bash/jq/curl needed — talks directly to the HTTP API
# with Invoke-RestMethod / .NET HttpClient.
#
# Usage:
#   execute-code.ps1 -Url URL [-File PATH | -Session ID] -Code "code"
#   execute-code.ps1 -Url URL [-File PATH | -Session ID] script.py
#   some-pipeline | execute-code.ps1 -Url URL [-File PATH | -Session ID]
#
# Find the URL + token with the sibling molab-connect.ps1: it caches the
# sandbox's connect block (`-Pasteline "<block>" -Save`) and `-Info` prints the
# cached url/token plus a live kernel probe. With one notebook open on the
# server the session is resolved automatically. -File stably identifies a
# notebook; -Session targets an exact (but ephemeral) session ID.
#
# Auth: set MARIMO_TOKEN env var (preferred) or pass -Token TOKEN (visible in ps).
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Url,
    [string]$Code,
    [string]$File,
    [Parameter(Position = 0)]
    [AllowEmptyString()]
    [string]$ScriptPath,
    [string]$Token = $env:MARIMO_TOKEN
)

Add-Type -AssemblyName System.Net.Http

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Usage {
    Write-Host "Usage: execute-code.ps1 -Url URL [-File PATH | -Session ID] -Code 'code'" -ForegroundColor Yellow
    Write-Host "       execute-code.ps1 -Url URL [-File PATH | -Session ID] script.py" -ForegroundColor Yellow
    Write-Host "URL:   run molab-connect.ps1 -Info (cached url + token + kernel probe), or" -ForegroundColor Yellow
    Write-Host "       delegate through molab-connect.ps1, which fills -Url/-Token from cache." -ForegroundColor Yellow
    Write-Host "Auth:  set MARIMO_TOKEN env var (preferred) or pass -Token TOKEN" -ForegroundColor Yellow
    exit 1
}

if (-not $Code -and -not $File -and -not $Session -and -not $ScriptPath -and -not [Console]::IsInputRedirected) { Usage }
if ($File -and $Session) {
    Write-Error "-File and -Session are mutually exclusive."
    Usage
}

# --- pull code from -ScriptPath positional or stdin ------------------------------
$script = $Code
if (-not $script) {
    if ($ScriptPath -eq "-") {
        # Bash-style '-' means stdin (harness pipes code on stdin).
        if (-not [Console]::IsInputRedirected) { Usage }
        $script = [Console]::In.ReadToEnd().TrimEnd()
    } elseif ($ScriptPath) {
        if (Test-Path $ScriptPath) {
            $script = Get-Content -Path $ScriptPath -Raw
        } else {
            Write-Error "Code file not found: $ScriptPath"
            exit 1
        }
    } elseif ([Console]::IsInputRedirected) {
        # Read piped stdin (code piped via '-'). Windows PowerShell 5.1
        # `powershell -File` cannot pass a bare '-' argument at all, so a plain
        # pipe with no positional also selects this branch.
        $script = [Console]::In.ReadToEnd().TrimEnd()
    } else {
        Usage
    }
}

if (-not $script) {
    Write-Error "No code provided."
    Usage
}

$base = $Url.TrimEnd('/')

# --- optional auth header -------------------------------------------------------
$headers = @{}
if ($Token) {
    $headers["Authorization"] = "Bearer $Token"
}

# --- resolve session ID -----------------------------------------------------------
# A file key is stable across browser reconnects, while a session ID names one
# particular browser/kernel connection.
if ($Session) {
    $sessionId = $Session
} else {
    try {
        $sessionsResp = Invoke-RestMethod -Uri "$base/api/sessions" -TimeoutSec 5 -Headers $headers -Method GET
    } catch {
        Write-Error "Failed to connect to marimo server at ${base}: $($_.Exception.Message)"
        exit 1
    }

    $sessions = @()
    if ($sessionsResp) {
        foreach ($prop in $sessionsResp.PSObject.Properties) {
            $sessions += [pscustomobject]@{
                key      = $prop.Name
                path     = [string]$prop.Value.path
                filename = [string]$prop.Value.filename
            }
        }
    }

    if ($sessions.Count -eq 0) {
        Write-Error "No active sessions on the server. Make sure a notebook is open in the browser."
        exit 1
    }

    if ($File) {
        $matching = @($sessions | Where-Object { $_.path -eq $File -or $_.filename -eq $File })
        if ($matching.Count -eq 0) {
            Write-Error "No active session matches -File '$File'. Available sessions:"
            foreach ($s in $sessions) {
                Write-Host "  $($s.key)  $($s.path)" -ForegroundColor Yellow
            }
            exit 1
        }
        if ($matching.Count -gt 1) {
            Write-Error "Multiple active sessions match -File '$File':"
            foreach ($s in $matching) {
                Write-Host "  $($s.key)  $($s.path)" -ForegroundColor Yellow
            }
            exit 1
        }
        $sessionId = $matching[0].key
    } else {
        if ($sessions.Count -gt 1) {
            Write-Error "Multiple sessions on server. Select one with -File or -Session:"
            foreach ($s in $sessions) {
                Write-Host "  $($s.key)  $($s.path)" -ForegroundColor Yellow
            }
            exit 1
        }
        $sessionId = $sessions[0].key
    }
}

# --- execute via SSE stream ---------------------------------------------------------
# Events: stdout/stderr stream as JSON {"data":"..."}, done is final result.
$exitCode = 0
$doneReceived = $false
# Get-Content -Raw attaches PSPath/PSDrive ETS properties which would
# serialize the whole FileInfo object; unwrap to the plain string first.
$codeJson = [string]$script | ConvertTo-Json -Compress
$bodyJson = '{"code": ' + $codeJson + '}'
$unparsed = New-Object System.Text.StringBuilder

$http = [System.Net.Http.HttpClient]::new()
$http.Timeout = [TimeSpan]::FromMinutes(10)
if ($Token) {
    $http.DefaultRequestHeaders.Add("Authorization", "Bearer $Token")
}

$response = $null
$stream = $null
try {
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::Post,
        "$base/api/kernel/execute"
    )
    $request.Content = [System.Net.Http.StringContent]::new(
        $bodyJson,
        [System.Text.Encoding]::UTF8,
        "application/json"
    )
    $request.Headers.Add("Marimo-Session-Id", $sessionId)

    $response = $http.SendAsync($request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    if (-not $response.IsSuccessStatusCode) {
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        try {
            $errObj = $body | ConvertFrom-Json
            $detail = if ($errObj.detail) { $errObj.detail } else { $body }
        } catch { $detail = $body }
        Write-Error "Execution did not complete: HTTP $([int]$response.StatusCode) from ${base}: $detail"
        exit 1
    }
    $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8)

    $currentEvent = ""
    while (-not $reader.EndOfStream -and -not $doneReceived) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        $line = $line.TrimEnd("`r")

        if ($line -match '^event:\s*(.+)$') {
            $currentEvent = $Matches[1].Trim()
        } elseif ($line -eq "") {
            # SSE record separator
        } elseif ($line -match '^data:\s*(.*)$') {
            $payload = $Matches[1]
            try { $obj = $payload | ConvertFrom-Json } catch { $obj = $null }

            switch ($currentEvent) {
                "stdout" {
                    if ($obj -and $obj.data) { [Console]::Out.Write([string]$obj.data) }
                }
                "stderr" {
                    if ($obj -and $obj.data) { [Console]::Error.Write([string]$obj.data) }
                }
                "done" {
                    # Carries the success bit and output only; errors arrived as stderr.
                    if ($obj -and $obj.success -eq $false) {
                        $exitCode = 1
                    } elseif ($obj -and $obj.output -and $obj.output.data) {
                        [Console]::Out.Write([string]$obj.output.data)
                    }
                    $doneReceived = $true
                }
                default {
                    [void]$unparsed.AppendLine($line)
                }
            }
        } else {
            # Not SSE at all - an error body rather than a stream.
            [void]$unparsed.AppendLine($line)
        }
    }
} catch {
    Write-Error "Execution failed: $($_.Exception.Message)"
    exit 1
} finally {
    if ($stream) { $stream.Dispose() }
    if ($response) { $response.Dispose() }
    $http.Dispose()
}

if (-not $doneReceived) {
    # No `done` event means the code never ran; without this we would exit 0.
    Write-Error "Execution did not complete: the server ended the stream without a result."
    if ($unparsed.Length -gt 0) {
        $errText = $unparsed.ToString()
        try {
            $errObj = $errText | ConvertFrom-Json
            if ($errObj.detail) { Write-Error $errObj.detail } else { Write-Error $errText }
        } catch {
            Write-Error $errText
        }
    }
    if ($File) {
        Write-Error "The session for -File may have changed during execution. Retry the same -File command."
    } elseif ($Session) {
        Write-Error "The -Session id may be stale: marimo renames a session when the browser reconnects. Retry without -Session."
    }
    exit 1
}

exit $exitCode
