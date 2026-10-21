; Incipit for Windows: installs the packaged program and asks the questions a client answers once:
; where to keep the data (an existing folder or a new one), the site's name on the network and its
; port, whether to start at login, and whether the router should open the port to the internet. It
; also lets Incipit through the Windows firewall (one administrator prompt). The data folder is made
; with `Incipit.exe init-folder`, lives outside {app}, and is never removed by the uninstaller; the
; shortcuts find it through the location file init-folder records, so a later move from Settings
; keeps them working. Build with Inno Setup 6: ISCC.exe installer\incipit.iss (after
; scripts\build_app.py).
;
; Unattended installs (an IT department's, or a test) answer the questions on the command line:
;   Incipit-x.y.z-setup.exe /VERYSILENT /DIR="C:\Incipit" /DATAFOLDER="D:\IncipitData"
;     /SITENAME=our-archive /PORT=8010 /NOLOGIN /NOFORWARD /NOTUNNEL /NOFIREWALL
;
; An update from the program itself runs  Incipit-x.y.z-setup.exe /SILENT /UPDATE : no questions,
; the data folder and every setting kept, no firewall prompt, and the tray started again quietly.

#define AppName "Incipit"
#define AppVersion Trim(FileRead(FileOpen("..\VERSION")))
; The publisher shown in Apps & features; INCIPIT_PUBLISHER overrides it for another build.
#define AppPublisher GetEnv("INCIPIT_PUBLISHER") != "" ? GetEnv("INCIPIT_PUBLISHER") : "Thomas Edison Papers"
#define AppExe "Incipit.exe"

[Setup]
AppId={{3BBE8C29-A34A-45D5-B675-30B6F9FA7A96}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupIconFile=..\build\icons\incipit.ico
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=Incipit-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
; The tray and the site run from {app}; an update closes them rather than asking someone to.
CloseApplications=force
RestartApplications=no

[Files]
Source: "..\dist\Incipit\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "tray"; IconFilename: "{app}\incipit.ico"
Name: "{group}\{#AppName} data folder"; Filename: "{code:DataFolder}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "tray"; IconFilename: "{app}\incipit.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
; The data folder, its file and its empty database; then the tray, which starts the site and opens the wizard.
Filename: "{app}\{#AppExe}"; Parameters: "init-folder ""{code:DataFolder}"" --port {code:Port} --site-name {code:SiteName}{code:StartAtLoginFlag}{code:ForwardFlag}{code:TunnelFlag}"; Flags: runhidden waituntilterminated; Check: not IsUpdate
; Lets the program through the firewall on every network profile (Windows calls a new network Public), replacing an earlier rule.
Filename: "{sys}\cmd.exe"; Parameters: "/c netsh advfirewall firewall delete rule name=""{#AppName}"" >nul & netsh advfirewall firewall add rule name=""{#AppName}"" dir=in action=allow program=""{app}\{#AppExe}"" enable=yes profile=any"; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; Check: WantFirewall
Filename: "{app}\{#AppExe}"; Parameters: "tray"; Description: "Start {#AppName} and open the setup page"; Flags: postinstall nowait skipifsilent
; After an update, the site comes back by itself, without opening a browser at the host.
Filename: "{app}\{#AppExe}"; Parameters: "tray --quiet"; Flags: nowait; Check: IsUpdate

[UninstallRun]
Filename: "schtasks"; Parameters: "/Delete /TN Incipit /F"; Flags: runhidden; RunOnceId: "RemoveLogonTask"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#AppName}"""; Verb: "runas"; Flags: shellexec runhidden waituntilterminated; RunOnceId: "RemoveFirewallRule"; Check: WantFirewall

[Code]
var
  FolderPage: TInputDirWizardPage;
  AddressPage: TInputQueryWizardPage;
  LoginPage: TInputOptionWizardPage;

// The name as the site will announce it: lower case, spaces and other characters as hyphens.
function CleanName(Value: String): String;
var
  I: Integer;
  C: Char;
begin
  Result := '';
  Value := Lowercase(Trim(Value));
  for I := 1 to Length(Value) do begin
    C := Value[I];
    if ((C >= 'a') and (C <= 'z')) or ((C >= '0') and (C <= '9')) then
      Result := Result + C
    else if (Length(Result) > 0) and (Result[Length(Result)] <> '-') then
      Result := Result + '-';
  end;
  while (Length(Result) > 0) and (Result[Length(Result)] = '-') do
    Delete(Result, Length(Result), 1);
end;

function IsUpdate: Boolean;
begin
  Result := Pos('/UPDATE', UpperCase(GetCmdTail)) > 0;
end;

// The data folder this computer already uses, from the location file init-folder writes, or ''.
function RememberedFolder: String;
var
  Text: AnsiString;
  Line: String;
  Start: Integer;
begin
  Result := '';
  if not LoadStringFromFile(ExpandConstant('{localappdata}\Incipit\location.toml'), Text) then Exit;
  Line := String(Text);
  Start := Pos('data_folder = "', Line);
  if Start = 0 then Exit;
  Line := Copy(Line, Start + Length('data_folder = "'), Length(Line));
  Start := Pos('"', Line);
  if Start = 0 then Exit;
  Result := Copy(Line, 1, Start - 1);
  // TOML writes each backslash of a Windows path twice.
  StringChangeEx(Result, '\\', '\', True);
  if not DirExists(Result) then Result := '';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // An update asks nothing: the folder, the address and the choices are already made.
  Result := IsUpdate and ((PageID = FolderPage.ID) or (PageID = AddressPage.ID) or (PageID = LoginPage.ID)
                          or (PageID = wpSelectDir) or (PageID = wpSelectProgramGroup) or (PageID = wpSelectTasks));
end;

function IsEmptyDir(Path: String): Boolean;
var
  Found: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Path) + '*', Found) then begin
    try
      repeat
        if (Found.Name <> '.') and (Found.Name <> '..') then begin
          Result := False;
          Break;
        end;
      until not FindNext(Found);
    finally
      FindClose(Found);
    end;
  end;
end;

procedure InitializeWizard;
begin
  FolderPage := CreateInputDirPage(wpSelectDir, 'Data folder',
    'Where should Incipit keep its documents and database?',
    'Everything the site stores goes in this folder: scans, transcriptions, exports and backups. ' +
    'Pick a folder with room for your scans, or use Browse and Make New Folder to create one. It can be ' +
    'moved later in Settings > Access, and it is never removed when Incipit is uninstalled.', False, 'Incipit Data');
  FolderPage.Add('');
  // Over an existing install, the folder already in use, so clicking Next keeps the data where it is.
  if RememberedFolder <> '' then
    FolderPage.Values[0] := ExpandConstant('{param:DATAFOLDER|' + RememberedFolder + '}')
  else
    FolderPage.Values[0] := ExpandConstant('{param:DATAFOLDER|{userdocs}\Incipit}');

  AddressPage := CreateInputQueryPage(FolderPage.ID, 'The site''s address',
    'What should your team type to reach the site?',
    'Choose a short name; on your network the site will answer at http://NAME.local:PORT/ ' +
    '(for example http://our-archive.local:8010/), so nobody needs this computer''s IP address. ' +
    'Keep the port at 8010 unless something else on this computer already uses it.');
  AddressPage.Add('Site name (letters, digits and hyphens):', False);
  AddressPage.Add('Port:', False);
  AddressPage.Values[0] := ExpandConstant('{param:SITENAME|incipit}');
  AddressPage.Values[1] := ExpandConstant('{param:PORT|8010}');

  LoginPage := CreateInputOptionPage(AddressPage.ID, 'Starting and reaching the site',
    'How should the site start, and who can reach it?',
    'Your team can only reach the site while it is running on this computer. Opening the port asks your ' +
    'router (by UPnP) to let people outside your network reach the site''s encrypted (HTTPS) address, ' +
    'never the plain one; Settings > Access shows the address to share and a note for the team about ' +
    'the browser''s one-time warning, or what to change if the router says no.', False, False);
  LoginPage.Add('Start Incipit when I log in (recommended)');
  LoginPage.Add('Open the port on the router so the team can reach the site from anywhere');
  LoginPage.Add('If the router cannot open it, use a free Cloudflare tunnel instead (no account needed)');
  LoginPage.Add('Let Incipit through the Windows firewall (asks for administrator approval once)');
  // On unless the command line says otherwise.
  LoginPage.Values[0] := Pos('/NOLOGIN', UpperCase(GetCmdTail)) = 0;
  LoginPage.Values[1] := Pos('/NOFORWARD', UpperCase(GetCmdTail)) = 0;
  LoginPage.Values[2] := Pos('/NOTUNNEL', UpperCase(GetCmdTail)) = 0;
  LoginPage.Values[3] := Pos('/NOFIREWALL', UpperCase(GetCmdTail)) = 0;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
  Name: String;
begin
  Result := True;
  // A folder already holding other files is used only on purpose; one holding a site is reused as it is.
  if (CurPageID = FolderPage.ID) and DirExists(FolderPage.Values[0])
      and not FileExists(AddBackslash(FolderPage.Values[0]) + 'incipit.toml')
      and not IsEmptyDir(FolderPage.Values[0]) then begin
    Result := MsgBox('That folder already holds other files. Keep Incipit''s data there alongside them?' + #13#10 +
      'Choose No to pick or create an empty folder.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
    Exit;
  end;
  if CurPageID = AddressPage.ID then begin
    Name := CleanName(AddressPage.Values[0]);
    if (Length(Name) < 1) or (Length(Name) > 63) then begin
      MsgBox('The site name needs 1 to 63 letters, digits or hyphens, such as our-archive.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    AddressPage.Values[0] := Name;
    Port := StrToIntDef(Trim(AddressPage.Values[1]), 0);
    if (Port < 1) or (Port > 65535) then begin
      MsgBox('The port is a whole number from 1 to 65535.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function DataFolder(Param: String): String;
begin
  Result := RemoveBackslashUnlessRoot(FolderPage.Values[0]);
end;

function SiteName(Param: String): String;
begin
  Result := CleanName(AddressPage.Values[0]);
  if Result = '' then Result := 'incipit';
end;

function Port(Param: String): String;
begin
  Result := Trim(AddressPage.Values[1]);
end;

function ForwardFlag(Param: String): String;
begin
  if LoginPage.Values[1] then Result := ' --forward-port' else Result := '';
end;

function TunnelFlag(Param: String): String;
begin
  if LoginPage.Values[2] then Result := ' --tunnel auto' else Result := ' --tunnel off';
end;

function WantFirewall: Boolean;
begin
  // An update keeps the rule the first install made (or chose not to), and asks nothing.
  Result := LoginPage.Values[3] and not IsUpdate;
end;

function StartAtLoginFlag(Param: String): String;
begin
  if LoginPage.Values[0] then Result := ' --start-at-login' else Result := '';
end;
