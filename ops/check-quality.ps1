# ops/check-quality.ps1
# Одна Windows-команда: воспроизводимые зависимости, lint/format и offline Python/JS/E2E.
param([switch]$Browser, [switch]$Live)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Invoke-Checked {
    # Прерывает проверки при первом ненулевом exit code; логи остаются видимыми.
    param([string]$Program, [string[]]$Arguments)
    $PreviousPreference = $ErrorActionPreference
    $PreviousEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $ErrorActionPreference = 'Continue'
    try {
        # Windows PowerShell 5.1 ошибочно считает informational stderr uv исключением.
        # Оба потока остаются видимыми; результат определяется реальным exit code.
        & $Program @Arguments 2>&1 | ForEach-Object { Write-Output $_.ToString() }
        $ProcessExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
        [Console]::OutputEncoding = $PreviousEncoding
    }
    if ($ProcessExitCode -ne 0) { throw "Проверка $Program завершилась с кодом $ProcessExitCode" }
}
Invoke-Checked uv @('sync', '--frozen')
Invoke-Checked uv @('run', '--frozen', 'ruff', 'check', 'app', 'tests', 'scripts', 'migrations')
Invoke-Checked uv @('run', '--frozen', 'ruff', 'format', '--check', 'app', 'tests', 'scripts', 'migrations')
# Node поставляется закреплённой версией Playwright; системный Node не требуется.
$NodeBinary = & .\.venv\Scripts\python.exe -c 'from playwright._impl._driver import compute_driver_executable; print(compute_driver_executable()[0])'
if ($LASTEXITCODE -ne 0) { throw 'Не удалось найти Node в установленном Playwright' }
Invoke-Checked ($NodeBinary.Trim()) @('--test', 'tests/frontend/state.test.js')
if ($Browser) { Invoke-Checked uv @('run', '--frozen', 'playwright', 'install', 'chromium') }
$TestArguments = @('run', '--frozen', 'pytest', '--cov=app', '--cov-report=term-missing', '--cov-report=json:reports/coverage.json')
if (-not $Browser -and -not $Live) { $TestArguments += @('-m', 'not live and not browser') }
elseif ($Browser -and -not $Live) {
    $TestArguments += @('-m', 'not live')
}
elseif (-not $Browser) { $TestArguments += @('-m', 'not browser') }
if ($Live) { $TestArguments += @('--postgres', '--broker', '--vlm') }
Invoke-Checked uv $TestArguments
Invoke-Checked git @('-c', ('safe.directory=' + (Get-Location).Path.Replace('\', '/')), 'diff', '--check')
