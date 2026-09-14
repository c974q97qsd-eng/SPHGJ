' 视频号工具 - 无控制台启动(wscript 运行,全程不出现 CMD 黑窗口)
'
' 双击本文件即可启动软件。排障日志写在同目录 start.log。

Option Explicit

Dim sh, fso, here, pyw, Q, cmd, pwBrowsers
Dim wmi, procs, pr, envObj

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Q = Chr(34)

Call LogLine("==== 启动尝试 " & Now & " ====")

' 0) 已在运行则直接提示,不再开第二个实例(多实例会抢端口)
If AlreadyRunning() Then
  Call LogLine("检测到已在运行,放弃启动")
  MsgBox "SPHGJ 已经在运行了,无需重复启动。", 64, "视频号工具"
  WScript.Quit 0
End If

' 1) 定位解释器
pyw = FindPythonw()
Call LogLine("选定解释器: " & pyw)

If pyw = "" Then
  Call LogLine("错误: 未找到可用的 pythonw.exe")
  MsgBox "未找到可用的 pythonw.exe。" & vbCrLf & vbCrLf & _
         "请先双击 配置环境安装\一键安装.bat 完成环境安装。" & vbCrLf & _
         "排查细节见同目录 start.log。", 16, "视频号工具 - 启动失败"
  WScript.Quit 1
End If

' 2) 指向随项目自带的离线浏览器(与 启动-SPHGJ.bat 保持一致)
pwBrowsers = here & "\配置环境安装\ms-playwright"
If fso.FolderExists(pwBrowsers) Then
  Set envObj = sh.Environment("PROCESS")
  envObj.Item("PLAYWRIGHT_BROWSERS_PATH") = pwBrowsers
  Call LogLine("已设置 PLAYWRIGHT_BROWSERS_PATH=" & pwBrowsers)
End If

' 3) 启动。
'    注意!窗口样式必须传 1(正常显示),不能传 0(SW_HIDE):
'    这里启动的是 pythonw.exe,属于 GUI 子系统,本身就不会创建控制台窗口,
'    传 0 会把 pywebview 创建的桌面窗口一起隐藏 —— 表现为「后端在跑、
'    浏览器能打开页面、但就是看不到软件窗口」。
'    False = 不等待,脚本启动后立即退出。
sh.CurrentDirectory = here
cmd = Q & pyw & Q & " " & Q & here & "\main.py" & Q
Call LogLine("执行命令: " & cmd)
sh.Run cmd, 1, False

WScript.Quit 0


' 判断是否已有实例在跑:查 pythonw 进程里命令行含 main.py 的
Function AlreadyRunning()
  AlreadyRunning = False
  On Error Resume Next
  Set wmi = GetObject("winmgmts:\\.\root\cimv2")
  If Err.Number <> 0 Then
    On Error GoTo 0
    Exit Function
  End If
  Set procs = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name='pythonw.exe' AND CommandLine LIKE '%main.py%'")
  For Each pr In procs
    AlreadyRunning = True
    Exit For
  Next
  On Error GoTo 0
End Function


' 按优先级找可用的 pythonw.exe:
' 便携版(随项目,依赖齐全) > 环境变量 > 脚本同目录 > PATH > 常见安装位置
Function FindPythonw()
  Dim cand, arr, i, d, base, subF, f

  FindPythonw = ""

  ' 1) 随项目自带的便携版 —— 首选,免探测、免依赖
  cand = here & "\配置环境安装\Python314\pythonw.exe"
  If Usable(cand) Then
    FindPythonw = cand
    Exit Function
  End If

  ' 2) 环境变量显式指定
  cand = sh.ExpandEnvironmentStrings("%SPHGJ_PYTHONW%")
  If cand <> "%SPHGJ_PYTHONW%" And cand <> "" Then
    If Usable(cand) Then
      FindPythonw = cand
      Exit Function
    End If
  End If

  ' 3) 脚本同目录
  cand = here & "\pythonw.exe"
  If Usable(cand) Then
    FindPythonw = cand
    Exit Function
  End If

  ' 4) PATH 中逐个目录找
  arr = Split(sh.ExpandEnvironmentStrings("%PATH%"), ";")
  For i = 0 To UBound(arr)
    d = Trim(arr(i))
    If d <> "" Then
      If Right(d, 1) = "\" Then d = Left(d, Len(d) - 1)
      cand = d & "\pythonw.exe"
      If Usable(cand) Then
        FindPythonw = cand
        Exit Function
      End If
    End If
  Next

  ' 5) 常见安装位置
  base = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Python"
  If fso.FolderExists(base) Then
    For Each subF In fso.GetFolder(base).SubFolders
      cand = subF.Path & "\pythonw.exe"
      If Usable(cand) Then
        FindPythonw = cand
        Exit Function
      End If
      For Each f In subF.SubFolders
        cand = f.Path & "\pythonw.exe"
        If Usable(cand) Then
          FindPythonw = cand
          Exit Function
        End If
      Next
    Next
  End If
End Function


' 依赖校验:pythonw 无控制台,只能靠退出码判断(0 = 依赖齐全)
' 注意:rc 必须局部变量并在 Run 前重置,否则上次的残留值会误判
Function Usable(exe)
  Dim rc
  Usable = False
  If Not fso.FileExists(exe) Then Exit Function
  rc = -1
  On Error Resume Next
  rc = sh.Run(Q & exe & Q & " -c ""import fastapi, webview, playwright""", 0, True)
  On Error GoTo 0
  If rc = 0 Then Usable = True
End Function


' 追加一行到 start.log
Sub LogLine(msg)
  On Error Resume Next
  Dim f
  Set f = fso.OpenTextFile(here & "\start.log", 8, True)
  f.WriteLine msg
  f.Close
  On Error GoTo 0
End Sub
