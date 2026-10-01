Option Explicit

Dim shell, files, environment, root, command, argument, hidden, windowStyle, result
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & "bin\win\install.ps1" & Chr(34)
hidden = False
For Each argument In WScript.Arguments
    Select Case argument
        Case "--hidden"
            hidden = True
            command = command & " -Hidden"
        Case "--no-launch"
            command = command & " -NoLaunch"
        Case Else
            MsgBox "Unknown setup option: " & argument, vbExclamation, "ADBeam me up setup"
            WScript.Quit 2
    End Select
Next
Set environment = shell.Environment("PROCESS")
environment("PSModulePath") = ""
windowStyle = 1
If hidden Then windowStyle = 0
result = shell.Run(command, windowStyle, True)
If result <> 0 Then
    MsgBox "Setup did not complete. Run it again from this folder. Details appear in the setup window or bin\win\logs\setup.log.", vbExclamation, "ADBeam me up setup"
End If
WScript.Quit result
