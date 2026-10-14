"""Run the downloader in the background, with no console window, every time you log in.

python autostart.py on       -> start at login, and start it now
python autostart.py off      -> stop starting at login, and stop it now
python autostart.py status
"""
import os
import socket
import subprocess
import sys
import winreg

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "udm"
here = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(here, "downloader", "downloader.py")
pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")  # python without a console
action = sys.argv[1] if len(sys.argv) == 2 else ""

with socket.socket() as s:
    s.settimeout(1)
    running = s.connect_ex(("127.0.0.1", 8765)) == 0

if action == "on":
    if not os.path.exists(pythonw):
        sys.exit("pythonw.exe not found next to %s" % sys.executable)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN) as key:
        winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, '"%s" "%s"' % (pythonw, script))
    if not running:
        subprocess.Popen([pythonw, script], creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
    print("autostart on. downloader %s." % ("was already running" if running else "started"))

elif action == "off":
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, NAME)
    except FileNotFoundError:
        pass
    # stop only the background copy (pythonw), not one you started in a console
    find = ("Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%s') } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % script)
    subprocess.run(["powershell", "-NoProfile", "-Command", find], creationflags=subprocess.CREATE_NO_WINDOW)
    print("autostart off. background downloader stopped.")

elif action == "status":
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN) as key:
            print("autostart: on  (%s)" % winreg.QueryValueEx(key, NAME)[0])
    except FileNotFoundError:
        print("autostart: off")
    print("downloader: %s" % ("running on port 8765" if running else "not running"))

else:
    sys.exit(__doc__)
