# build_windows.ps1 — Build Personal Finance Analyser as a self-contained Windows exe.
#
# Usage:
#   .\build_windows.ps1
#
# Output:
#   dist\PersonalFinanceAnalyser\PersonalFinanceAnalyser.exe
#
# Requirements:
#   pip install pyinstaller
#
# The resulting folder can be zipped and distributed.
# Users do NOT need Python installed.
# On first launch, create a config.yaml in the same folder as the exe.

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

Write-Host ""
Write-Host "=== Personal Finance Analyser — Windows Build ===" -ForegroundColor Cyan
Write-Host "Root: $Root"
Write-Host ""

# ── 1. Verify PyInstaller is available ────────────────────────────────────────
$pyinstaller = $null
try {
    $pyinstaller = (python -c "import PyInstaller; print(PyInstaller.__version__)") 2>&1
    Write-Host "PyInstaller $pyinstaller found." -ForegroundColor Green
} catch {
    Write-Host "PyInstaller not found. Install it with: pip install pyinstaller" -ForegroundColor Red
    exit 1
}

# ── 2. Clean previous build ────────────────────────────────────────────────────
Write-Host "Cleaning previous build artifacts..."
if (Test-Path "$Root\build") { Remove-Item "$Root\build" -Recurse -Force }
if (Test-Path "$Root\dist")  { Remove-Item "$Root\dist"  -Recurse -Force }

# ── 3. Run PyInstaller ─────────────────────────────────────────────────────────
Write-Host "Running PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller "$Root\pfa.spec" --distpath "$Root\dist" --workpath "$Root\build" --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller failed." -ForegroundColor Red
    exit 1
}

$OutDir = "$Root\dist\PersonalFinanceAnalyser"
Write-Host ""
Write-Host "Build complete: $OutDir" -ForegroundColor Green

# ── 4. Copy user-facing files into the dist folder ────────────────────────────
# Use config.yaml.example (clean template, no personal data) as the starter
# config in the distributable. Never copy the developer's personal config.yaml.
Write-Host "Copying config template..."
if (Test-Path "$Root\config.yaml.example") {
    Copy-Item "$Root\config.yaml.example" "$OutDir\config.yaml" -Force
    Write-Host "  config.yaml (from config.yaml.example) copied." -ForegroundColor Green
} else {
    Write-Host "  config.yaml.example not found — dist folder has no starter config." -ForegroundColor Yellow
    Write-Host "  Recipients will need to create config.yaml manually." -ForegroundColor Yellow
}

# ── 5. Create a minimal README for end users ──────────────────────────────────
$readme = @"
Personal Finance Analyser
=========================

1. Edit config.yaml to point at your bank statement folder and set your name/accounts.
2. Double-click PersonalFinanceAnalyser.exe.
   - The app opens in your default browser automatically.
   - A console window stays open showing log output.
3. Your data lives in the Data\ folder next to the exe — back it up via OneDrive.

To stop the server: close the console window or press Ctrl+C in it.

First-time setup
----------------
The app includes a starter config.yaml — edit it to add your bank accounts
before importing statements. The in-app Help page (? button) explains each
setting. The setup wizard (shown on first launch) guides you through the basics.

If you have an Anthropic API key for AI categorisation, set it as a Windows
environment variable (does NOT go in config.yaml):
  [Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
"@
$readme | Out-File -Encoding utf8 "$OutDir\README.txt"
Write-Host "  README.txt written." -ForegroundColor Green

# ── 6. Inno Setup — build Setup.exe installer ─────────────────────────────────
$iscc = $null
foreach ($candidate in @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    (Get-Command iscc -ErrorAction SilentlyContinue)?.Source
)) {
    if ($candidate -and (Test-Path $candidate)) { $iscc = $candidate; break }
}

if ($iscc) {
    Write-Host ""
    Write-Host "Running Inno Setup..." -ForegroundColor Cyan
    & $iscc "$Root\pfa_setup.iss"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Inno Setup failed (exit $LASTEXITCODE)." -ForegroundColor Yellow
    } else {
        $setupExe = Get-ChildItem "$Root\dist" -Filter "*-Setup.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($setupExe) {
            $sizeMB = [math]::Round($setupExe.Length / 1MB, 1)
            Write-Host "Installer: $($setupExe.FullName) ($sizeMB MB)" -ForegroundColor Green
        }
    }
} else {
    Write-Host ""
    Write-Host "Inno Setup not found — skipping installer step." -ForegroundColor Yellow
    Write-Host "To build a Setup.exe, install Inno Setup 6 from:"
    Write-Host "  https://jrsoftware.org/isdl.php" -ForegroundColor Cyan
    Write-Host "Then re-run this script — it will be picked up automatically."
}

# ── 7. Summary ────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
Write-Host "Distributable folder: $OutDir"
Write-Host "Exe size: $([math]::Round((Get-Item "$OutDir\PersonalFinanceAnalyser.exe").Length / 1MB, 1)) MB"
$totalMB = [math]::Round((Get-ChildItem $OutDir -Recurse | Measure-Object Length -Sum).Sum / 1MB, 0)
Write-Host "Total folder size: $totalMB MB"
Write-Host ""
Write-Host "To test: & `"$OutDir\PersonalFinanceAnalyser.exe`""
Write-Host "To zip:  Compress-Archive `"$OutDir`" `"$Root\dist\PersonalFinanceAnalyser.zip`""
Write-Host ""
