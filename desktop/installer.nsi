; CodeMonkey installer: the window app for this user, no administrator needed.
;
; Installs the packaged Electron shell (dist\CodeMonkey-win32-x64) to
; %LOCALAPPDATA%\Programs\CodeMonkey, with Start menu and desktop shortcuts and
; an entry in Settings > Apps for uninstalling. The Python code is not copied:
; root.txt beside the exe names the CodeMonkey folder it was built from, so
; changes made there show up the next time the app starts.
;
; Build: npm run installer   (packages first; needs NSIS's makensis on PATH)
;        npm run release     (the same, with the next version number: installed copies update to it)
;
; Updating: the app runs this silently with /S /RELAUNCH /D=<its folder>: it waits for the app to
; close, replaces it, and opens it again.
; makensis /DROOT=<CodeMonkey folder> /DVERSION=<x.y.z> installer.nsi
;
; Standalone (npm run standalone, scripts/make-standalone.js): /DSTANDALONE instead of /DROOT builds
; dist\CodeMonkey-<version>-Windows.exe, which carries the code (app\) and its own Python (python\)
; from dist\standalone, to give to someone without this folder. Its sessions and settings live in
; $INSTDIR\app\agent-workspace and stay through reinstalls.

!ifdef STANDALONE
  !define ROOT "$INSTDIR\app"
!endif
!ifndef ROOT
  !error "pass /DROOT=<the CodeMonkey folder>"
!endif
!ifndef VERSION
  !define VERSION "0.1.0"
!endif
!define APP "CodeMonkey"
!define SRC "dist\CodeMonkey-win32-x64"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"

Unicode true
Name "${APP}"
!ifdef STANDALONE
  OutFile "dist\CodeMonkey-${VERSION}-Windows.exe"
!else
  OutFile "dist\CodeMonkey-Setup-${VERSION}.exe"
!endif
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "build\icon.ico"
UninstallIcon "build\icon.ico"
BrandingText "${APP} ${VERSION}"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!define MUI_ICON "build\icon.ico"
!define MUI_UNICON "build\icon.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP}"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var relaunch

Section "Install"
  ; (a running copy holds its files open; NSIS then offers Retry once it is closed)
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/RELAUNCH" $R1
  IfErrors +3 0
    StrCpy $relaunch "1"
    Sleep 3000                           ; an update: the app that started it is closing
  SetOutPath "$INSTDIR"
  RMDir /r "$INSTDIR\resources"          ; nothing left over from an older version
  File /r "${SRC}\*.*"
!ifdef STANDALONE
  RMDir /r "$INSTDIR\python"
  File /r "dist\standalone\python"
  File /r "dist\standalone\app"
!endif
  FileOpen $0 "$INSTDIR\root.txt" w
  FileWrite $0 "${ROOT}"
  FileClose $0

  CreateShortcut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\${APP}.exe" "" "$INSTDIR\${APP}.exe" 0
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\${APP}.exe" "" "$INSTDIR\${APP}.exe" 0

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP}.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Function .onInstSuccess
  StrCmp $relaunch "1" 0 +2
    Exec '"$INSTDIR\${APP}.exe"'
FunctionEnd

Section "Uninstall"
  ; only the window app: the CodeMonkey folder, its sessions and saved keys are left alone
  ; (standalone: app\ is the CodeMonkey folder, so its sessions go with it)
  Delete "$SMPROGRAMS\${APP}.lnk"
  Delete "$DESKTOP\${APP}.lnk"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP}"
SectionEnd
