' Bruker auto measurement launcher (WebView2, no console window. Style 1 so the window comes to the front)
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
dir = fs.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "pythonw """ & dir & "\src\web_app.py""", 1, False
