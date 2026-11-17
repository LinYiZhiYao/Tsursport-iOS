; Tsursport iOS —— Inno Setup 脚本（英文语言包 + 中文界面文案覆盖）
; 编译：ISCC.exe Tsursport.iss

#define AppName "Tsursport iOS"
#define AppVersion "1.0.0"
#define AppPublisher "LinYiZhiYao"
#define AppExe "Tsursport-iOS.exe"
#define SourceDir "dist_offline\Tsursport-iOS"

[Setup]
AppId={{8F3B2C41-7D6E-4A55-9C2B-1E7A9D3F0B21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\Tsursport-iOS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=release
OutputBaseFilename=Tsursport-iOS-Setup-{#AppVersion}
SetupIconFile=tsursport.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
DisableWelcomePage=no

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=%1 安装程序
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将在你的电脑上安装 [name/ver]。%n%n这是一个免越狱的 iPhone 定位模拟工具，安装包已包含全部运行环境，无需额外安装 Python。%n%n继续请点「下一步」。
WizardSelectDir=选择安装位置
SelectDirDesc=选择 [name] 的安装目录
SelectDirLabel3=安装程序会把 [name] 安装到下面的文件夹。
SelectDirBrowseLabel=点「浏览」可以换一个目录；直接点「下一步」使用默认目录。
SelectTasksDesc=选择附加任务
SelectTasksTitle=选择附加任务
ReadyLabel1=准备就绪，即将开始安装 [name]。
ReadyLabel2a=点「安装」开始安装，或点「上一步」返回修改设置。
InstallingLabel=正在安装 [name]，请稍候…
FinishedHeadingLabel=[name] 安装完成
FinishedLabel=[name] 已经安装到你的电脑上。%n%n如需连接 iPhone，请先安装 Apple 移动设备驱动（winget install Apple.AppleMobileDeviceSupport），并在 iPhone 上信任此电脑、开启开发者模式。
FinishedLabelNoIcons=[name] 已经安装完成。
ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonInstall=安装(&I)
ButtonFinish=完成(&F)
ButtonCancel=取消(&C)
ButtonBrowse=浏览(&B)…
ButtonYes=是(&Y)
ButtonNo=否(&N)
UninstallAppFullTitle=卸载 %1

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\_internal\tsursport.ico"; WorkingDir: "{app}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\_internal\tsursport.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent
