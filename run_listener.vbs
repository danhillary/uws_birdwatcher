' Launch the birdwatcher listener hidden (no console window), supervised by
' capture/watchdog.py, which restarts it if it crashes, hangs or loses the mic
' and sends an alert only if that doesn't fix it. The listener's output goes to
' listener.log and the watchdog's restart history to watchdog.log, both next
' to this script. Portable: it uses its own folder, so there are no hardcoded
' paths to edit.
'
' Use it two ways:
'   - Double-click to start the listener in the background right now.
'   - Point a Task Scheduler "At log on" task at it for autostart (see README).
' Only one watchdog runs at a time, so launching this again while it's already
' running does nothing (safe to repeat from a scheduled task).
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = scriptDir

' 0 = hidden window, False = don't wait for it to finish.
' -u = unbuffered stdout/stderr, so watchdog.log updates live instead of in blocks.
sh.Run "cmd /c "".venv\Scripts\python.exe"" -u -m capture.watchdog >> watchdog.log 2>&1", 0, False
