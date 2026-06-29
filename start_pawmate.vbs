Option Explicit

Dim shell, fso, baseDir, env, pythonCmd, appCommand
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = baseDir

Set env = shell.Environment("PROCESS")
env("PYTHONUTF8") = "1"
env("PYTHONPATH") = baseDir

pythonCmd = ResolvePythonCommand(baseDir)
If Len(pythonCmd) = 0 Then
    MsgBox "No Python interpreter with PawMate GUI dependencies was found." & vbCrLf & _
           "Install PySide6 for the active Python, or create .venv311 / .venv in this folder.", _
           vbCritical, "PawMate startup failed"
    WScript.Quit 1
End If

If env("PAWMATE_VBS_DRY_RUN") = "1" Then
    WScript.Echo pythonCmd
    WScript.Quit 0
End If

appCommand = pythonCmd & " -m pawmate"
shell.Run appCommand, 0, False

Function ResolvePythonCommand(root)
    Dim pyExe, pywExe

    pyExe = root & "\.venv311\Scripts\python.exe"
    pywExe = root & "\.venv311\Scripts\pythonw.exe"
    If fso.FileExists(pyExe) And CanRunPawMate(Quote(pyExe)) Then
        If fso.FileExists(pywExe) Then
            ResolvePythonCommand = Quote(pywExe)
        Else
            ResolvePythonCommand = Quote(pyExe)
        End If
        Exit Function
    End If

    pyExe = root & "\.venv\Scripts\python.exe"
    pywExe = root & "\.venv\Scripts\pythonw.exe"
    If fso.FileExists(pyExe) And CanRunPawMate(Quote(pyExe)) Then
        If fso.FileExists(pywExe) Then
            ResolvePythonCommand = Quote(pywExe)
        Else
            ResolvePythonCommand = Quote(pyExe)
        End If
        Exit Function
    End If

    If CanRunPawMate("python") Then
        If CommandExists("pythonw") Then
            ResolvePythonCommand = "pythonw"
        Else
            ResolvePythonCommand = "python"
        End If
        Exit Function
    End If

    If CanRunPawMate("py -3.14") Then
        ResolvePythonCommand = "pyw -3.14"
        Exit Function
    End If

    If CanRunPawMate("py -3.13") Then
        ResolvePythonCommand = "pyw -3.13"
        Exit Function
    End If

    If CanRunPawMate("py -3.11") Then
        ResolvePythonCommand = "pyw -3.11"
        Exit Function
    End If

    If CanRunPawMate("py") Then
        ResolvePythonCommand = "pyw"
        Exit Function
    End If

    ResolvePythonCommand = ""
End Function

Function CanRunPawMate(cmd)
    Dim rc
    On Error Resume Next
    rc = shell.Run(cmd & " -B -c ""import pawmate.main""", 0, True)
    If Err.Number <> 0 Then
        Err.Clear
        CanRunPawMate = False
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0
    CanRunPawMate = (rc = 0)
End Function

Function CommandExists(cmd)
    Dim rc
    On Error Resume Next
    rc = shell.Run(cmd & " -B -c ""import sys""", 0, True)
    If Err.Number <> 0 Then
        Err.Clear
        CommandExists = False
        On Error GoTo 0
        Exit Function
    End If
    On Error GoTo 0
    CommandExists = (rc = 0)
End Function

Function Quote(value)
    Quote = """" & value & """"
End Function
