<#
.SYNOPSIS
    Run the full Personal Finance Analyser test suite.
    Exit code 0 = all pass, 1 = any failure.
#>

Set-Location $PSScriptRoot

$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm'
Write-Host ""
Write-Host "  Personal Finance Analyser — Test Suite  ($timestamp)" -ForegroundColor Cyan
Write-Host ""

python -m pytest
$exitCode = $LASTEXITCODE

Write-Host ""
if ($exitCode -eq 0) {
    Write-Host "  ALL TESTS PASSED" -ForegroundColor Green
} else {
    Write-Host "  TESTS FAILED  (exit code $exitCode)" -ForegroundColor Red
}
Write-Host ""

exit $exitCode
