' hidden.vbs — run a command with NO window at all (no console, no taskbar entry)
' Usage: wscript.exe hidden.vbs <program> [arg1 arg2 ...]
' Every argument is quoted and joined, then executed with window style 0.
' Example: wscript.exe hidden.vbs cmd /c "C:\Users\me\llmnpu\scripts\serve_gpu.bat"
Set args = WScript.Arguments
If args.Count < 1 Then WScript.Quit 1
cmd = """" & args(0) & """"
For i = 1 To args.Count - 1
    cmd = cmd & " " & """" & args(i) & """"
Next
CreateObject("WScript.Shell").Run cmd, 0, False