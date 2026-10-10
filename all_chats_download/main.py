"""all_chats_download — плагин exteraGram (SDK Elyx).

Добавляет пункт 💾 «Скачать чат» в меню «три точки» внутри чата или группы.
По нажатию выгружает всю доступную историю переписки в .txt файл и сохраняет
его в папку «Загрузки» на устройстве.

Формат файла:

    Чат: Название
    Экспортировано: 2026-10-09 15:30
    Всего сообщений: 12453
    ─────────────────────────────
    [2024-01-01 10:15] Иван: Привет
    [2024-01-01 10:16] Мария: Привет, как дела?
    [2024-01-01 10:17] Иван: [фото]

Имя файла: chat_НазваниеЧата_2026-10-09.txt (недопустимые символы -> "_").

Во время выгрузки показывается прогресс «Загружено N / M» с кнопкой «Отменить».
После сохранения — диалог «Чат экспортирован, файл в Загрузках» с кнопками
«Открыть папку» и «ОК».
"""

__elyx_entrypoint__ = "main:AllChatsDownload"

import re
import time
from datetime import datetime

from java.io import File
from java.lang import Integer, String

from org.telegram.messenger import (
    ChatMessagesController,
    MessagesController,
)

# --- Импорты SDK: поддерживаем и пакет `extera`, и пакет `elyx` -------------
try:
    from extera import BasePlugin, PluginMetadata
except ImportError:  # pragma: no cover - зависит от сборки SDK
    from elyx import BasePlugin, PluginMetadata

try:
    from extera.android_utils import (
        makeOnClickListener,
        onAndroidUiThread,
        runOnUIThread,
        startActivity,
        toast,
    )
    from extera.client_utils import (
        get_current_activity,
        get_dialog_object,
        get_user_id,
    )
    from extera.file_utils import get_downloads_dir, write_bytes
except ImportError:  # pragma: no cover - альтернативные имена модулей
    from elyx.android import (
        makeOnClickListener,
        onAndroidUiThread,
        runOnUIThread,
        startActivity,
        toast,
    )
    from elyx.client import get_current_activity, get_dialog_object, get_user_id
    from elyx.files import get_downloads_dir, write_bytes


BATCH_SIZE = 100        # размер пачки при запросе истории (~100 сообщений)
POLL_STEP_MS = 200      # период обновления прогресса / подтяжки сообщений
MAX_WAIT_SEC = 60       # макс. ожидание ответа сервера перед финализацией
MENU_HASH = 0x41D4A4AD  # уникальный id пункта меню


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _peer_title(dialog):
    """Название чата/группы/пользователя по DialogObject."""
    try:
        if dialog.chat is not None:
            t = str(dialog.chat.title or "")
            if t:
                return t
        if dialog.channel is not None:
            t = str(dialog.channel.title or "")
            if t:
                return t
        if dialog.user is not None:
            u = dialog.user
            name = " ".join(
                x for x in (str(u.first_name or ""), str(u.last_name or "")) if x
            )
            return name or str(u.username or "")
    except Exception:
        pass
    try:
        return f"Чат {int(dialog.getPeer().id)}"
    except Exception:
        return "Чат"


def _sender_name(msg, dc):
    """Имя отправителя сообщения."""
    try:
        uid = int(msg.fromId)
        if uid == 0:
            return "Система"
        if uid == int(get_user_id(dc)):
            return "Вы"
        user = MessagesController.getInstance(dc).getUser(Integer(uid), False)
        if user is not None:
            name = " ".join(
                x for x in (str(user.first_name or ""), str(user.last_name or "")) if x
            )
            if name:
                return name
            if user.username:
                return "@" + str(user.username)
    except Exception:
        pass
    return "Участник"


def _media_label(media):
    """Пометка для нетекстовых сообщений: [фото], [видео], [голосое] и т.д."""
    if media is None:
        return None
    cls = type(media).__name__
    if cls in ("MessageMediaPhoto", "MessageServicePhoto"):
        return "[фото]"
    if cls == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        mtype = str(getattr(doc, "mtype", "") or "") if doc is not None else ""
        if doc is not None and getattr(doc, "sticker", None) is not None:
            return "[стикер]"
        if "round" in mtype or "voice" in mtype:
            return "[голосовое]"
        if "animation" in mtype:
            return "[gif]"
        if doc is not None and getattr(doc, "video", None) is not None:
            return "[видео]"
        return "[документ]"
    if cls in ("MessageMediaVideo", "MessageServiceVideo"):
        return "[видео]"
    if cls in ("MessageMediaRoundVideo", "MessageServiceRoundVideo"):
        return "[видеосообщение]"
    if cls == "MessageMediaAudio":
        return "[аудио]"
    if cls == "MessageMediaPoll":
        return "[опрос]"
    if cls == "MessageMediaDice":
        return "[кости]"
    if cls == "MessageMediaContact":
        return "[контакт]"
    if cls == "MessageMediaGeo":
        return "[геопозиция]"
    if cls == "MessageMediaVenue":
        return "[место]"
    if cls == "MessageMediaInvoice":
        return "[счет]"
    if cls == "MessageMediaWebPage":
        return "[ссылка]"
    if cls == "MessageMediaStory":
        return "[история]"
    return "[медиа]"


def _service_action_label(action):
    acls = type(action).__name__ if action is not None else ""
    if "Invite" in acls:
        return "[пригласил в группу]"
    if "Join" in acls:
        return "[вошел в группу]"
    if "Photo" in acls:
        return "[изменил фото группы]"
    if "Title" in acls:
        return "[изменил название группы]"
    if action is not None:
        return "[служебное сообщение]"
    return None


def _format_message(msg, dc):
    """Строка вида '[2024-01-01 10:15] Иван: Привет'."""
    ts = datetime.fromtimestamp(int(msg.date) if msg.date else time.time())
    stamp = ts.strftime("%Y-%m-%d %H:%M")
    sender = _sender_name(msg, dc)
    text = str(msg.message) if msg.message is not None else ""
    label = _media_label(getattr(msg, "media", None))
    if label:
        text = f"{text} {label}".strip() if text else label
    if not text:
        text = _service_action_label(getattr(msg, "action", None)) or "[без текста]"
    return f"[{stamp}] {sender}: {text}"


def _safe_filename(title, date_str):
    """chat_НазваниеЧата_2026-10-09.txt; недопустимые символы заменяются на '_'."""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title)
    safe = re.sub(r"\s+", "_", safe).strip("._")[:80] or "chat"
    return f"chat_{safe}_{date_str}.txt"


# ---------------------------------------------------------------------------
# Механизм выгрузки
# ---------------------------------------------------------------------------

class Exporter:
    """Собирает историю открытого чата пачками и пишет её в txt-файл.

    Источник №1 — локальная база Telegram (ChatMessagesController): сюда
    складываются сообщения, приходящие с сервера, включая загруженные по
    loadHistory.  Источник №2 — запросы tl.messages.getHistory через
    loadHistory(...) пачками по BATCH_SIZE сообщений от новых к старым.
    Всё выполняется вне UI-потока, интерфейс не блокируется.
    """

    def __init__(self, plugin, dialog, dc):
        self.plugin = plugin
        self.dialog = dialog
        self.dc = dc
        self.peer = dialog.getPeer()
        self.chat_id = int(self.peer.id)
        self.title = _peer_title(dialog)
        self.cancelled = False
        self.exported_ids = set()
        self.entries = []          # [(date, line), ...] отсортированы по дате
        self.total_hint = 0        # оценка общего числа сообщений чата
        self.server_done = False   # сервер больше не отдаёт новые пачки
        self.awaiting_server = False
        self.last_progress_ts = time.time()
        self.last_path = None

    # ---------- запуск ----------

    def start(self):
        cmc = ChatMessagesController.getInstance(self.dc)
        # 1. сколько всего сообщений в этом чате (для счётчика прогресса)
        try:
            dialog = cmc.getDialog(self.peer)
            if dialog is not None:
                self.total_hint = int(dialog.topMessage)
        except Exception as e:
            self.plugin.log.warn(f"getDialog failed: {e}")
        # 2. всё, что уже закешировано на устройстве
        try:
            cached = cmc.getAllMessages(self.chat_id, 0, 0, 0, -1, True)
            self._collect_java_list(cached)
        except Exception as e:
            self.plugin.log.warn(f"getAllMessages failed: {e}")
        self.total_hint = max(self.total_hint, len(self.entries))
        # 3. докачиваем недостающее с сервера
        self._kick_history()
        self._tick()

    def cancel(self):
        self.cancelled = True

    # ---------- сбор сообщений ----------

    def _collect_java_list(self, msgs):
        if msgs is None:
            return
        try:
            n = len(msgs)
        except Exception:
            return
        for i in range(n):
            try:
                msg = msgs.get(i)
            except Exception:
                break
            if msg is not None:
                self._add_message(msg)

    def _add_message(self, msg):
        try:
            mid = int(msg.id)
        except Exception:
            return
        if mid in self.exported_ids:
            return
        self.exported_ids.add(mid)
        try:
            line = _format_message(msg, self.dc)
        except Exception:
            return
        key = int(msg.date) if msg.date else 0
        lo, hi = 0, len(self.entries)
        while lo < hi:
            mid_i = (lo + hi) // 2
            if self.entries[mid_i][0] <= key:
                lo = mid_i + 1
            else:
                hi = mid_i
        self.entries.insert(lo, (key, line))

    def _poll_database(self):
        """Подтягивает в экспорт сообщения, пришедшие из сети в локальную базу."""
        try:
            cmc = ChatMessagesController.getInstance(self.dc)
            oldest = min(self.exported_ids) if self.exported_ids else 0
            res = cmc.getAllMessages(self.chat_id, oldest, 0, 0, BATCH_SIZE, True)
            self._collect_java_list(res)
        except Exception as e:
            self.plugin.log.warn(f"poll db failed: {e}")

    # ---------- запрос истории с сервера пачками ----------

    def _kick_history(self):
        if self.cancelled or self.server_done or self.awaiting_server:
            return
        oldest = min(self.exported_ids) if self.exported_ids else 0
        # запрос идёт из UI-цикла тика, колбэк вернётся тоже в UI-поток
        self.awaiting_server = True
        try:
            ChatMessagesController.getInstance(self.dc).loadHistory(
                self.peer, oldest, 0, 0, BATCH_SIZE, True, False, True,
                self._on_history, self.chat_id,
            )
        except Exception as e:
            self.plugin.log.warn(f"loadHistory call failed: {e}")
            self.awaiting_server = False
            self.server_done = True

    def _on_history(self, resp, error):
        self.awaiting_server = False
        self.last_progress_ts = time.time()
        if self.cancelled:
            return
        if error is not None:
            self.plugin.log.warn(f"loadHistory error: {error}")
            self.server_done = True
            return
        before = len(self.exported_ids)
        try:
            mc = MessagesController.getInstance(self.dc)
            chats, users, msgs = resp.chats, resp.users, resp.messages
            for i in range(len(chats)):
                mc.putChatInCache(chats.get(i))
            for i in range(len(users)):
                mc.putUserInCache(users.get(i))
            for i in range(len(msgs)):
                m = msgs.get(i)
                if m is not None:
                    self._add_message(m)
        except Exception as e:
            self.plugin.log.warn(f"parse history batch failed: {e}")
        if len(self.exported_ids) - before <= 0:
            # дальше только дубликаты — история собрана
            self.server_done = True
            return
        self._kick_history()

    # ---------- прогресс и завершение ----------

    def _current_count(self):
        return len(self.entries)

    def _total_estimate(self):
        total = max(self.total_hint, len(self.entries))
        try:
            dialog = ChatMessagesController.getInstance(self.dc).getDialog(self.peer)
            if dialog is not None:
                total = max(total, int(dialog.topMessage))
        except Exception:
            pass
        return total

    def _tick(self):
        if self.cancelled:
            self.plugin.on_export_cancelled()
            return
        self._poll_database()
        finished = self.server_done and not self.awaiting_server
        if not finished:
            # страховка: если сервер молчит слишком долго — завершаем тем, что есть
            idle_sec = time.time() - self.last_progress_ts if self.last_progress_ts else 0
            if self.awaiting_server and idle_sec > MAX_WAIT_SEC:
                self.server_done = True
                self.awaiting_server = False
                finished = True
        if finished:
            self._finish()
            return
        self.last_progress_ts = time.time()
        self.plugin.show_progress(self._current_count(), self._total_estimate())
        runOnUIThread(lambda: self._tick(), POLL_STEP_MS, replace=False)

    def _finish(self):
        lines = [line for _, line in self.entries]
        count = len(lines)
        now = datetime.now()
        header = (
            f"Чат: {self.title}\n"
            f"Экспортировано: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"Всего сообщений: {count}\n"
            "─────────────────────────────\n"
        )
        content = (header + "\n".join(lines) + "\n").encode("utf-8")
        fname = _safe_filename(self.title, now.strftime("%Y-%m-%d"))
        try:
            path = self._save_to_downloads(fname, content)
        except Exception as e:
            self.plugin.log.error(f"save failed: {e}")
            self.plugin.on_export_error(str(e))
            return
        self.last_path = path
        self.plugin.on_export_done(path, count)

    def _save_to_downloads(self, fname, content):
        """Сохраняет файл в общую папку «Загрузки» устройства."""
        # Android 10+: MediaStore Downloads (через helper SDK, если доступен)
        try:
            try:
                from extera.android_utils import saveFileToDownloads
            except ImportError:
                from elyx.android import saveFileToDownloads
            uri = saveFileToDownloads(content, fname, "text/plain")
            if uri is not None:
                return str(uri)
        except Exception:
            pass
        # запасной вариант — прямая запись в каталог Download
        out = File(get_downloads_dir(), fname)
        write_bytes(out.getPath(), content)
        # уведомить медиа-сканер, чтобы файл сразу появился в «Загрузках»
        try:
            activity = get_current_activity()
            if activity is not None:
                from android.content import Intent
                from android.net import Uri
                scan = Intent(Intent.ACTION_MEDIA_SCANNER_SCAN_FILE)
                scan.setData(Uri.fromFile(out))
                activity.sendBroadcast(scan)
        except Exception:
            pass
        return out.getPath()


# ---------------------------------------------------------------------------
# Класс плагина
# ---------------------------------------------------------------------------

class AllChatsDownload(BasePlugin):
    """Точка входа: регистрирует пункт меню и управляет выгрузкой."""

    metadata = PluginMetadata(
        id="all_chats_download",
        name="All Chats Download",
        description=(
            "Кнопка 💾 «Скачать чат» в меню «три точки»: выгружает всю "
            "доступную историю переписки в .txt и сохраняет в «Загрузки»."
        ),
        author="exteraGram community",
        version="1.0.0",
    )

    def on_load(self):
        super().on_load()
        self._exporter = None
        self._progress = None       # активный диалог прогресса
        self._last_path = None

    # ---------- пункт в меню «три точки» ----------

    def get_chat_menu_items(self):
        return [
            {
                "title": "💾 Скачать чат",
                "hash": MENU_HASH,
                "callback": lambda *args: self._start_export(),
            }
        ]

    # ---------- запуск ----------

    def _start_export(self):
        @onAndroidUiThread
        def _():
            if self._exporter is not None:
                toast("Выгрузка уже выполняется")
                return
            activity = get_current_activity()
            if activity is None:
                toast("Не удалось определить открытый чат")
                return
            dialog = get_dialog_object(
                activity.getCurrentChatId(), activity.getCurrentChatTopicId()
            )
            if dialog is None or dialog.empty():
                toast("Не удалось определить открытый чат")
                return
            exporter = Exporter(self, dialog, self.current_account)
            self._exporter = exporter
            self.log.info(f"экспорт начат: {exporter.title}")
            try:
                exporter.start()
            except Exception as e:
                self.log.error(f"экспорт упал на старте: {e}")
                self._exporter = None
                self.on_export_error(str(e))

    # ---------- прогресс ----------

    def show_progress(self, current, total):
        """Обновляет счётчик «Загружено current / total», кнопка «Отменить»."""
        @onAndroidUiThread
        def _():
            from androidx.appcompat.app import AlertDialog
            activity = get_current_activity()
            if activity is None:
                return
            text = f"Загружено {current} / {total}"
            if self._progress is not None:
                try:
                    self._progress.setMessage(String(text))
                    return
                except Exception:
                    self._dismiss_progress()
            builder = AlertDialog.Builder(activity)
            builder.setTitle("Экспорт чата…")
            builder.setMessage(String(text))
            builder.setCancelable(False)
            builder.setNegativeButton(
                String("Отменить"),
                makeOnClickListener(lambda v: self._cancel_export()),
            )
            dlg = builder.create()
            dlg.show()
            self._progress = dlg

    def _cancel_export(self):
        if self._exporter is not None:
            self._exporter.cancel()

    def _dismiss_progress(self):
        dlg = self._progress
        self._progress = None
        if dlg is not None:
            try:
                dlg.dismiss()
            except Exception:
                pass

    # ---------- финальные колбэки ----------

    def on_export_cancelled(self):
        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            toast("Экспорт отменён")

    def on_export_error(self, err):
        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            toast("Ошибка экспорта: " + str(err)[:120])

    def on_export_done(self, path, count):
        self._last_path = path

        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            self._alert_done(path, count)

    # ---------- диалог по завершении ----------

    def _alert_done(self, path, count):
        activity = get_current_activity()
        message = (
            "Чат экспортирован, файл в Загрузках\n"
            f"Сообщений: {count}\n{path}"
        )
        if activity is None:
            toast("Чат экспортирован, файл в Загрузках")
            return
        from androidx.appcompat.app import AlertDialog
        builder = AlertDialog.Builder(activity)
        builder.setTitle("Готово")
        builder.setMessage(message)
        builder.setPositiveButton(String("ОК"), None)
        builder.setNeutralButton(
            String("Открыть папку"),
            makeOnClickListener(lambda v: self._open_downloads()),
        )
        builder.create().show()

    def _open_downloads(self):
        """Открывает «Загрузки» (или сам файл, если проводник недоступен)."""
        activity = get_current_activity()
        if activity is None:
            return
        from android.content import Intent
        from android.net import Uri
        from java.io import File

        # 1) файловый менеджер с общим каталогом Download
        try:
            intent = Intent("android.intent.action.VIEW")
            intent.setDataAndType(Uri.parse("content://downloads/my_downloads"), "*/*")
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(intent)
            return
        except Exception as e:
            self.log.warn(f"open downloads dir failed: {e}")
        # 2) открыть сам экспортированный файл во внешнем просмотрщике
        path = self._last_path
        if path:
            try:
                f = File(path)
                view = Intent(Intent.ACTION_VIEW)
                view.setDataAndType(Uri.fromFile(f), "text/plain")
                view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                view.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(view)
            except Exception as e:
                self.log.error(f"open file failed: {e}")
