"""Register the native host for Chrome (current user only).

python install_host.py <extension-id>
python install_host.py --remove
"""
import json
import os
import sys
import winreg

NAME = "com.udm.host"
KEY = r"Software\Google\Chrome\NativeMessagingHosts" + "\\" + NAME
here = os.path.dirname(os.path.abspath(__file__))
manifest_path = os.path.join(here, NAME + ".json")

if len(sys.argv) != 2:
    sys.exit(__doc__)

if sys.argv[1] == "--remove":
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY)
    print("removed HKCU\\" + KEY)
    sys.exit()

# 1. allow only our extension to start the host
with open(manifest_path) as f:
    manifest = json.load(f)
manifest["allowed_origins"] = ["chrome-extension://%s/" % sys.argv[1]]
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)

# 2. Chrome needs an executable, so wrap host.py in a .bat that uses this python
with open(os.path.join(here, "host.bat"), "w") as f:
    f.write('@echo off\n"%s" -u "%s"\n' % (sys.executable, os.path.join(here, "host.py")))

# 3. point Chrome at the manifest
with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, manifest_path)

print("registered HKCU\\%s -> %s" % (KEY, manifest_path))
