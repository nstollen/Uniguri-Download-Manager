; UDM-Setup.exe. build.bat builds the two exes with PyInstaller first, then runs this with Inno Setup 6:
;   ISCC /DAppVersion=2.1 installer\udm.iss   ->   dist\UDM-Setup.exe
;
; Installs for the current user only (no admin) to %LOCALAPPDATA%\UDM:
;   downloader\  udm-downloader.exe (+ _internal\), ffmpeg.exe, ffprobe.exe if present, udm.ico
;   host\        udm-host.exe (+ _internal\), com.udm.host.json
;   extension\   the Chrome extension, loaded with "Load unpacked"
;   README.md, INSTALL.txt, ffmpeg-LICENSE.txt, steps.html (opened at Finish), uninstall.py (for the uninstaller)
; History, settings and downloads stay in %USERPROFILE%\Downloads\udm, which Setup never touches, so
; installing a newer UDM-Setup.exe over an old install keeps them and only replaces the program files.

#ifndef AppVersion
  #define AppVersion "0"
#endif
#define Dist "..\build\dist"
; the title of the downloader's hidden tray window (downloader.py, main)
#define Tray "UDM tray 8765"

[Setup]
AppId={{AF4819F2-7DFC-40B4-B294-2014E3988EDE}
AppName=UDM (Uniguri Download Manager)
AppVersion={#AppVersion}
AppVerName=UDM {#AppVersion}
DefaultDirName={localappdata}\UDM
DisableDirPage=yes
DefaultGroupName=UDM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=UDM-Setup
SetupIconFile=..\downloader\udm.ico
UninstallDisplayIcon={app}\downloader\udm-downloader.exe
UninstallDisplayName=UDM (Uniguri Download Manager)
WizardStyle=modern dynamic
Compression=lzma2/max
SolidCompression=yes
; Setup stops the downloader itself (StopDownloader below), more gently than the Restart Manager would
CloseApplications=no

[InstallDelete]
; an update: the old program files go first, so nothing stale is left next to the new ones (data is elsewhere)
Type: filesandordirs; Name: "{app}\downloader\_internal"
Type: filesandordirs; Name: "{app}\host\_internal"
Type: filesandordirs; Name: "{app}\extension"

[Files]
Source: "{#Dist}\udm-downloader\*"; DestDir: "{app}\downloader"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\downloader\ffmpeg.exe"; DestDir: "{app}\downloader"; Flags: ignoreversion
Source: "..\downloader\ffprobe.exe"; DestDir: "{app}\downloader"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\downloader\udm.ico"; DestDir: "{app}\downloader"; Flags: ignoreversion
Source: "{#Dist}\udm-host\*"; DestDir: "{app}\host"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "com.udm.host.json"; DestDir: "{app}\host"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\INSTALL.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\downloader\ffmpeg-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "steps.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "uninstall.py"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
; the native host, for Chrome, Brave and Edge (each reads its own key)
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.udm.host"; ValueType: string; ValueName: ""; ValueData: "{app}\host\com.udm.host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.udm.host"; ValueType: string; ValueName: ""; ValueData: "{app}\host\com.udm.host.json"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Edge\NativeMessagingHosts\com.udm.host"; ValueType: string; ValueName: ""; ValueData: "{app}\host\com.udm.host.json"; Flags: uninsdeletekey
; autostart: the downloader starts at login (the same value autostart.py sets)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "udm"; ValueData: """{app}\downloader\udm-downloader.exe"""; Flags: uninsdeletevalue

[Icons]
Name: "{group}\UDM folder"; Filename: "{app}"
Name: "{group}\UDM readme"; Filename: "{win}\notepad.exe"; Parameters: """{app}\README.md"""
Name: "{group}\Start UDM downloader"; Filename: "{app}\downloader\udm-downloader.exe"
Name: "{group}\Uninstall UDM"; Filename: "{uninstallexe}"

[Run]
; start the downloader (tray icon near the clock); at Finish open the extension folder for "Load unpacked"
; and the 3 steps in the browser, which both stay open while you follow them (not on an update)
Filename: "{app}\downloader\udm-downloader.exe"; Flags: nowait
Filename: "{win}\explorer.exe"; Parameters: """{app}\extension"""; Description: "Open the extension folder"; Flags: postinstall nowait skipifsilent; Check: not IsUpdate
Filename: "{app}\steps.html"; Description: "Show the 3 steps to add UDM to your browser"; Flags: postinstall shellexec nowait skipifsilent; Check: not IsUpdate

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  Update: Boolean;  { an older UDM was installed: the extension only needs a reload }

function DownloaderRunning: Boolean;  { any udm-downloader.exe, from this install or not }
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq udm-downloader.exe" /NH | find /I "udm-downloader.exe"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure WaitForDownloaders(Tenths: Integer);
var
  I: Integer;
begin
  for I := 1 to Tenths do begin
    if not DownloaderRunning then Exit;
    Sleep(100);
  end;
end;

procedure StopDownloader;
var
  I, Code: Integer;
  Wnd: HWND;
begin
  { 1. the tray icon's Quit, posted to the hidden window of every copy that runs (an older UDM could run more
    than one): each saves its history, takes its icon away and ends. 10 seconds for all of them together }
  for I := 1 to 100 do begin
    Wnd := FindWindowByWindowName('{#Tray}');
    if Wnd = 0 then Break;
    PostMessage(Wnd, $0111 { WM_COMMAND }, 4, 0);
    Sleep(100);
  end;
  { 2. copies without that window (still starting, another port): taskkill without /F closes their windows,
    which is the same clean Quit (and an older UDM at least takes its icon away) }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM udm-downloader.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  WaitForDownloaders(50);
  { 3. still running (stuck): stop it; also a host the browser has running right now. Then wait until every
    copy is really gone, so its files can be replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM udm-downloader.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM udm-host.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  WaitForDownloaders(100);
  Sleep(500);  { Windows lets go of the files }
end;

function InitializeSetup: Boolean;
begin
  Update := RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{AF4819F2-7DFC-40B4-B294-2014E3988EDE}_is1');
  Result := True;
end;

function IsUpdate: Boolean;  { for Check: in [Run] }
begin
  Result := Update;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopDownloader;  { its files can be replaced only when it is not running }
  Result := '';
end;


procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then Exit;
  if Update then
    WizardForm.FinishedLabel.Caption := 'UDM is updated and its downloader is running again. Your history and settings are kept.' + #13#10#13#10 +
      'Last step: open chrome://extensions (or brave://extensions, edge://extensions) and press the reload arrow on Uniguri Download Manager.' + #13#10#13#10 +
      'UDM is an unofficial fan project, not affiliated with or endorsed by ZUTOMAYO.'
  else
    WizardForm.FinishedLabel.Caption := 'UDM is installed and its downloader is running: the hedgehog icon near the clock.' + #13#10#13#10 +
      'Last step: add the extension to your browser. Click Finish to open the extension folder and a page with the 3 steps.' + #13#10#13#10 +
      'UDM is an unofficial fan project, not affiliated with or endorsed by ZUTOMAYO.';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data, What: String;
  Code: Integer;
begin
  if CurUninstallStep <> usUninstall then Exit;
  StopDownloader;
  { what UDM saved and downloaded: kept unless you say otherwise (a silent uninstall keeps everything) }
  Data := ExpandConstant('{%USERPROFILE}\Downloads\udm');
  What := '';
  if FileExists(Data + '\history.json') and (SuppressibleMsgBox('Also delete the files UDM downloaded?' + #13#10#13#10 +
      'Yes deletes every finished download in the UDM list (and its subtitles), and the part files of unfinished ones. ' +
      'No keeps them.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
    What := What + ' files';
  if DirExists(Data) and (SuppressibleMsgBox('Delete your UDM history, stats and settings?' + #13#10#13#10 +
      'They are in ' + Data + ' (history.json, settings.json, stats.json, thumbs, udm.log). ' +
      'No keeps them, and a new install of UDM picks them up again.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
    What := What + ' data';
  if What <> '' then
    Exec(ExpandConstant('{app}\downloader\udm-downloader.exe'),
      '-c "exec(open(r''' + ExpandConstant('{app}\uninstall.py') + ''', encoding=''utf-8'').read())" "' + Data + '"' + What,
      '', SW_HIDE, ewWaitUntilTerminated, Code);
end;
