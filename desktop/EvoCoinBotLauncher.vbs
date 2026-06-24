Option Explicit

Dim shell, fso, scriptDir, ps1Path, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1Path = fso.BuildPath(scriptDir, "EvoCoinControl.ps1")
shell.CurrentDirectory = fso.GetParentFolderName(scriptDir)

If Not fso.FileExists(ps1Path) Then
    shell.Popup "Launcher nao encontrado: " & ps1Path, 10, "Evo Coin Bot", 16
    WScript.Quit 1
End If

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1Path & """"
shell.Run command, 0, False
