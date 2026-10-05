import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
BOT_PATH = HERE / "bot.py"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
configuration["enabled"] = True
CONFIG_PATH.write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")

pythonw = sys.executable.replace("python.exe", "pythonw.exe")
if not os.path.isfile(pythonw):
    pythonw = sys.executable
subprocess.Popen([pythonw, str(BOT_PATH)], cwd=str(HERE), creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("TG-bridge ВКЛЮЧЁН: enabled=true, бот запущен в фоне.")
input("Готово. Enter для выхода…")
