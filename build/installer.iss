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
; 只用 Inno Setup 自带的英文语言文件：官方安装包不含简体中文 .isl，
; 额外下载会让 CI 依赖外网且不可复现。界面文案已用中文写在下面各段。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
english.WelcomeLabel1=欢迎安装 [name]
english.WelcomeLabel2=即将把 [name] 安装到你的电脑。%n%n这是本地桌面应用，账号凭据加密保存在本机，不会上传。%n%n继续前请关闭正在运行的 CodeFuel。
english.FinishedHeadingLabel=[name] 安装完成
english.FinishedLabelNoIcons=[name] 已安装完成，可以直接启动。
english.FinishedLabel=[name] 已安装完成。勾选下面的选项即可立即启动。
english.SelectDirLabel3=选择安装位置，然后点「下一步」继续。
english.SelectStartMenuFolderLabel3=选择开始菜单文件夹名称。
english.SelectTasksLabel2=选择附加任务，然后点「下一步」继续。
english.ReadyLabel1=准备就绪，即将开始安装。
english.ReadyLabel2a=点「安装」开始安装 [name]。
english.ClickInstallToContinue=点「安装」开始安装。
english.ConfirmUninstall=确定要卸载 [name] 吗？%n%n账号数据保存在用户目录下，不会随卸载删除。
english.UninstalledAll=[name] 已卸载完成。
english.ButtonNext=下一步(&N) >
english.ButtonBack=< 上一步(&B)
english.ButtonInstall=安装(&I)
english.ButtonCancel=取消
english.ButtonFinish=完成(&F)
english.ButtonBrowse=B 浏览…
english.ButtonNo=否(&N)
english.ButtonYes=是(&Y)

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[CustomMessages]
LaunchApp=立即启动 码粮 CodeFuel

[Files]
; 整个 PyInstaller 目录都要带上，Node 驱动和 .dll 缺一不可。
Source: "..\dist\CodeFuel\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
