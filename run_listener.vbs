' Launch the birdwatcher microphone listener hidden (no console window) and
' append its output to listener.log next to this script. Portable: it uses its
' own folder, so there are no hardcoded paths to edit.
'
' Use it two ways:
'   - Double-click to start the listener in the background right now.
'   - Point a Task Scheduler "At log on" task at it for autostart (see README).
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = scriptDir

' 0 = hidden window, False = don't wait for it to finish.
sh.Run "cmd /c "".venv\Scripts\python.exe"" -m capture.listen >> listener.log 2>&1", 0, False
