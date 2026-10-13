#!/usr/bin/env pwsh
#MISE description="Start Gradio chat on :7860 with logging (primary chat UI; refreshes the store at startup)"
#MISE alias="sc"
#MISE depends=["ensure-logs-dir"]

param()

$root = Split-Path -Parent $PSScriptRoot
$root = Split-Path -Parent $root
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$existing = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Chat already running on :7860 (PID $($existing.OwningProcess))"
    exit 0
}

$exe = 'uv'
$serverArgs = @('run','--with','gradio','python','scripts/rag_chat.py')
$outLog = Join-Path $logDir 'chat.log'
$errLog = Join-Path $logDir 'chat-error.log'
# PG_RAG_CHAT_NO_BROWSER=1: this is the serving (mise start) path — don't pop a
# browser. The foreground `mise chat` task still auto-opens one.
$batPath = [IO.Path]::GetTempFileName() + '.bat'
$batContent = "@echo off`ncd /d `"$root`"`nSET PG_RAG_CHAT_NO_BROWSER=1`n`"$exe`" $($serverArgs -join ' ') > `"$outLog`" 2> `"$errLog`""
[IO.File]::WriteAllText($batPath, $batContent)

Start-Process -FilePath 'cmd.exe' -ArgumentList "/c `"$batPath`"" -WindowStyle Hidden

Write-Host "Started Gradio chat (:7860) - logs in logs/chat.log"