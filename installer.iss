; PropCopy Installer — Inno Setup Script
; Build with: iscc installer.iss (after running build.bat)

#define MyAppName "PropCopy"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "PropCopy contributors"
#define MyAppExeName "PropCopy.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=installer_output
OutputBaseFilename=PropCopy_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Main executable
Source: "dist\PropCopy.exe"; DestDir: "{app}"; Flags: ignoreversion

; Example configs (only install if user doesn't already have real configs)
Source: "config.example.yaml"; DestDir: "{app}"; DestName: "config.example.yaml"; Flags: ignoreversion
Source: "accounts.example.yaml"; DestDir: "{app}"; DestName: "accounts.example.yaml"; Flags: ignoreversion

; MT4 EA bridge (user copies to their MT4 terminal)
; Uncomment the line below once PropCopy_Bridge.mq4 is in the project root
;Source: "PropCopy_Bridge.mq4"; DestDir: "{app}\mt4_ea"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch PropCopy"; Flags: nowait postinstall skipifsilent

[Dirs]
; Create data directory for analytics/trade mapping
Name: "{app}\data"
