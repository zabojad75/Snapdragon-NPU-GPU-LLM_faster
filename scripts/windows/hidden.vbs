' hidden.vbs — run a command with NO window at all (no console, no taskbar entry)
' Usage: wscript.exe hidden.vbs <program> [arg1 arg2 ...]
' Arguments are joined and executed with window style 0. Only arguments that
' contain spaces (or are empty) get quotes: cmd /c strips the first and last
' quote of its command line when there are more than two, so quoting every
' argument turned `cmd /c "x.bat" "30b" "16384"` into a broken command.
' Example: wscript.exe hidden.vbs cmd /c "C:\Users\me\llmnpu\scripts\windows\serve_gpu.bat" 30b 16384
Function Q(s)
    If s = "" Or InStr(s, " ") > 0 Then
        Q = """" & s & """"
    Else
        Q = s
    End If
End Function
Set args = WScript.Arguments
If args.Count < 1 Then WScript.Quit 1
cmd = Q(args(0))
For i = 1 To args.Count - 1
    cmd = cmd & " " & Q(args(i))
Next
CreateObject("WScript.Shell").Run cmd, 0, False
