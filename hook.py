import json
import os
import socket
import sys
import time
import urllib.request

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
SPOOL_PATH = os.path.join(SCRIPT_DIRECTORY, "spool.jsonl")
CONFIG_PATH = os.path.join(SCRIPT_DIRECTORY, "config.json")

FORWARDED_EVENTS = ("Stop", "Notification", "SubagentStop", "PermissionRequest")


def read_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as config_file:
            return json.load(config_file)
    except (OSError, ValueError):
        return {}


def bot_is_listening(port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def forward_event(payload, port, secret):
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Bridge-Secret"] = secret
    request = urllib.request.Request(f"http://127.0.0.1:{port}/event", data=data, headers=headers)
    try:
        urllib.request.urlopen(request, timeout=1.0).close()
    except (OSError, ValueError):
        try:
            with open(SPOOL_PATH, "a", encoding="utf-8") as spool_file:
                spool_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass


def main():
    # Если хук запущен внутри headless-вызова самого бота (пересказ/ответ в сессию) — ничего не делаем,
    # иначе бот уведомлял бы сам себя и зацикливался
    if os.environ.get("TG_BRIDGE_INTERNAL"):
        return
    try:
        raw_input_text = sys.stdin.read()
    except (OSError, ValueError):
        raw_input_text = ""
    try:
        data = json.loads(raw_input_text) if raw_input_text.strip() else {}
    except ValueError:
        data = {}

    event_name = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")
    config = read_config()
    if not config.get("enabled", True):
        return
    port = config.get("listen_port", 8787)

    if not bot_is_listening(port):
        # Ручной режим: бот сам НЕ запускается. Если он сейчас не работает — событие игнорируем (ничего не копим, ресурсы не едим)
        return

    if event_name in FORWARDED_EVENTS:
        payload = {
            "event": event_name,
            "session_id": data.get("session_id", ""),
            "cwd": data.get("cwd", ""),
            "transcript_path": data.get("transcript_path", ""),
            "message": data.get("message", ""),
            "tool_name": data.get("tool_name", ""),
            "tool_input": data.get("tool_input", {}),
            "ts": int(time.time() * 1000),
        }
        forward_event(payload, port, config.get("event_secret"))


if __name__ == "__main__":
    main()
