; 码粮 CodeFuel 的 Windows 安装程序定义（Inno Setup 6）。
; 编译：ISCC.exe /DMyAppVersion=0.3.0 build\installer.iss
; 输入是 PyInstaller 产出的 dist\CodeFuel 目录，输出到 dist\CodeFuel-<版本>-setup.exe。

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "码粮 CodeFuel"
#define MyAppShortName "CodeFuel"
#define MyAppPublisher "weiguang1017"
#define MyAppURL "https://github.com/weiguang1017/codefuel"
#define MyAppExeName "CodeFuel.exe"

[Setup]
AppId={{8F3C1A6E-2D45-4B71-9E8A-5C7D3F1B9A24}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 64 位程序装进 Program Files，避免 WOW64 重定向。
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 打包体积约 180MB，安装完即可用，不需要拆分成 cab 分卷。
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename={#MyAppShortName}-{#MyAppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 整个 PyInstaller 目录都要带上，Node 驱动和 .dll 缺一不可。
Source: "..\dist\CodeFuel\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
