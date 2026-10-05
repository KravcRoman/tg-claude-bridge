import json
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
configuration["enabled"] = False
CONFIG_PATH.write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")

kill_command = ("Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | "
                "Where-Object { $_.CommandLine -like '*tg-claude-bridge*bot.py*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
subprocess.run(["powershell.exe", "-NoProfile", "-Command", kill_command], capture_output=True)

print("TG-bridge ВЫКЛЮЧЕН: enabled=false, процесс остановлен, авто-запуск отключён.")
input("Готово. Enter для выхода…")

