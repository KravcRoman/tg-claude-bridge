# Разовая headless-авторизация Claude Code CLI — нужна для inbound (ответы из Telegram в сессию).
# Запуск:  powershell -ExecutionPolicy Bypass -File setup-token.ps1
$cli = Get-ChildItem "$env:APPDATA\Claude\claude-code\*\claude.exe" -ErrorAction SilentlyContinue |
    Sort-Object { try { [version]$_.Directory.Name } catch { [version]'0.0.0' } } |
    Select-Object -Last 1
if (-not $cli) {
    Write-Error "Claude Code CLI не найден в $env:APPDATA\Claude\claude-code"
    exit 1
}
Write-Host "CLI: $($cli.FullName)"
& $cli.FullName setup-token
