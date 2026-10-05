import datetime
import glob
import html
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIRECTORY, "config.json")
STATE_PATH = os.path.join(SCRIPT_DIRECTORY, "state.json")
SPOOL_PATH = os.path.join(SCRIPT_DIRECTORY, "spool.jsonl")
LOG_PATH = os.path.join(SCRIPT_DIRECTORY, "bot.log")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TELEGRAM_MESSAGE_LIMIT = 3900
SESSION_VIEW_MESSAGE_COUNT = 6
STATUS_DIGEST_SESSION_COUNT = 5
INBOUND_TIMEOUT_SECONDS = 600
DESKTOP_FAILURE_TOLERANCE = 6

SESSION_ID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
INTERNAL_SUMMARY_MARKER = "[tgbridge-summary]"

running = True
event_queue = queue.Queue()
state_lock = threading.Lock()
inbound_lock = threading.Lock()
inbound_in_flight = set()
configuration = {}
state = {}
desktop_check_failures = 0
last_scanned_question_mtime = {}
telegram_offset = 0


def log_line(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def load_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as source_file:
            return json.load(source_file)
    except (OSError, ValueError):
        return default


def save_state():
    with state_lock:
        try:
            temporary_path = STATE_PATH + ".tmp"
            with open(temporary_path, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, STATE_PATH)
        except OSError as error:
            log_line(f"Не удалось сохранить state: {error}")


def is_valid_session_id(session_id):
    return isinstance(session_id, str) and SESSION_ID_PATTERN.fullmatch(session_id) is not None


def get_claude_home():
    configured = configuration.get("claude_home")
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), ".claude")


def get_projects_directory():
    return os.path.join(get_claude_home(), "projects")


def resolve_cli_path():
    roaming = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Claude", "claude-code", "*", "claude.exe")
    candidates = glob.glob(roaming)
    if not candidates:
        return None

    def version_key(path):
        version_text = os.path.basename(os.path.dirname(path))
        return [int(chunk) if chunk.isdigit() else 0 for chunk in version_text.split(".")]

    candidates.sort(key=version_key)
    return candidates[-1]


def desktop_is_running():
    global desktop_check_failures
    marker = configuration.get("desktop_process_marker", "AnthropicClaude")
    command = (
        "(Get-CimInstance Win32_Process -Filter \"name='claude.exe'\" | "
        f"Where-Object {{ $_.ExecutablePath -like '*{marker}*' }} | Measure-Object).Count"
    )
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        desktop_check_failures = 0
        return int((completed.stdout or "0").strip() or "0") > 0
    except (subprocess.SubprocessError, ValueError, OSError) as error:
        desktop_check_failures += 1
        log_line(f"Проверка процесса десктопа не удалась ({desktop_check_failures}): {error}")
        return desktop_check_failures < DESKTOP_FAILURE_TOLERANCE


# --- Разбор транскриптов -------------------------------------------------

def extract_text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        collected = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                collected.append(block.get("text", ""))
        return "\n".join(part for part in collected if part)
    return ""


def is_noise_user_text(text):
    stripped = text.strip()
    if not stripped:
        return True
    noise_prefixes = ("<command-name>", "<local-command", "<command-message>", "Caveat:", "Continue from where you left off", "[Request interrupted")
    return stripped.startswith(noise_prefixes)


def iterate_transcript_entries(transcript_path):
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as transcript_file:
            for raw_line in transcript_file:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    yield json.loads(raw_line)
                except ValueError:
                    continue
    except OSError:
        return


def read_meaningful_messages(transcript_path, limit):
    messages = []
    for entry in iterate_transcript_entries(transcript_path):
        entry_type = entry.get("type")
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        if entry_type not in ("user", "assistant"):
            continue
        message_object = entry.get("message")
        if not isinstance(message_object, dict):
            continue
        text = extract_text_from_content(message_object.get("content")).strip()
        if not text:
            continue
        if entry_type == "user" and is_noise_user_text(text):
            continue
        is_error = bool(entry.get("isApiErrorMessage")) or "API Error" in text
        messages.append({"role": entry_type, "text": text, "timestamp": entry.get("timestamp", ""), "is_error": is_error})
    if limit and len(messages) > limit:
        return messages[-limit:]
    return messages


def read_recent_exchange(transcript_path, per_role=2, max_chars=300):
    # Последние per_role вопросов и столько же ответов. Длинные ответы пересказываем целиком (LLM), а не режем
    messages = read_meaningful_messages(transcript_path, 0)
    chosen_indices = set()
    for role in ("user", "assistant"):
        role_indices = [index for index, message in enumerate(messages) if message["role"] == role]
        chosen_indices.update(role_indices[-per_role:])
    result = []
    for index in sorted(chosen_indices):
        message = messages[index]
        original = message["text"]
        if len(original) > max_chars:
            text = llm_summarize(original, structured=True) or shorten(original, max_sentences=3, max_chars=max_chars)
        else:
            text = " ".join(strip_markdown(original).split())
        result.append({"role": message["role"], "text": text})
    return result


def effective_owner():
    configured = configuration.get("owner_chat_id")
    if configured:
        return configured
    return state.get("owner_chat_id")


def effective_notify_level():
    return state.get("notify_level") or configuration.get("notify_level", "all")


def find_pending_question(transcript_path):
    # Возвращает (id, input) последнего AskUserQuestion без ответа (сессия ждёт выбора), иначе (None, None)
    asks = {}
    order = []
    answered = set()
    for entry in iterate_transcript_entries(transcript_path):
        message_object = entry.get("message")
        if not isinstance(message_object, dict):
            continue
        content = message_object.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                asks[block.get("id")] = block.get("input")
                order.append(block.get("id"))
            elif block.get("type") == "tool_result":
                answered.add(block.get("tool_use_id"))
    for ask_id in reversed(order):
        if ask_id not in answered:
            return ask_id, asks[ask_id]
    return None, None


def extract_question(ask_input):
    if not isinstance(ask_input, dict):
        return None
    questions = ask_input.get("questions") or []
    if not questions or not isinstance(questions[0], dict):
        return None
    first = questions[0]
    options = [option.get("label", "") for option in (first.get("options") or []) if isinstance(option, dict) and option.get("label")]
    text = first.get("question", "")
    if not text:
        return None
    return {"text": text, "options": options}


def read_session_summary(transcript_path):
    first_user_text = ""
    git_branch = ""
    working_directory = ""
    custom_title = ""
    last_assistant = None
    last_role = None
    has_error = False
    tool_use_ids = set()
    tool_result_ids = set()
    for entry in iterate_transcript_entries(transcript_path):
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            custom_title = entry.get("customTitle")
        if not git_branch and entry.get("gitBranch"):
            git_branch = entry.get("gitBranch")
        if not working_directory and entry.get("cwd"):
            working_directory = entry.get("cwd")
        entry_type = entry.get("type")
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        if entry_type not in ("user", "assistant"):
            continue
        message_object = entry.get("message")
        if not isinstance(message_object, dict):
            continue
        content = message_object.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        tool_use_ids.add(block.get("id"))
                    elif block.get("type") == "tool_result":
                        tool_result_ids.add(block.get("tool_use_id"))
        text = extract_text_from_content(content).strip()
        if entry_type == "user":
            if not is_noise_user_text(text):
                if not first_user_text:
                    first_user_text = text
                last_role = "user"
        elif entry_type == "assistant" and text:
            last_assistant = text
            last_role = "assistant"
            # Ошибкой считаем ТОЛЬКО последний ход (а не всю историю): значение перезаписывается каждым новым ответом
            has_error = bool(entry.get("isApiErrorMessage")) or text.lstrip().startswith("API Error") or "API Error:" in text
    return {
        "first_user_text": first_user_text,
        "git_branch": git_branch,
        "cwd": working_directory,
        "custom_title": custom_title,
        "last_assistant": last_assistant,
        "last_role": last_role,
        "has_error": has_error,
        "has_unanswered_tool_use": bool(tool_use_ids - tool_result_ids),
    }


def read_tail_entries(transcript_path, max_bytes=65536):
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    entries = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def detect_tail_state(transcript_path):
    # По хвосту транскрипта: 'question' (ждёт ответа на AskUserQuestion), 'working' (сейчас работает), либо None (ход завершён текстом)
    last_entry = None
    for entry in read_tail_entries(transcript_path):
        if entry.get("type") in ("user", "assistant") and isinstance(entry.get("message"), dict) and not entry.get("isMeta"):
            last_entry = entry
    if not last_entry:
        return None
    content = last_entry["message"].get("content")
    blocks = content if isinstance(content, list) else []
    if last_entry["type"] == "assistant":
        if blocks and isinstance(blocks[-1], dict) and blocks[-1].get("type") == "tool_use":
            return "question" if blocks[-1].get("name") == "AskUserQuestion" else "working"
        return None
    return "working"


def classify_session_state(summary, transcript_path=None):
    if summary.get("has_error"):
        return "error", "🔴"
    tail_state = detect_tail_state(transcript_path) if transcript_path else None
    if tail_state == "question":
        return "question", "❓"
    # Незавершённый вызов инструмента = сессия занята / возможно ждёт твоего подтверждения (не «idle»)
    if summary.get("has_unanswered_tool_use") or tail_state == "working":
        return "working", "⏳"
    if transcript_path:
        try:
            # Подстраховка: транскрипт писался только что → Claude ещё работает (стрим текста / между инструментами)
            if time.time() - os.path.getmtime(transcript_path) < 20:
                return "working", "⏳"
        except OSError:
            pass
    last_assistant = summary.get("last_assistant") or ""
    if summary.get("last_role") == "assistant":
        if last_assistant.rstrip().endswith("?"):
            return "question", "❓"
        return "idle", "✅"
    return "working", "⏳"


def build_session_label(summary, session_id):
    base = summary.get("first_user_text") or summary.get("git_branch") or session_id[:8]
    base = " ".join(base.split())
    if len(base) > 60:
        base = base[:57] + "…"
    return base


def display_name(session_id, summary):
    nickname = state.get("session_names", {}).get(session_id)
    if nickname:
        return nickname
    if summary.get("custom_title"):
        return summary["custom_title"]
    return build_session_label(summary, session_id)


def strip_markdown(text):
    text = re.sub(r"[*_`~]+", "", text or "")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    return text


def shorten(text, max_sentences=3, max_chars=320):
    collapsed = " ".join(strip_markdown(text).split())
    if not collapsed:
        return ""
    sentences = re.split(r"(?<=[.!?…])\s+", collapsed)
    result = " ".join(sentences[:max_sentences]).strip()
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + "…"
    return result


def find_session_file(session_id):
    if not is_valid_session_id(session_id):
        return None
    projects_directory = get_projects_directory()
    if not os.path.isdir(projects_directory):
        return None
    for project_name in os.listdir(projects_directory):
        candidate = os.path.join(projects_directory, project_name, session_id + ".jsonl")
        if os.path.isfile(candidate):
            return candidate
    return None


def resolve_event_transcript(event):
    # Доверяем transcript_path из события, только если он реально внутри каталога projects
    # (защита от path traversal); иначе резолвим строго по валидному session_id.
    candidate = event.get("transcript_path") or ""
    if candidate:
        try:
            projects_real = os.path.realpath(get_projects_directory())
            candidate_real = os.path.realpath(candidate)
            if os.path.isfile(candidate_real) and os.path.commonpath([projects_real, candidate_real]) == projects_real:
                return candidate_real
        except (OSError, ValueError):
            pass
    return find_session_file(event.get("session_id", ""))


def is_internal_session(summary):
    first_user_text = summary.get("first_user_text") or ""
    return first_user_text.startswith(INTERNAL_SUMMARY_MARKER) or first_user_text.startswith("Перескажи по-русски в 2-3 предложениях")


def list_recent_sessions(limit):
    projects_directory = get_projects_directory()
    candidates = []
    if not os.path.isdir(projects_directory):
        return candidates
    for project_name in os.listdir(projects_directory):
        project_path = os.path.join(projects_directory, project_name)
        if not os.path.isdir(project_path):
            continue
        for transcript_path in glob.glob(os.path.join(project_path, "*.jsonl")):
            try:
                modified_at = os.path.getmtime(transcript_path)
            except OSError:
                continue
            candidates.append({"session_id": os.path.basename(transcript_path)[:-6], "transcript_path": transcript_path, "modified_at": modified_at})
    candidates.sort(key=lambda item: item["modified_at"], reverse=True)
    selected = []
    for row in candidates:
        summary = read_session_summary(row["transcript_path"])
        if is_internal_session(summary):
            continue
        state_name, emoji = classify_session_state(summary, row["transcript_path"])
        row["summary"] = summary
        row["state_name"] = state_name
        row["emoji"] = emoji
        row["label"] = build_session_label(summary, row["session_id"])
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def format_relative_time(modified_at):
    delta_seconds = max(0, int(time.time() - modified_at))
    if delta_seconds < 60:
        return f"{delta_seconds} с назад"
    if delta_seconds < 3600:
        return f"{delta_seconds // 60} мин назад"
    if delta_seconds < 86400:
        return f"{delta_seconds // 3600} ч назад"
    return f"{delta_seconds // 86400} дн назад"


# --- Telegram API --------------------------------------------------------

def telegram_request(method, payload, timeout):
    token = configuration.get("telegram_bot_token", "")
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_http_error(http_error):
    try:
        body = json.loads(http_error.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        body = {}
    return {"description": body.get("description", ""), "retry_after": (body.get("parameters") or {}).get("retry_after")}


def strip_html(text):
    return html.unescape(HTML_TAG_PATTERN.sub("", text))


def find_safe_cut(text, limit):
    cut = limit
    ampersand = text.rfind("&", 0, cut)
    semicolon = text.rfind(";", 0, cut)
    if ampersand > semicolon:
        cut = ampersand
    less_than = text.rfind("<", 0, cut)
    greater_than = text.rfind(">", 0, cut)
    if less_than > greater_than:
        cut = less_than
    if cut < limit - 200:
        # Слишком далеко откатились назад (патологический ввод) — режем жёстко, plain-fallback подстрахует
        return limit
    return max(1, cut)


def split_text_into_chunks(text):
    chunks = []
    current = ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 > TELEGRAM_MESSAGE_LIMIT:
            if current:
                chunks.append(current)
                current = ""
            while len(paragraph) > TELEGRAM_MESSAGE_LIMIT:
                cut = find_safe_cut(paragraph, TELEGRAM_MESSAGE_LIMIT)
                chunks.append(paragraph[:cut])
                paragraph = paragraph[cut:]
            current = paragraph
        else:
            current = paragraph if not current else current + "\n" + paragraph
    if current:
        chunks.append(current)
    return chunks or [""]


def deliver_chunk(chat_id, text, markup, plain=False):
    if not text.strip():
        return True
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if not plain:
        payload["parse_mode"] = "HTML"
    if markup:
        payload["reply_markup"] = markup
    try:
        telegram_request("sendMessage", payload, timeout=20)
        return True
    except urllib.error.HTTPError as http_error:
        detail = read_http_error(http_error)
        log_line(f"sendMessage HTTP {http_error.code}: {detail['description']}")
        return False
    except (urllib.error.URLError, ValueError, OSError) as error:
        log_line(f"sendMessage ошибка: {error}")
        return False


def send_telegram_message(chat_id, text, buttons=None):
    chunks = split_text_into_chunks(text)
    last_index = max((index for index, chunk in enumerate(chunks) if chunk.strip()), default=len(chunks) - 1)
    for index, chunk in enumerate(chunks):
        markup = {"inline_keyboard": buttons} if (buttons and index == last_index) else None
        if not deliver_chunk(chat_id, chunk, markup):
            # Запасной путь: если Telegram отверг HTML-разметку — шлём тем же чанком как обычный текст
            deliver_chunk(chat_id, strip_html(chunk), markup, plain=True)


def answer_callback(callback_query_id, text=""):
    try:
        telegram_request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}, timeout=15)
    except (urllib.error.URLError, ValueError, OSError):
        pass


MAIN_REPLY_KEYBOARD = {"keyboard": [["📊 Сессии"]], "resize_keyboard": True, "is_persistent": True}


def send_with_main_keyboard(chat_id, text):
    try:
        telegram_request("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": MAIN_REPLY_KEYBOARD}, timeout=20)
    except (urllib.error.URLError, ValueError, OSError) as error:
        log_line(f"sendMessage(keyboard) ошибка: {error}")


def set_bot_commands():
    commands = [
        {"command": "status", "description": "Последние 5 сессий и их статусы"},
        {"command": "level", "description": "Что присылать: all или important"},
        {"command": "name", "description": "Назвать активную сессию"},
        {"command": "stop", "description": "Выключить бота"},
        {"command": "help", "description": "Справка"},
    ]
    try:
        telegram_request("setMyCommands", {"commands": commands}, timeout=15)
    except (urllib.error.URLError, ValueError, OSError):
        pass


# --- Inbound: отправка ответа в сессию через CLI -------------------------

def subprocess_env_with_token():
    environment = os.environ.copy()
    environment["TG_BRIDGE_INTERNAL"] = "1"
    token = configuration.get("claude_oauth_token")
    if token:
        environment["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return environment


def llm_summarize(text, structured=False):
    if not configuration.get("summarize_with_llm"):
        return None
    cli_path = resolve_cli_path()
    if not cli_path or not (text or "").strip():
        return None
    if structured:
        instruction = "Сократи это сообщение, СОХРАНИВ структуру: если в нём есть пункты/список — оставь их нумерованными пунктами, по одной короткой фразе на каждый (суть/решение по пункту, без потери смысла). Если это сплошной текст — 2-4 предложения сути."
    else:
        instruction = "Перескажи по-русски в 2-3 предложениях суть этого сообщения, передав главное (что сделано / что дальше / суть вопроса)."
    prompt = INTERNAL_SUMMARY_MARKER + " " + instruction + " По-русски, без markdown, без преамбулы, только результат:\n\n" + text[:6000]
    arguments = [cli_path, "-p", prompt, "--model", "claude-haiku-4-5", "--output-format", "json"]
    try:
        completed = subprocess.run(arguments, cwd=os.path.expanduser("~"), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40, creationflags=CREATE_NO_WINDOW, env=subprocess_env_with_token())
        parsed = json.loads((completed.stdout or "").strip())
        if parsed.get("is_error"):
            return None
        return (parsed.get("result") or "").strip() or None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def send_message_into_session(session_id, working_directory, text):
    if not configuration.get("inbound_enabled", False):
        return {"ok": False, "result": "Ответы в сессию отключены (inbound_enabled=false). Бот не делает вызовов Claude — это защита аккаунта."}
    cli_path = resolve_cli_path()
    if not cli_path:
        return {"ok": False, "result": "CLI Claude Code не найден на хосте."}
    arguments = [cli_path, "-p", text, "--resume", session_id, "--output-format", "json"]
    try:
        completed = subprocess.run(arguments, cwd=working_directory or None, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=INBOUND_TIMEOUT_SECONDS, creationflags=CREATE_NO_WINDOW, env=subprocess_env_with_token())
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": "Сессия не ответила за 10 минут (таймаут)."}
    except OSError as error:
        return {"ok": False, "result": f"Не удалось запустить CLI: {error}"}
    stdout = (completed.stdout or "").strip()
    try:
        parsed = json.loads(stdout)
    except ValueError:
        if completed.returncode != 0:
            return {"ok": False, "result": (completed.stderr or stdout or "Неизвестная ошибка CLI")[:1500]}
        return {"ok": True, "result": stdout[:3000] or "(пустой ответ)"}
    result_text = parsed.get("result", "")
    if parsed.get("is_error"):
        return {"ok": False, "result": result_text or "Ошибка выполнения."}
    return {"ok": True, "result": result_text or "(пустой ответ)"}


def route_reply_async(chat_id, session_id, working_directory, text):
    if not is_valid_session_id(session_id):
        send_telegram_message(chat_id, "Некорректный идентификатор сессии.")
        return
    if not text:
        send_telegram_message(chat_id, "Пустое сообщение — нечего отправлять.")
        return
    with inbound_lock:
        busy = session_id in inbound_in_flight
        if not busy:
            inbound_in_flight.add(session_id)
    if busy:
        send_telegram_message(chat_id, "⏳ Уже выполняю предыдущий запрос по этой сессии — дождись ответа.")
        return

    def worker():
        try:
            transcript = find_session_file(session_id)
            target_name = display_name(session_id, read_session_summary(transcript) if transcript else {})
            send_telegram_message(chat_id, f"⏳ Отправляю в «{html.escape(target_name)}» и жду ответ…")
            outcome = send_message_into_session(session_id, working_directory, text)
            if outcome["ok"]:
                header = f"💬 Ответ из сессии <code>{html.escape(session_id[:8])}</code>:"
                send_telegram_message(chat_id, f"{header}\n\n{html.escape(outcome['result'])}", build_session_action_buttons(session_id))
            else:
                hint = ""
                if "login" in outcome["result"].lower():
                    hint = "\n\n⚠️ CLI не авторизован. Выполни один раз в терминале: <code>claude setup-token</code>"
                send_telegram_message(chat_id, f"⚠️ Не удалось доставить в сессию:\n{html.escape(outcome['result'])}{hint}")
        finally:
            with inbound_lock:
                inbound_in_flight.discard(session_id)

    threading.Thread(target=worker, daemon=True).start()


# --- Кнопки --------------------------------------------------------------

def build_session_action_buttons(session_id):
    return [[{"text": "✍️ Ответить", "callback_data": f"reply|{session_id}"}, {"text": "📖 Открыть диалог", "callback_data": f"sess|{session_id}"}]]


def build_question_buttons(session_id, options):
    rows = [[{"text": f"{index + 1}. {option[:36]}", "callback_data": f"opt|{session_id}|{index}"}] for index, option in enumerate(options[:6])]
    rows.append([{"text": "📖 Открыть диалог", "callback_data": f"sess|{session_id}"}])
    return rows


CANNED_REPLIES = {
    "continue": "Продолжай.",
}


# --- Обработка событий от хуков ------------------------------------------

def event_dedupe_key(event):
    return f"{event.get('event')}|{event.get('session_id')}|{event.get('ts', '')}"


def is_within_quiet_hours():
    quiet_hours = configuration.get("quiet_hours")
    if not quiet_hours or len(quiet_hours) != 2:
        return False
    start_hour, end_hour = quiet_hours
    current_hour = datetime.datetime.now().hour
    if start_hour <= end_hour:
        return start_hour <= current_hour < end_hour
    return current_hour >= start_hour or current_hour < end_hour


def classify_hook_event(event):
    session_id = event.get("session_id", "")
    transcript_path = resolve_event_transcript(event)
    summary = read_session_summary(transcript_path) if transcript_path else {}
    if is_internal_session(summary):
        return None
    name = html.escape(display_name(session_id, summary) if session_id else "сессия")
    head = f"<b>{name}</b>"

    match event.get("event"):
        case "Stop":
            last_assistant = (summary.get("last_assistant") or "").strip()
            if summary.get("has_error"):
                body = llm_summarize(last_assistant) or shorten(last_assistant)
                return {"priority": "high", "text": f"🔴 {head}\n{html.escape(body)}", "session_id": session_id, "cwd": summary.get("cwd", "")}
            if not last_assistant:
                return None
            if last_assistant.rstrip().endswith("?"):
                # Вопрос показываем как есть (укорачивая), чтобы было что ответить — без пересказа
                icon, priority, body = "❓", "high", shorten(last_assistant, max_sentences=4, max_chars=500)
            else:
                icon, priority, body = "✅", "normal", (llm_summarize(last_assistant) or shorten(last_assistant))
            return {"priority": priority, "text": f"{icon} {head}\n{html.escape(body)}", "session_id": session_id, "cwd": summary.get("cwd", "")}
        case "Notification":
            # Если сессия ждёт ответа на AskUserQuestion — его отправит question-watcher с вариантами, не дублируем
            if transcript_path and find_pending_question(transcript_path)[0]:
                return None
            message_text = (event.get("message") or "").strip()
            if not message_text:
                return None
            return {"priority": "high", "text": f"🔔 {head}\n{html.escape(shorten(message_text))}", "session_id": session_id, "cwd": summary.get("cwd", "")}
        case "PermissionRequest":
            tool_name = event.get("tool_name") or "инструмент"
            tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
            detail = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("path") or tool_input.get("url") or ""
            body = tool_name + (f": {detail}" if detail else "")
            return {"priority": "high", "text": f"🔐 <b>{name}</b> просит разрешение\n<code>{html.escape(body[:350])}</code>\n<i>Подтверди в десктопе.</i>", "session_id": session_id, "cwd": summary.get("cwd", "")}
        case "SubagentStop":
            return None
        case _:
            return None


def process_hook_event(event):
    with state_lock:
        seen = state.setdefault("seen_events", [])
        key = event_dedupe_key(event)
        if key in seen:
            return
        seen.append(key)
        if len(seen) > 200:
            del seen[:-200]
    owner_chat_id = effective_owner()
    if not owner_chat_id:
        return
    notification = classify_hook_event(event)
    if not notification:
        return
    # notify_level="important" → шлём только высокоприоритетное (❓ вопросы, 🔴 ошибки, 🔔 запросы внимания), без обычных ✅
    if effective_notify_level() == "important" and notification["priority"] != "high":
        return
    if is_within_quiet_hours() and notification["priority"] != "high":
        return
    session_id = notification.get("session_id")
    buttons = build_session_action_buttons(session_id) if is_valid_session_id(session_id) else None
    send_telegram_message(owner_chat_id, notification["text"], buttons)
    save_state()


def event_worker_loop():
    while running:
        try:
            event = event_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            process_hook_event(event)
        except Exception as error:
            log_line(f"Ошибка обработки события: {error}")


def drain_spool():
    if not os.path.isfile(SPOOL_PATH):
        return
    draining_path = SPOOL_PATH + ".draining"
    try:
        os.replace(SPOOL_PATH, draining_path)
    except OSError:
        return
    lines = []
    try:
        with open(draining_path, encoding="utf-8", errors="replace") as spool_file:
            lines = spool_file.readlines()
        os.remove(draining_path)
    except OSError:
        pass
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event_queue.put(json.loads(raw_line))
        except ValueError:
            continue


def spool_redrain_loop():
    while running:
        time.sleep(30)
        try:
            drain_spool()
        except Exception as error:
            log_line(f"Ошибка повторного дренажа spool: {error}")


def cleanup_internal_sessions():
    # Удаляем старые (>1ч) служебные сессии-пересказы, которые порождает llm_summarize, чтобы не копились на диске
    projects_directory = get_projects_directory()
    if not os.path.isdir(projects_directory):
        return
    cutoff = time.time() - 3600
    for project_name in os.listdir(projects_directory):
        project_path = os.path.join(projects_directory, project_name)
        if not os.path.isdir(project_path):
            continue
        for transcript_path in glob.glob(os.path.join(project_path, "*.jsonl")):
            try:
                if os.path.getmtime(transcript_path) > cutoff:
                    continue
            except OSError:
                continue
            if is_internal_session(read_session_summary(transcript_path)):
                try:
                    os.remove(transcript_path)
                except OSError:
                    pass


def maintenance_loop():
    while running:
        time.sleep(600)
        try:
            cleanup_internal_sessions()
        except Exception as error:
            log_line(f"Ошибка очистки внутренних сессий: {error}")


# --- HTTP-сервер для приёма событий от hook.py ---------------------------

class HookRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/event":
            self.send_response(404)
            self.end_headers()
            return
        configured_secret = configuration.get("event_secret")
        if configured_secret and self.headers.get("X-Bridge-Secret") != configured_secret:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"forbidden")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            event = json.loads(body.decode("utf-8"))
            event_queue.put(event)
            self.send_response(200)
        except ValueError:
            self.send_response(400)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format_string, *arguments):
        return


def start_hook_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), HookRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- Telegram: обработка апдейтов ----------------------------------------

def render_help_text():
    return (
        "🤖 <b>Claude → Telegram мост</b>\n\n"
        "Я присылаю важные статусы сессий (готово / вопрос / ошибка) и даю отвечать прямо отсюда.\n\n"
        "Команды:\n"
        "/status — дайджест по последним сессиям\n"
        "/name &lt;имя&gt; — назвать активную сессию (например /name 840_3)\n"
        "/level all|important — что присылать: всё или только важное\n"
        "/stop — выключить бота (не будет запускаться, пока не включишь enable.ps1)\n"
        "/help — эта справка\n\n"
        "Кнопка «📊 Сессии» внизу (или /status) — список 5 последних сессий: ✅ готово · ❓ ждёт ответа · ⏳ в работе · 🔴 ошибка. Тапни сессию — увидишь последние сообщения.\n"
        "Чтобы ответить в сессию: нажми «✍️ Ответить» под её уведомлением (или открой её) — после этого пиши текст, он уйдёт в неё."
    )


def handle_status_command(chat_id):
    sessions = list_recent_sessions(STATUS_DIGEST_SESSION_COUNT)
    if not sessions:
        send_telegram_message(chat_id, "Пока не нашёл ни одной сессии.")
        return
    lines = ["📊 <b>Последние сессии</b>\n"]
    buttons = []
    for session in sessions:
        name = display_name(session["session_id"], session["summary"])
        snippet = shorten(session["summary"].get("last_assistant") or "", 1, 80)
        lines.append(f"{session['emoji']} <b>{html.escape(name)}</b>\n<i>{format_relative_time(session['modified_at'])}</i> — {html.escape(snippet)}")
        buttons.append([{"text": f"{session['emoji']} {name[:24]}", "callback_data": f"sess|{session['session_id']}"}])
    buttons.append([{"text": "🔄 Обновить", "callback_data": "refresh|status"}])
    send_telegram_message(chat_id, "\n\n".join(lines), buttons)


def handle_open_session(chat_id, session_id):
    transcript_path = find_session_file(session_id)
    if not transcript_path:
        send_telegram_message(chat_id, "Сессия не найдена (возможно, заархивирована).")
        return
    summary = read_session_summary(transcript_path)
    messages = read_recent_exchange(transcript_path)
    state_name, emoji = classify_session_state(summary, transcript_path)
    with state_lock:
        state.setdefault("active_target", {})[str(chat_id)] = {"session_id": session_id, "cwd": summary.get("cwd", "")}
    save_state()
    header = f"{emoji} <b>{html.escape(display_name(session_id, summary))}</b>"
    body_lines = [header, ""]
    for message in messages:
        prefix = "🧑" if message["role"] == "user" else "🤖"
        body_lines.append(f"{prefix} {html.escape(message['text'])}")
    if summary.get("has_unanswered_tool_use"):
        body_lines.append("\n⏳ <b>Сессия чем-то занята</b> — есть незавершённое действие. Возможно, ждёт твоего подтверждения в десктопе — проверь там.")
    body_lines.append("\n<i>Активная сессия — пиши сюда, и я передам в неё. /name &lt;имя&gt; — переименовать.</i>")
    send_telegram_message(chat_id, "\n".join(body_lines), build_session_action_buttons(session_id))


def handle_text_message(chat_id, text):
    chat_key = str(chat_id)
    with state_lock:
        awaiting = state.get("awaiting_reply", {}).pop(chat_key, None)
        target = awaiting or state.get("active_target", {}).get(chat_key)
    if not target:
        send_telegram_message(chat_id, "Не выбрана сессия. Нажми «✍️ Ответить» под уведомлением (или открой сессию через «📊 Сессии») — тогда пойму, куда отправить.")
        return
    save_state()
    route_reply_async(chat_id, target["session_id"], target.get("cwd", ""), text)


def handle_name_command(chat_id, text):
    nickname = text[len("/name"):].strip()
    if not nickname:
        send_telegram_message(chat_id, "Использование: <code>/name короткое-имя</code> — назвать текущую активную сессию (например <code>/name 840_3</code>).")
        return
    target = state.get("active_target", {}).get(str(chat_id))
    if not target:
        send_telegram_message(chat_id, "Сначала открой сессию через /status, потом /name &lt;имя&gt;.")
        return
    with state_lock:
        state.setdefault("session_names", {})[target["session_id"]] = nickname[:40]
    save_state()
    send_telegram_message(chat_id, f"✅ Сессия названа: <b>{html.escape(nickname[:40])}</b>")


def handle_level_command(chat_id, text):
    value = text[len("/level"):].strip().lower()
    if value not in ("all", "important"):
        send_telegram_message(chat_id, f"Текущий режим: <b>{html.escape(effective_notify_level())}</b>.\n<code>/level all</code> — все уведомления (✅ готово, ❓ вопрос, 🔴 ошибка, 🔔 внимание).\n<code>/level important</code> — только ❓ вопросы, 🔴 ошибки, 🔔 внимание.")
        return
    with state_lock:
        state["notify_level"] = value
    save_state()
    send_telegram_message(chat_id, f"✅ Режим уведомлений: <b>{value}</b>.")


def set_config_enabled(value):
    try:
        config = load_json_file(CONFIG_PATH, {})
        config["enabled"] = value
        temporary_path = CONFIG_PATH + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, CONFIG_PATH)
    except OSError as error:
        log_line(f"Не удалось записать enabled в config: {error}")


def handle_stop_command(chat_id):
    set_config_enabled(False)
    send_telegram_message(chat_id, "🔴 Выключаюсь и больше не запускаюсь автоматически.\nВключить обратно — запусти на ПК <code>enable.bat</code> (двойной клик) или <code>enable.py</code>.")
    # Подтверждаем offset в Telegram, чтобы /stop не переотдавался при следующем старте (иначе петля стоп→старт→стоп)
    try:
        telegram_request("getUpdates", {"offset": telegram_offset, "timeout": 0}, timeout=10)
    except (urllib.error.URLError, ValueError, OSError):
        pass
    log_line("Команда /stop — выключаюсь (enabled=false).")
    os._exit(0)


def handle_callback(chat_id, callback_query_id, data):
    parts = data.split("|")
    action = parts[0]
    match action:
        case "sess" if len(parts) >= 2 and is_valid_session_id(parts[1]):
            answer_callback(callback_query_id, "Открываю…")
            threading.Thread(target=handle_open_session, args=(chat_id, parts[1]), daemon=True).start()
        case "refresh":
            answer_callback(callback_query_id, "Обновляю")
            handle_status_command(chat_id)
        case "reply" if len(parts) >= 2 and is_valid_session_id(parts[1]):
            answer_callback(callback_query_id)
            session_id = parts[1]
            summary = read_session_summary(find_session_file(session_id)) if find_session_file(session_id) else {}
            with state_lock:
                state.setdefault("awaiting_reply", {})[str(chat_id)] = {"session_id": session_id, "cwd": summary.get("cwd", "")}
            save_state()
            send_telegram_message(chat_id, f"✍️ Напиши сообщение — отправлю в сессию <code>{html.escape(session_id[:8])}</code>.")
        case "act" if len(parts) >= 3 and parts[1] in CANNED_REPLIES and is_valid_session_id(parts[2]):
            answer_callback(callback_query_id, "Отправляю")
            session_id = parts[2]
            summary = read_session_summary(find_session_file(session_id)) if find_session_file(session_id) else {}
            route_reply_async(chat_id, session_id, summary.get("cwd", ""), CANNED_REPLIES[parts[1]])
        case "opt" if len(parts) >= 3 and is_valid_session_id(parts[1]) and parts[2].isdigit():
            answer_callback(callback_query_id, "Отправляю")
            session_id = parts[1]
            index = int(parts[2])
            options = state.get("pending_options", {}).get(session_id, [])
            if 0 <= index < len(options):
                summary = read_session_summary(find_session_file(session_id)) if find_session_file(session_id) else {}
                route_reply_async(chat_id, session_id, summary.get("cwd", ""), options[index])
            else:
                send_telegram_message(chat_id, "Вариант устарел — ответь текстом.")
        case _:
            answer_callback(callback_query_id, "Не удалось обработать действие")


def is_authorized(chat_id, text):
    owner_chat_id = effective_owner()
    if owner_chat_id is None:
        if text.strip().startswith("/start"):
            with state_lock:
                state["owner_chat_id"] = chat_id
            save_state()
            log_line(f"Владелец закреплён: chat_id={chat_id}")
            return True
        return False
    return chat_id == owner_chat_id


def handle_update(update):
    if "message" in update and "text" in update.get("message", {}):
        message = update["message"]
        chat_id = message["chat"]["id"]
        text = message["text"]
        if not is_authorized(chat_id, text):
            return
        if text == "📊 Сессии":
            handle_status_command(chat_id)
            return
        if text.startswith("/"):
            command = text.split()[0].split("@")[0]
            match command:
                case "/start" | "/help":
                    send_with_main_keyboard(chat_id, render_help_text())
                case "/status" | "/sessions":
                    handle_status_command(chat_id)
                case "/name":
                    handle_name_command(chat_id, text)
                case "/level":
                    handle_level_command(chat_id, text)
                case "/stop" | "/off":
                    handle_stop_command(chat_id)
                case "/whoami":
                    send_telegram_message(chat_id, f"chat_id: <code>{chat_id}</code>")
                case _:
                    send_telegram_message(chat_id, "Неизвестная команда. /help — список.")
        else:
            handle_text_message(chat_id, text)
    elif "callback_query" in update:
        callback = update["callback_query"]
        chat = (callback.get("message") or {}).get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or effective_owner() != chat_id:
            answer_callback(callback.get("id", ""))
            return
        handle_callback(chat_id, callback["id"], callback.get("data", ""))


def telegram_polling_loop():
    global telegram_offset
    telegram_offset = state.get("telegram_offset", 0)
    backoff = 3
    while running:
        payload = {"timeout": 25, "offset": telegram_offset, "allowed_updates": ["message", "callback_query"]}
        try:
            response = telegram_request("getUpdates", payload, timeout=35)
        except urllib.error.HTTPError as http_error:
            detail = read_http_error(http_error)
            match http_error.code:
                case 401:
                    log_line(f"Telegram 401 (невалидный токен?): {detail['description']}")
                    time.sleep(min(60, backoff))
                case 409:
                    log_line(f"Telegram 409 (конфликт getUpdates — токен опрашивается ещё где-то): {detail['description']}")
                    time.sleep(backoff)
                case 429:
                    time.sleep(detail["retry_after"] or backoff)
                case _:
                    log_line(f"Telegram HTTP {http_error.code}: {detail['description']}")
                    time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except (urllib.error.URLError, ValueError, OSError) as error:
            log_line(f"getUpdates ошибка: {error}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        backoff = 3
        if not response.get("ok"):
            time.sleep(3)
            continue
        for update in response.get("result", []):
            telegram_offset = update["update_id"] + 1
            with state_lock:
                state["telegram_offset"] = telegram_offset
            save_state()
            try:
                handle_update(update)
            except Exception as error:
                log_line(f"Ошибка обработки апдейта: {error}")


# --- Слежение за десктопом: самовыключение -------------------------------

def wait_for_inbound_to_finish():
    waited = 0
    while waited < 120:
        with inbound_lock:
            if not inbound_in_flight:
                return
        time.sleep(2)
        waited += 2


def lifecycle_watch_loop():
    global running
    grace_seconds = configuration.get("desktop_grace_seconds", 45)
    poll_seconds = configuration.get("desktop_poll_seconds", 20)
    last_seen = time.time()
    time.sleep(poll_seconds)
    while running:
        try:
            if not load_json_file(CONFIG_PATH, {}).get("enabled", True):
                log_line("enabled=false — завершаю бота.")
                running = False
                os._exit(0)
            if desktop_is_running():
                last_seen = time.time()
            elif time.time() - last_seen > grace_seconds:
                wait_for_inbound_to_finish()
                log_line("Claude Desktop закрыт — завершаю бота.")
                running = False
                os._exit(0)
        except Exception as error:
            log_line(f"Ошибка слежения за десктопом: {error}")
        time.sleep(poll_seconds)


def scan_pending_questions(owner_chat_id):
    projects_directory = get_projects_directory()
    if not os.path.isdir(projects_directory):
        return
    candidates = []
    for project_name in os.listdir(projects_directory):
        project_path = os.path.join(projects_directory, project_name)
        if not os.path.isdir(project_path):
            continue
        for transcript_path in glob.glob(os.path.join(project_path, "*.jsonl")):
            try:
                candidates.append((os.path.getmtime(transcript_path), transcript_path))
            except OSError:
                continue
    candidates.sort(reverse=True)
    for modified_at, transcript_path in candidates[:10]:
        session_id = os.path.basename(transcript_path)[:-6]
        if last_scanned_question_mtime.get(session_id) == modified_at:
            continue
        last_scanned_question_mtime[session_id] = modified_at
        ask_id, ask_input = find_pending_question(transcript_path)
        if not ask_id or ask_id in state.get("notified_questions", []):
            continue
        question = extract_question(ask_input)
        if not question:
            continue
        summary = read_session_summary(transcript_path)
        name = display_name(session_id, summary)
        with state_lock:
            state.setdefault("pending_options", {})[session_id] = question["options"]
            notified = state.setdefault("notified_questions", [])
            notified.append(ask_id)
            if len(notified) > 200:
                del notified[:-200]
            state.setdefault("active_target", {})[str(owner_chat_id)] = {"session_id": session_id, "cwd": summary.get("cwd", "")}
        save_state()
        text = f"❓ <b>{html.escape(name)}</b> ждёт ответа\n{html.escape(shorten(question['text'], 3, 400))}"
        if question["options"]:
            text += "\n\n" + "\n".join(f"{index + 1}. {html.escape(option)}" for index, option in enumerate(question["options"]))
        text += "\n\n<i>Ответь кнопкой, номером или своим текстом — уйдёт в сессию.</i>"
        send_telegram_message(owner_chat_id, text, build_question_buttons(session_id, question["options"]))


def question_watch_loop():
    poll_seconds = configuration.get("question_poll_seconds", 10)
    time.sleep(poll_seconds)
    while running:
        try:
            owner_chat_id = effective_owner()
            if owner_chat_id:
                scan_pending_questions(owner_chat_id)
        except Exception as error:
            log_line(f"Ошибка слежения за вопросами: {error}")
        time.sleep(poll_seconds)


def acquire_single_instance(port):
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        guard.bind(("127.0.0.1", port + 1))
        return guard
    except OSError:
        return None


def main():
    global configuration, state
    configuration = load_json_file(CONFIG_PATH, {})
    state = load_json_file(STATE_PATH, {})
    if not configuration.get("enabled", True):
        log_line("Бот выключен (enabled=false) — выход.")
        return
    token = configuration.get("telegram_bot_token", "")
    if not token or token.startswith("PUT-"):
        log_line("Токен Telegram не задан в config.json — выход.")
        return
    port = configuration.get("listen_port", 8787)
    guard_socket = acquire_single_instance(port)
    if guard_socket is None:
        log_line("Экземпляр бота уже запущен — выход.")
        return
    log_line("Бот запускается.")
    set_bot_commands()
    start_hook_server(port)
    drain_spool()
    threading.Thread(target=event_worker_loop, daemon=True).start()
    threading.Thread(target=spool_redrain_loop, daemon=True).start()
    threading.Thread(target=lifecycle_watch_loop, daemon=True).start()
    threading.Thread(target=question_watch_loop, daemon=True).start()
    threading.Thread(target=maintenance_loop, daemon=True).start()
    owner_chat_id = effective_owner()
    if owner_chat_id:
        send_telegram_message(owner_chat_id, "🟢 Бот включён и работает. Напиши /status — список сессий.")
    telegram_polling_loop()


if __name__ == "__main__":
    main()
