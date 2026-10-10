"""all_chats_download - plagin exteraGram (SDK Elyx).

Dobavlyaet punkt  Skachat chat v menyu tri tochki vnutri chata ili gruppy.
Po nazhatiyu vygruzhaet vsyu dostupnuyu istoriyu perepiski v .txt fayl i sokhranyaet
ego v papku Zagruzki na ustroystve.

Format fayla:

    CHat: Nazvanie
    Eksportirovano: 2026-10-09 15:30
    Vsego soobshcheniy: 12453
    
    [2024-01-01 10:15] Ivan: Privet
    [2024-01-01 10:16] Mariya: Privet, kak dela?
    [2024-01-01 10:17] Ivan: [foto]

Imya fayla: chat_NazvanieCHata_2026-10-09.txt (nedopustimye simvoly -> "_").

Vo vremya vygruzki pokazyvaetsya progress Zagruzheno N / M s knopkoy Otmenit.
Posle sokhraneniya - dialog CHat eksportirovan, fayl v Zagruzkakh s knopkami
Otkryt papku i OK.
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

# --- Importy SDK: podderzhivaem i paket `extera`, i paket `elyx` -------------
try:
    from extera import BasePlugin, PluginMetadata
except ImportError:  # pragma: no cover - zavisit ot sborki SDK
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
except ImportError:  # pragma: no cover - alternativnye imena moduley
    from elyx.android import (
        makeOnClickListener,
        onAndroidUiThread,
        runOnUIThread,
        startActivity,
        toast,
    )
    from elyx.client import get_current_activity, get_dialog_object, get_user_id
    from elyx.files import get_downloads_dir, write_bytes


BATCH_SIZE = 100        # razmer pachki pri zaprose istorii (~100 soobshcheniy)
POLL_STEP_MS = 200      # period obnovleniya progressa / podtyazhki soobshcheniy
MAX_WAIT_SEC = 60       # maks. ozhidanie otveta servera pered finalizatsiey
MENU_HASH = 0x41D4A4AD  # unikalnyy id punkta menyu


# ---------------------------------------------------------------------------
# Vspomogatelnye funktsii
# ---------------------------------------------------------------------------

def _peer_title(dialog):
    """Nazvanie chata/gruppy/polzovatelya po DialogObject."""
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
        return f"CHat {int(dialog.getPeer().id)}"
    except Exception:
        return "CHat"


def _sender_name(msg, dc):
    """Imya otpravitelya soobshcheniya."""
    try:
        uid = int(msg.fromId)
        if uid == 0:
            return "Sistema"
        if uid == int(get_user_id(dc)):
            return "Vy"
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
    return "Uchastnik"


def _media_label(media):
    """Pometka dlya netekstovykh soobshcheniy: [foto], [video], [golosoe] i t.d."""
    if media is None:
        return None
    cls = type(media).__name__
    if cls in ("MessageMediaPhoto", "MessageServicePhoto"):
        return "[foto]"
    if cls == "MessageMediaDocument":
        doc = getattr(media, "document", None)
        mtype = str(getattr(doc, "mtype", "") or "") if doc is not None else ""
        if doc is not None and getattr(doc, "sticker", None) is not None:
            return "[stiker]"
        if "round" in mtype or "voice" in mtype:
            return "[golosovoe]"
        if "animation" in mtype:
            return "[gif]"
        if doc is not None and getattr(doc, "video", None) is not None:
            return "[video]"
        return "[dokument]"
    if cls in ("MessageMediaVideo", "MessageServiceVideo"):
        return "[video]"
    if cls in ("MessageMediaRoundVideo", "MessageServiceRoundVideo"):
        return "[videosoobshchenie]"
    if cls == "MessageMediaAudio":
        return "[audio]"
    if cls == "MessageMediaPoll":
        return "[opros]"
    if cls == "MessageMediaDice":
        return "[kosti]"
    if cls == "MessageMediaContact":
        return "[kontakt]"
    if cls == "MessageMediaGeo":
        return "[geopozitsiya]"
    if cls == "MessageMediaVenue":
        return "[mesto]"
    if cls == "MessageMediaInvoice":
        return "[schet]"
    if cls == "MessageMediaWebPage":
        return "[ssylka]"
    if cls == "MessageMediaStory":
        return "[istoriya]"
    return "[media]"


def _service_action_label(action):
    acls = type(action).__name__ if action is not None else ""
    if "Invite" in acls:
        return "[priglasil v gruppu]"
    if "Join" in acls:
        return "[voshel v gruppu]"
    if "Photo" in acls:
        return "[izmenil foto gruppy]"
    if "Title" in acls:
        return "[izmenil nazvanie gruppy]"
    if action is not None:
        return "[sluzhebnoe soobshchenie]"
    return None


def _format_message(msg, dc):
    """Stroka vida '[2024-01-01 10:15] Ivan: Privet'."""
    ts = datetime.fromtimestamp(int(msg.date) if msg.date else time.time())
    stamp = ts.strftime("%Y-%m-%d %H:%M")
    sender = _sender_name(msg, dc)
    text = str(msg.message) if msg.message is not None else ""
    label = _media_label(getattr(msg, "media", None))
    if label:
        text = f"{text} {label}".strip() if text else label
    if not text:
        text = _service_action_label(getattr(msg, "action", None)) or "[bez teksta]"
    return f"[{stamp}] {sender}: {text}"


def _safe_filename(title, date_str):
    """chat_NazvanieCHata_2026-10-09.txt; nedopustimye simvoly zamenyayutsya na '_'."""
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", title)
    safe = re.sub(r"\s+", "_", safe).strip("._")[:80] or "chat"
    return f"chat_{safe}_{date_str}.txt"


# ---------------------------------------------------------------------------
# Mekhanizm vygruzki
# ---------------------------------------------------------------------------

class Exporter:
    """Sobiraet istoriyu otkrytogo chata pachkami i pishet ee v txt-fayl.

    Istochnik 1 - lokalnaya baza Telegram (ChatMessagesController): syuda
    skladyvayutsya soobshcheniya, prikhodyashchie s servera, vklyuchaya zagruzhennye po
    loadHistory.  Istochnik 2 - zaprosy tl.messages.getHistory cherez
    loadHistory(...) pachkami po BATCH_SIZE soobshcheniy ot novykh k starym.
    Vse vypolnyaetsya vne UI-potoka, interfeys ne blokiruetsya.
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
        self.entries = []          # [(date, line), ...] otsortirovany po date
        self.total_hint = 0        # otsenka obshchego chisla soobshcheniy chata
        self.server_done = False   # server bolshe ne otdaet novye pachki
        self.awaiting_server = False
        self.last_progress_ts = time.time()
        self.last_path = None

    # ---------- zapusk ----------

    def start(self):
        cmc = ChatMessagesController.getInstance(self.dc)
        # 1. skolko vsego soobshcheniy v etom chate (dlya schetchika progressa)
        try:
            dialog = cmc.getDialog(self.peer)
            if dialog is not None:
                self.total_hint = int(dialog.topMessage)
        except Exception as e:
            self.plugin.log.warn(f"getDialog failed: {e}")
        # 2. vse, chto uzhe zakeshirovano na ustroystve
        try:
            cached = cmc.getAllMessages(self.chat_id, 0, 0, 0, -1, True)
            self._collect_java_list(cached)
        except Exception as e:
            self.plugin.log.warn(f"getAllMessages failed: {e}")
        self.total_hint = max(self.total_hint, len(self.entries))
        # 3. dokachivaem nedostayushchee s servera
        self._kick_history()
        self._tick()

    def cancel(self):
        self.cancelled = True

    # ---------- sbor soobshcheniy ----------

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
        """Podtyagivaet v eksport soobshcheniya, prishedshie iz seti v lokalnuyu bazu."""
        try:
            cmc = ChatMessagesController.getInstance(self.dc)
            oldest = min(self.exported_ids) if self.exported_ids else 0
            res = cmc.getAllMessages(self.chat_id, oldest, 0, 0, BATCH_SIZE, True)
            self._collect_java_list(res)
        except Exception as e:
            self.plugin.log.warn(f"poll db failed: {e}")

    # ---------- zapros istorii s servera pachkami ----------

    def _kick_history(self):
        if self.cancelled or self.server_done or self.awaiting_server:
            return
        oldest = min(self.exported_ids) if self.exported_ids else 0
        # zapros idet iz UI-tsikla tika, kolbek vernetsya tozhe v UI-potok
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
            # dalshe tolko dublikaty - istoriya sobrana
            self.server_done = True
            return
        self._kick_history()

    # ---------- progress i zavershenie ----------

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
            # strakhovka: esli server molchit slishkom dolgo - zavershaem tem, chto est
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
            f"\u0427\u0430\u0442: {self.title}\n"
            f"\u042d\u043a\u0441\u043f\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u043e: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"\u0412\u0441\u0435\u0433\u043e \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439: {count}\n"
            "\n"
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
        """Sokhranyaet fayl v obshchuyu papku Zagruzki ustroystva."""
        # Android 10+: MediaStore Downloads (cherez helper SDK, esli dostupen)
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
        # zapasnoy variant - pryamaya zapis v katalog Download
        out = File(get_downloads_dir(), fname)
        write_bytes(out.getPath(), content)
        # uvedomit media-skaner, chtoby fayl srazu poyavilsya v Zagruzkakh
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
# Klass plagina
# ---------------------------------------------------------------------------

class AllChatsDownload(BasePlugin):
    """Tochka vkhoda: registriruet punkt menyu i upravlyaet vygruzkoy."""

    metadata = PluginMetadata(
        id="all_chats_download",
        name="All Chats Download",
        description=(
            "Knopka  Skachat chat v menyu tri tochki: vygruzhaet vsyu "
            "dostupnuyu istoriyu perepiski v .txt i sokhranyaet v Zagruzki."
        ),
        author="exteraGram community",
        version="1.0.0",
    )

    def on_load(self):
        super().on_load()
        self._exporter = None
        self._progress = None       # aktivnyy dialog progressa
        self._last_path = None

    # ---------- punkt v menyu tri tochki ----------

    def get_chat_menu_items(self):
        return [
            {
                "title": "\U0001F4BE Skachat chat",
                "hash": MENU_HASH,
                "callback": lambda *args: self._start_export(),
            }
        ]

    # ---------- zapusk ----------

    def _start_export(self):
        @onAndroidUiThread
        def _():
            if self._exporter is not None:
                toast("Vygruzka uzhe vypolnyaetsya")
                return
            activity = get_current_activity()
            if activity is None:
                toast("Ne udalos opredelit otkrytyy chat")
                return
            dialog = get_dialog_object(
                activity.getCurrentChatId(), activity.getCurrentChatTopicId()
            )
            if dialog is None or dialog.empty():
                toast("Ne udalos opredelit otkrytyy chat")
                return
            exporter = Exporter(self, dialog, self.current_account)
            self._exporter = exporter
            self.log.info(f"eksport nachat: {exporter.title}")
            try:
                exporter.start()
            except Exception as e:
                self.log.error(f"eksport upal na starte: {e}")
                self._exporter = None
                self.on_export_error(str(e))

    # ---------- progress ----------

    def show_progress(self, current, total):
        """Obnovlyaet schetchik Zagruzheno current / total, knopka Otmenit."""
        @onAndroidUiThread
        def _():
            from androidx.appcompat.app import AlertDialog
            activity = get_current_activity()
            if activity is None:
                return
            text = f"Zagruzheno {current} / {total}"
            if self._progress is not None:
                try:
                    self._progress.setMessage(String(text))
                    return
                except Exception:
                    self._dismiss_progress()
            builder = AlertDialog.Builder(activity)
            builder.setTitle("Eksport chata...")
            builder.setMessage(String(text))
            builder.setCancelable(False)
            builder.setNegativeButton(
                String("Otmenit"),
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

    # ---------- finalnye kolbeki ----------

    def on_export_cancelled(self):
        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            toast("Eksport otmenen")

    def on_export_error(self, err):
        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            toast("Oshibka eksporta: " + str(err)[:120])

    def on_export_done(self, path, count):
        self._last_path = path

        @onAndroidUiThread
        def _():
            self._dismiss_progress()
            self._exporter = None
            self._alert_done(path, count)

    # ---------- dialog po zavershenii ----------

    def _alert_done(self, path, count):
        activity = get_current_activity()
        message = (
            "CHat eksportirovan, fayl v Zagruzkakh\n"
            f"Soobshcheniy: {count}\n{path}"
        )
        if activity is None:
            toast("CHat eksportirovan, fayl v Zagruzkakh")
            return
        from androidx.appcompat.app import AlertDialog
        builder = AlertDialog.Builder(activity)
        builder.setTitle("Gotovo")
        builder.setMessage(message)
        builder.setPositiveButton(String("OK"), None)
        builder.setNeutralButton(
            String("Otkryt papku"),
            makeOnClickListener(lambda v: self._open_downloads()),
        )
        builder.create().show()

    def _open_downloads(self):
        """Otkryvaet Zagruzki (ili sam fayl, esli provodnik nedostupen)."""
        activity = get_current_activity()
        if activity is None:
            return
        from android.content import Intent
        from android.net import Uri
        from java.io import File

        # 1) faylovyy menedzher s obshchim katalogom Download
        try:
            intent = Intent("android.intent.action.VIEW")
            intent.setDataAndType(Uri.parse("content://downloads/my_downloads"), "*/*")
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(intent)
            return
        except Exception as e:
            self.log.warn(f"open downloads dir failed: {e}")
        # 2) otkryt sam eksportirovannyy fayl vo vneshnem prosmotrshchike
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
