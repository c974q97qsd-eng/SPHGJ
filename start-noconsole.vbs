' 视频号工具 - 无控制台启动(wscript 运行,全程不出现 CMD 黑窗口)
'
' 双击本文件即可启动软件。日志请在软件界面底部「运行日志」板块查看;
' 崩溃线索写在同目录 run.log。
'
' 若你的 Python 不在 PATH 或存在多个版本,可设置环境变量 SPHGJ_PYTHONW
' 指向要使用的 pythonw.exe 完整路径,本脚本会优先采用。

Option Explicit

Dim sh, fso, here, pyw, Q, rc, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Q = Chr(34)

pyw = FindPythonw()

If pyw = "" Then
  MsgBox "未找到可用的 pythonw.exe。" & vbCrLf & vbCrLf & _
         "请先双击 setup.bat 完成安装;若 Python 不在 PATH," & vbCrLf & _
         "请设置环境变量 SPHGJ_PYTHONW 指向 pythonw.exe 的完整路径。", _
         16, "视频号工具 - 启动失败"
  WScript.Quit 1
End If

sh.CurrentDirectory = here
' 参数:命令, 0=隐藏窗口, False=不等待(启动后立即结束本脚本)
cmd = Q & pyw & Q & " " & Q & here & "\main.py" & Q
sh.Run cmd, 0, False


' ---------------------------------------------------------------------------
' 按优先级找 pythonw.exe,并对每个候选做依赖校验(避免选中没装依赖的解释器
' 导致无控制台下静默失败 —— 用户什么都看不到)。
' ---------------------------------------------------------------------------
Function FindPythonw()
  Dim cand, i, arr, d, base, subF, f

  FindPythonw = ""

  ' 1) 环境变量显式指定
  cand = sh.ExpandEnvironmentStrings("%SPHGJ_PYTHONW%")
  If cand <> "%SPHGJ_PYTHONW%" And cand <> "" Then
    If Usable(cand) Then FindPythonw = cand : Exit Function
  End If

  ' 2) 本脚本同目录(解压即用场景)
  cand = here & "\pythonw.exe"
  If fso.FileExists(cand) And Usable(cand) Then
    FindPythonw = cand : Exit Function
  End If

  ' 3) PATH 中逐个目录找
  arr = Split(sh.ExpandEnvironmentStrings("%PATH%"), ";")
  For i = 0 To UBound(arr)
    d = Trim(arr(i))
    If d <> "" Then
      If Right(d, 1) = "\" Then d = Left(d, Len(d) - 1)
      cand = d & "\pythonw.exe"
      If fso.FileExists(cand) And Usable(cand) Then
        FindPythonw = cand : Exit Function
      End If
    End If
  Next

  ' 4) %LOCALAPPDATA%\Python\<发行版> 下的常见位置(PyManager / 官方安装器)
  base = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Python"
  If fso.FolderExists(base) Then
    For Each subF In fso.GetFolder(base).SubFolders
      cand = subF.Path & "\pythonw.exe"
      If fso.FileExists(cand) And Usable(cand) Then
        FindPythonw = cand : Exit Function
      End If
      For Each f In subF.SubFolders
        cand = f.Path & "\pythonw.exe"
        If fso.FileExists(cand) And Usable(cand) Then
          FindPythonw = cand : Exit Function
        End If
      Next
    Next
  End If
End Function


' 依赖校验:pythonw 无控制台,靠进程退出码判断(0=依赖齐全)
Function Usable(exe)
  Usable = False
  If Not fso.FileExists(exe) Then Exit Function
  On Error Resume Next
  ' 0=隐藏窗口, True=等待结束以取退出码
  rc = sh.Run(Q & exe & Q & " -c ""import fastapi, webview, playwright""", 0, True)
  On Error GoTo 0
  If rc = 0 Then Usable = True
End Function
