' Open Foo AI in its own window, with no console.
Set shell = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "npm start", 0, False
