; pfa_setup.iss — Inno Setup 6 script for Personal Finance Analyser
;
; Prerequisites:
;   1. Run .\build_windows.ps1 first to produce dist\PersonalFinanceAnalyser\
;   2. Install Inno Setup 6: https://jrsoftware.org/isdl.php
;   3. Compile: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" pfa_setup.iss
;      (build_windows.ps1 does this automatically if ISCC.exe is on PATH)
;
; Output: dist\PersonalFinanceAnalyser-1.0.0-Setup.exe
;
; Install location: %LOCALAPPDATA%\PersonalFinanceAnalyser
;   - No UAC / admin prompt required (PrivilegesRequired=lowest)
;   - Data\ and config.yaml live in the same directory as the exe (existing behaviour)
;   - Upgrades replace binaries only; Data\ and config.yaml are never touched

#define AppName      "Personal Finance Analyser"
#define AppVersion   "1.0.0"
#define AppPublisher "PFA"
#define AppExeName   "PersonalFinanceAnalyser.exe"
#define SourceDir    "dist\PersonalFinanceAnalyser"

[Setup]
; Unique GUID — do not change after first release (identifies this app to Windows)
AppId={{6F2A3E1B-84DC-4C9A-B7F5-920D3A5E1C47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; No icon yet — uncomment when static\favicon.ico exists:
; SetupIconFile=static\favicon.ico
OutputDir=dist
OutputBaseFilename=PersonalFinanceAnalyser-{#AppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; No UAC prompt — installs entirely in user profile
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; Prompt to close the app if it's running before upgrade
CloseApplications=yes
CloseApplicationsFilter=PersonalFinanceAnalyser.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checked

[Files]
; ── Binaries (replaced on every upgrade) ─────────────────────────────────────
; Main executable
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Python runtime + all dependencies (bundled by PyInstaller into _internal\)
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; User-facing docs
Source: "{#SourceDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

; ── Intentionally excluded ────────────────────────────────────────────────────
; Data\          — user financial data; never distributed; never overwritten
; config.yaml    — contains personal account details; user creates/edits this
; reports\       — generated output; not part of the install

[Icons]
; Start Menu
Name: "{autoprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autoprograms}\{#AppName}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Desktop (optional — user-selected task)
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; "Launch now" checkbox on the final installer page
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Intentionally empty — never auto-delete Data\ or config.yaml on uninstall.
; The user must remove personal data manually.

[Code]
// ── Post-install: create a minimal starter config.yaml if none exists ─────────
// This only runs when there is no existing config (i.e. fresh install, not upgrade).
procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath: String;
  Lines: TStringList;
begin
  if CurStep <> ssPostInstall then Exit;

  ConfigPath := ExpandConstant('{app}\config.yaml');
  if FileExists(ConfigPath) then Exit;   // preserve existing config on upgrade

  Lines := TStringList.Create;
  try
    Lines.Add('# Personal Finance Analyser — configuration');
    Lines.Add('# Edit this file to set up your accounts and preferences.');
    Lines.Add('# See README.txt and Help (? button in the app) for full documentation.');
    Lines.Add('');
    Lines.Add('server:');
    Lines.Add('  port: 5100');
    Lines.Add('  # To set a local password (optional):');
    Lines.Add('  # password_hash: pbkdf2:sha256:100000:<salt_hex>:<hash_hex>');
    Lines.Add('');
    Lines.Add('# Bank accounts — add one entry per account.');
    Lines.Add('# Supported types: anz_csv, anz_plus_pdf, anz_access_advantage,');
    Lines.Add('#   latitude_html, paypal_csv, revolut_csv, wise_pdf,');
    Lines.Add('#   commbank_csv, westpac_csv, nab_csv, ofx (.ofx/.qfx)');
    Lines.Add('accounts: {}');
    Lines.Add('');
    Lines.Add('# Category overrides — map merchant name fragments to categories.');
    Lines.Add('categories: {}');
    Lines.SaveToFile(ConfigPath);
    MsgBox('A starter config.yaml has been created in:' + #13#10 + ExpandConstant('{app}') + #13#10#13#10 +
           'Edit it to add your bank accounts before importing statements.', mbInformation, MB_OK);
  finally
    Lines.Free;
  end;
end;

// ── Uninstall: warn before removing if Data\ contains files ──────────────────
function InitializeUninstall(): Boolean;
var
  DataDir: String;
  Msg: String;
begin
  Result := True;
  DataDir := ExpandConstant('{app}\Data');
  if DirExists(DataDir) then
  begin
    Msg := 'Your financial data is stored in:' + #13#10 + DataDir + #13#10#13#10 +
           'Uninstalling will remove the application but will NOT delete your data.' + #13#10 +
           'You can safely delete that folder manually afterwards if you wish.' + #13#10#13#10 +
           'Continue uninstalling?';
    Result := (MsgBox(Msg, mbConfirmation, MB_YESNO) = IDYES);
  end;
end;
