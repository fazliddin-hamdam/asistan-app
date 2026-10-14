#!/usr/bin/env python3
"""
Kişisel Telegram Muhasebe Asistanı
==================================

Android Termux üzerinde 7/24 çalışan, yalnızca sahibine yanıt veren,
verileri Google Sheets'e kaydeden kişisel muhasebe botu.

Kullanım:
    "A101 500 TL"          -> 500 TL gider, açıklama "A101"
    "Banyo musluğu 400"    -> 400 TL gider, açıklama "Banyo musluğu"
    "+15000 maaş"          -> 15.000 TL gelir (başta/tutarda "+" veya "gelir" kelimesi)
    "gelir kira 8000"      -> 8.000 TL gelir

Komutlar:
    /yenisayfa [ad]  Yeni sekme (kasa) oluşturur
    /sayfalar        Mevcut sekmeleri listeler
    /ozet            Her sekmenin gelir/gider/net toplamı
    /iptal           Devam eden işlemi iptal eder
    /yardim          Yardım metni
"""

import asyncio
import html
import logging
import re
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import gspread
import requests
from gspread.utils import ValueRenderOption
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

# =============================================================================
# AYARLAR  —  >>> BU BÖLÜMDEKİ İŞARETLİ ALANLARI KENDİ BİLGİLERİNİZLE DOLDURUN <<<
# =============================================================================

# >>> PLACEHOLDER 1: @BotFather'dan aldığınız bot token'ı
TELEGRAM_TOKEN = "BURAYA_TELEGRAM_BOT_TOKENINI_YAZIN"

# >>> PLACEHOLDER 2: Kendi Telegram kullanıcı ID'niz (sayı, tırnaksız). @userinfobot'tan öğrenebilirsiniz.
ALLOWED_USER_ID = 0

# >>> PLACEHOLDER 3: Google Sheets dosyanızın ID'si.
# URL'deki /d/ ile /edit arasındaki kısım:
# https://docs.google.com/spreadsheets/d/<<<BU_KISIM>>>/edit
SPREADSHEET_ID = "BURAYA_GOOGLE_SHEETS_ID_YAZIN"

# >>> PLACEHOLDER 4: Service Account JSON anahtar dosyasının yolu (main.py ile aynı klasörde olabilir)
CREDENTIALS_FILE = "credentials.json"

# Saat dilimi: Türkiye 2016'dan beri sabit UTC+3 (yaz saati yok).
# Sabit ofset kullanıyoruz; Termux'ta tz veritabanı sorunlarından etkilenmez.
TIMEZONE = timezone(timedelta(hours=3), "TRT")
DATE_FORMAT = "%d-%m-%Y %H:%M"  # GG-AA-YYYY SS:DD

# Her yeni sekmenin ilk satırına yazılacak başlıklar (A, B, C, D sütunları)
HEADERS = ["Tarih", "Tür", "Tutar", "Açıklama"]

# Buton listesinde gösterilmeyecek sekmeler (örn. {"Sayfa1", "Ayarlar"})
HIDDEN_SHEETS: set[str] = set()

# Klavyede bir satırdaki buton sayısı
BUTTONS_PER_ROW = 2

# Bot başladığında size "çalışıyorum" mesajı göndersin mi?
NOTIFY_ON_START = True

# Aynı anda bekleyen (sekme seçilmemiş) en fazla kayıt sayısı
MAX_PENDING_ENTRIES = 20

LOG_FILE = "bot.log"

# =============================================================================
# LOGLAMA
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)
# httpx her polling isteğini INFO seviyesinde loglar; gürültüyü kes.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("muhasebe")

# ConversationHandler'a CallbackQueryHandler giriş noktası eklediğimiz için PTB
# "per_message" uyarısı verir; bizim kullanımımızda beklenen bir durum.
warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)

# =============================================================================
# YARDIMCI FONKSİYONLAR
# =============================================================================

KIND_EXPENSE = "Gider"
KIND_INCOME = "Gelir"


def now_str() -> str:
    """Şu anki zamanı GG-AA-YYYY SS:DD biçiminde döndürür."""
    return datetime.now(TIMEZONE).strftime(DATE_FORMAT)


def fmt_money(value: float) -> str:
    """1250.5 -> '1.250,50 ₺' (Türkçe biçim)."""
    s = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{s} ₺"


def esc(text: str) -> str:
    """Kullanıcı metnini HTML parse_mode için güvenli hâle getirir."""
    return html.escape(str(text), quote=False)


# =============================================================================
# METİN AYRIŞTIRICI (PARSER)
# =============================================================================

CURRENCY_WORDS = {"tl", "try", "₺", "lira", "tl."}
INCOME_KEYWORDS = {"gelir", "+gelir"}
EXPENSE_KEYWORDS = {"gider", "harcama"}

# İsteğe bağlı işaret, isteğe bağlı ₺ ön eki, rakamla başlayan sayı, isteğe bağlı birim son eki.
# "A101" gibi harfle başlayan kelimeler eşleşmez; böylece tutar sanılmaz.
AMOUNT_RE = re.compile(r"^([+-]?)₺?(\d[\d.,]*)(tl|try|₺)?$", re.IGNORECASE)


@dataclass
class ParsedEntry:
    amount: float
    kind: str  # "Gider" veya "Gelir"
    description: str


def parse_number(raw: str) -> float | None:
    """
    Türkçe ve İngilizce sayı biçimlerini float'a çevirir.
        "500" -> 500, "1.250" -> 1250, "1.250,50" -> 1250.5,
        "1,250.50" -> 1250.5, "99,90" -> 99.9, "12.5" -> 12.5
    """
    if "." in raw and "," in raw:
        # Sonda olan ayırıcı ondalık ayırıcıdır.
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            raw = f"{parts[0]}.{parts[1]}"  # 99,90 -> ondalık
        else:
            raw = raw.replace(",", "")  # 1,250 -> binlik
    elif "." in raw:
        parts = raw.split(".")
        if len(parts) > 2 or len(parts[1]) == 3:
            raw = raw.replace(".", "")  # 1.250 / 1.250.000 -> binlik (TR alışkanlığı)
        # aksi hâlde 12.5 gibi ondalık; olduğu gibi bırak
    try:
        return float(raw)
    except ValueError:
        return None


def parse_entry(text: str) -> ParsedEntry | None:
    """
    Serbest metinden tutar, tür ve açıklama çıkarır. Tutar bulunamazsa None döner.

    Kural: Birim (TL/₺) ile işaretlenmiş sayı varsa o, yoksa metindeki SON sayı tutardır.
    Böylece "2 adet ampul 150" -> 150 TL, "150 TL 2 adet ampul" -> 150 TL olur.
    """
    tokens = text.split()
    if not tokens:
        return None

    kind = KIND_EXPENSE
    first = tokens[0].lower()
    if first in INCOME_KEYWORDS:
        kind, tokens = KIND_INCOME, tokens[1:]
    elif first in EXPENSE_KEYWORDS:
        tokens = tokens[1:]

    # (indeks, değer, işaret, birim_var_mı, birim_sonraki_kelimede_mi)
    candidates: list[tuple[int, float, str, bool, bool]] = []
    for i, tok in enumerate(tokens):
        m = AMOUNT_RE.match(tok.rstrip(".,;:!?"))
        if not m:
            continue
        value = parse_number(m.group(2))
        if value is None or value <= 0:
            continue
        next_is_currency = i + 1 < len(tokens) and tokens[i + 1].lower() in CURRENCY_WORDS
        has_currency = bool(m.group(3)) or tok.startswith(("₺", "+₺", "-₺")) or next_is_currency
        candidates.append((i, value, m.group(1), has_currency, next_is_currency))

    if not candidates:
        return None

    with_currency = [c for c in candidates if c[3]]
    idx, value, sign, _, currency_next = (with_currency or candidates)[-1]

    if sign == "+":
        kind = KIND_INCOME
    elif sign == "-":
        kind = KIND_EXPENSE

    skip = {idx, idx + 1} if currency_next else {idx}
    description = " ".join(t for i, t in enumerate(tokens) if i not in skip).strip()
    if description.startswith("+"):  # "+ maaş 15000" gibi yazımlar
        kind = KIND_INCOME
        description = description[1:].strip()

    return ParsedEntry(amount=round(value, 2), kind=kind, description=description or "(açıklama yok)")


# =============================================================================
# GOOGLE SHEETS KATMANI
# =============================================================================


class SheetExistsError(Exception):
    """Aynı isimde sekme zaten varsa fırlatılır."""


class SheetsRepository:
    """
    gspread senkron (bloklayan) bir kütüphanedir. Bu sınıftaki metotlar
    bot tarafında asyncio.to_thread() ile ayrı thread'de çağrılır; böylece
    Sheets isteği sürerken bot donmaz.
    """

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, credentials_file: str, spreadsheet_id: str):
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._spreadsheet: gspread.Spreadsheet | None = None

    # --- bağlantı -----------------------------------------------------------

    def _connect(self) -> gspread.Spreadsheet:
        if self._spreadsheet is None:
            logger.info("Google Sheets'e bağlanılıyor...")
            client = gspread.service_account(filename=self._credentials_file)
            self._spreadsheet = client.open_by_key(self._spreadsheet_id)
            logger.info("Bağlandı: %s", self._spreadsheet.title)
        return self._spreadsheet

    def _with_retry(self, fn):
        """Geçici ağ/kota hatalarında bağlantıyı sıfırlayıp bir kez daha dener."""
        try:
            return fn(self._connect())
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.warning("Ağ hatası, yeniden deneniyor: %s", e)
        except gspread.exceptions.APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in self.RETRYABLE_STATUS:
                raise
            logger.warning("Sheets API geçici hata (%s), yeniden deneniyor", status)
        self._spreadsheet = None
        time.sleep(2)
        return fn(self._connect())

    # --- işlemler -----------------------------------------------------------

    def check_connection(self) -> str:
        return self._with_retry(lambda sh: sh.title)

    def list_sheet_titles(self) -> list[str]:
        return self._with_retry(
            lambda sh: [ws.title for ws in sh.worksheets() if ws.title not in HIDDEN_SHEETS]
        )

    def create_sheet(self, title: str) -> None:
        def _create(sh: gspread.Spreadsheet):
            existing = {ws.title.casefold() for ws in sh.worksheets()}
            if title.casefold() in existing:
                raise SheetExistsError(title)
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(HEADERS))
            ws.update([HEADERS], "A1")
            # Görsel düzenlemeler: başarısız olsa da sekme kullanılabilir.
            try:
                ws.format("A1:D1", {"textFormat": {"bold": True}})
                ws.freeze(rows=1)
            except Exception as e:  # noqa: BLE001
                logger.warning("Başlık biçimlendirilemedi (önemsiz): %s", e)

        self._with_retry(_create)

    def append_entry(self, title: str, entry: ParsedEntry, date_str: str) -> None:
        row = [date_str, entry.kind, entry.amount, entry.description]
        # RAW: tarih metin olarak, tutar sayı olarak yazılır (yerel ayar yorumu yok).
        self._with_retry(
            lambda sh: sh.worksheet(title).append_row(
                row, value_input_option="RAW", table_range="A1"
            )
        )

    def summarize(self) -> list[tuple[str, float, float]]:
        """Her sekme için (ad, toplam_gelir, toplam_gider) döndürür."""

        def _summarize(sh: gspread.Spreadsheet):
            result = []
            for ws in sh.worksheets():
                if ws.title in HIDDEN_SHEETS:
                    continue
                rows = ws.get("A2:D", value_render_option=ValueRenderOption.unformatted)
                income = expense = 0.0
                for r in rows:
                    if len(r) < 3:
                        continue
                    amount = r[2]
                    if isinstance(amount, str):
                        amount = parse_number(amount.replace("₺", "").strip())
                    if not isinstance(amount, (int, float)):
                        continue
                    if str(r[1]).strip().lower() == KIND_INCOME.lower():
                        income += amount
                    else:
                        expense += amount
                result.append((ws.title, income, expense))
            return result

        return self._with_retry(_summarize)


def describe_sheets_error(e: Exception) -> str:
    """Sheets hatalarını kullanıcıya anlaşılır mesaja çevirir."""
    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return "Bu sekme artık yok (silinmiş ya da adı değişmiş olabilir)."
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "Tablo bulunamadı. SPREADSHEET_ID'yi ve tablonun service account ile paylaşıldığını kontrol edin."
    if isinstance(e, gspread.exceptions.APIError):
        status = getattr(getattr(e, "response", None), "status_code", "?")
        if status == 403:
            return "Erişim reddedildi (403). Tabloyu service account e-postasıyla 'Düzenleyen' olarak paylaşın."
        if status == 429:
            return "Google kota sınırı aşıldı, bir dakika sonra tekrar deneyin."
        return f"Google Sheets API hatası ({status})."
    if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "İnternet bağlantısı yok ya da Google'a ulaşılamıyor."
    return f"Beklenmeyen hata: {type(e).__name__}"


# =============================================================================
# BOT DURUMU (bekleyen kayıtlar)
# =============================================================================


@dataclass
class PendingEntry:
    entry: ParsedEntry
    date_str: str
    sheets: list[str]  # Butonların gösterildiği andaki sekme listesi (callback'te indeksle seçilir)
    chat_id: int
    message_id: int | None = None  # Butonlu bot mesajının ID'si
    created: float = field(default_factory=time.time)


def get_repo(context: ContextTypes.DEFAULT_TYPE) -> SheetsRepository:
    return context.application.bot_data["repo"]


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> dict[str, PendingEntry]:
    return context.user_data.setdefault("pending", {})


def add_pending(context: ContextTypes.DEFAULT_TYPE, pending: PendingEntry) -> str:
    store = get_pending(context)
    # Çok eski kayıtları temizle (dict ekleme sırasını korur; ilk eleman en eskisi)
    while len(store) >= MAX_PENDING_ENTRIES:
        store.pop(next(iter(store)))
    entry_id = uuid.uuid4().hex[:8]
    store[entry_id] = pending
    return entry_id


def entry_summary(entry: ParsedEntry) -> str:
    icon = "🟢" if entry.kind == KIND_INCOME else "🔴"
    return f"{icon} <b>{entry.kind}</b>: {fmt_money(entry.amount)}\n📝 {esc(entry.description)}"


def build_sheet_keyboard(entry_id: str, titles: list[str]) -> InlineKeyboardMarkup:
    """
    Sekme butonları. callback_data 64 bayt sınırı olduğundan sekme adını değil,
    bekleyen kaydın kısa ID'sini ve sekmenin listedeki indeksini taşır.
    """
    rows, row = [], []
    for idx, title in enumerate(titles):
        row.append(InlineKeyboardButton(title, callback_data=f"kaydet|{entry_id}|{idx}"))
        if len(row) == BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("➕ Yeni sayfa", callback_data=f"yeni|{entry_id}"),
            InlineKeyboardButton("❌ İptal", callback_data=f"iptal|{entry_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


# =============================================================================
# GÜVENLİK
# =============================================================================


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Tüm güncellemelerden önce (group=-1) çalışır. Kullanıcı ALLOWED_USER_ID
    değilse güncellemeyi sessizce durdurur: hiçbir yanıt verilmez.
    """
    user = update.effective_user
    if user is None or user.id != ALLOWED_USER_ID:
        if user is not None:
            logger.warning("Yetkisiz erişim denemesi: id=%s username=%s", user.id, user.username)
        raise ApplicationHandlerStop


# =============================================================================
# KOMUTLAR
# =============================================================================

HELP_TEXT = (
    "<b>💰 Muhasebe Asistanı</b>\n\n"
    "<b>Kayıt eklemek için</b> doğrudan yazın:\n"
    "• <code>A101 500 TL</code>\n"
    "• <code>Banyo musluğu 400</code>\n"
    "• <code>Market 1.250,50</code>\n"
    "• <code>+15000 maaş</code> veya <code>gelir kira 8000</code> → gelir\n\n"
    "Ardından kaydın gideceği sekmeyi butonlardan seçin.\n\n"
    "<b>Komutlar</b>\n"
    "/yenisayfa – Yeni sekme (kasa) oluştur\n"
    "/sayfalar – Sekmeleri listele\n"
    "/ozet – Sekme bazında gelir/gider özeti\n"
    "/iptal – Devam eden işlemi iptal et\n"
    "/yardim – Bu mesaj"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP_TEXT)


async def cmd_sayfalar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        titles = await asyncio.to_thread(get_repo(context).list_sheet_titles)
    except Exception as e:  # noqa: BLE001
        logger.exception("Sekmeler alınamadı")
        await update.message.reply_text(f"⚠️ {describe_sheets_error(e)}")
        return
    if not titles:
        await update.message.reply_text("Henüz sekme yok. /yenisayfa ile oluşturabilirsiniz.")
        return
    lines = "\n".join(f"• {esc(t)}" for t in titles)
    await update.message.reply_html(f"<b>📑 Sekmeler</b>\n{lines}")


async def cmd_ozet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Hesaplanıyor...")
    try:
        summary = await asyncio.to_thread(get_repo(context).summarize)
    except Exception as e:  # noqa: BLE001
        logger.exception("Özet alınamadı")
        await msg.edit_text(f"⚠️ {describe_sheets_error(e)}")
        return
    if not summary:
        await msg.edit_text("Henüz sekme yok.")
        return

    parts = ["<b>📊 Özet</b>"]
    total_in = total_out = 0.0
    for title, income, expense in summary:
        total_in += income
        total_out += expense
        parts.append(
            f"\n<b>{esc(title)}</b>\n"
            f"  🟢 Gelir: {fmt_money(income)}\n"
            f"  🔴 Gider: {fmt_money(expense)}\n"
            f"  ⚖️ Net: {fmt_money(income - expense)}"
        )
    parts.append(
        f"\n<b>TOPLAM</b>\n"
        f"  🟢 {fmt_money(total_in)}  🔴 {fmt_money(total_out)}  ⚖️ {fmt_money(total_in - total_out)}"
    )
    await msg.edit_text("\n".join(parts), parse_mode="HTML")


async def cmd_iptal_idle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Konuşma dışında /iptal yazılırsa."""
    await update.message.reply_text("İptal edilecek bir işlem yok.")


# =============================================================================
# HIZLI VERİ GİRİŞİ
# =============================================================================


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Serbest metni ayrıştırır ve sekme seçim butonlarını gösterir."""
    text = update.message.text.strip()
    entry = parse_entry(text)
    if entry is None:
        await update.message.reply_html(
            "🤔 Tutar bulamadım. Örnek: <code>A101 500 TL</code>\nYardım için /yardim"
        )
        return

    try:
        titles = await asyncio.to_thread(get_repo(context).list_sheet_titles)
    except Exception as e:  # noqa: BLE001
        logger.exception("Sekmeler alınamadı")
        await update.message.reply_text(f"⚠️ {describe_sheets_error(e)}")
        return

    pending = PendingEntry(
        entry=entry,
        date_str=now_str(),  # Kayıt zamanı: mesajın geldiği an
        sheets=titles,
        chat_id=update.effective_chat.id,
    )
    entry_id = add_pending(context, pending)

    prompt = "📂 Hangi sekmeye kaydedilsin?" if titles else "📂 Henüz sekme yok, yeni bir tane oluşturun:"
    sent = await update.message.reply_html(
        f"{entry_summary(entry)}\n\n{prompt}",
        reply_markup=build_sheet_keyboard(entry_id, titles),
    )
    pending.message_id = sent.message_id


async def on_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sekme butonuna basılınca kaydı ilgili sekmeye yazar."""
    query = update.callback_query
    await query.answer()

    try:
        _, entry_id, idx_str = query.data.split("|")
        idx = int(idx_str)
    except ValueError:
        logger.error("Geçersiz callback verisi: %s", query.data)
        return

    store = get_pending(context)
    # Çift tıklamada iki kez yazmamak için kaydı hemen listeden çıkar.
    pending = store.pop(entry_id, None)
    if pending is None:
        await query.edit_message_text("⌛ Bu kayıt artık geçerli değil (zaten kaydedilmiş ya da bot yeniden başlamış).")
        return
    if not 0 <= idx < len(pending.sheets):
        await query.edit_message_text("⚠️ Geçersiz sekme seçimi.")
        return

    title = pending.sheets[idx]
    try:
        await asyncio.to_thread(get_repo(context).append_entry, title, pending.entry, pending.date_str)
    except Exception as e:  # noqa: BLE001
        logger.exception("Kayıt yazılamadı")
        store[entry_id] = pending  # Tekrar denenebilsin diye geri koy
        await query.edit_message_text(
            f"{entry_summary(pending.entry)}\n\n⚠️ {esc(describe_sheets_error(e))}\nTekrar deneyin:",
            parse_mode="HTML",
            reply_markup=build_sheet_keyboard(entry_id, pending.sheets),
        )
        return

    logger.info("Kaydedildi -> %s: %s %s %s", title, pending.entry.kind, pending.entry.amount, pending.entry.description)
    await query.edit_message_text(
        f"✅ <b>{esc(title)}</b> sekmesine kaydedildi\n\n{entry_summary(pending.entry)}\n🕒 {pending.date_str}",
        parse_mode="HTML",
    )


async def on_cancel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split("|", 1)[1]
    get_pending(context).pop(entry_id, None)
    await query.edit_message_text("❌ Kayıt iptal edildi.")


# =============================================================================
# YENİ SAYFA (SEKME) OLUŞTURMA — ConversationHandler
# =============================================================================

WAITING_SHEET_NAME = 1
MAX_SHEET_NAME_LEN = 100  # Google Sheets sekme adı sınırı


async def new_sheet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/yenisayfa komutu. '/yenisayfa Borclar' şeklinde ad doğrudan da verilebilir."""
    context.user_data.pop("after_create_entry", None)
    if context.args:
        return await _create_sheet_flow(update, context, " ".join(context.args))
    await update.message.reply_html(
        "📄 Yeni sekmenin adını yazın (örn. <code>Ortak_Is_Ahmet</code>, <code>Borclar</code>)\n"
        "Vazgeçmek için /iptal"
    )
    return WAITING_SHEET_NAME


async def new_sheet_from_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Kayıt klavyesindeki '➕ Yeni sayfa' butonu: sekme oluşturulunca kayıt oraya yazılır."""
    query = update.callback_query
    await query.answer()
    entry_id = query.data.split("|", 1)[1]
    if entry_id not in get_pending(context):
        await query.edit_message_text("⌛ Bu kayıt artık geçerli değil.")
        return ConversationHandler.END
    context.user_data["after_create_entry"] = entry_id
    await query.message.reply_html(
        "📄 Yeni sekmenin adını yazın. Oluşturulunca bu kayıt oraya eklenecek.\nVazgeçmek için /iptal"
    )
    return WAITING_SHEET_NAME


async def new_sheet_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _create_sheet_flow(update, context, update.message.text)


async def _create_sheet_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_name: str) -> int:
    name = " ".join(raw_name.split())  # Fazla boşlukları temizle
    if not name:
        await update.message.reply_text("Ad boş olamaz, tekrar yazın (veya /iptal).")
        return WAITING_SHEET_NAME
    if len(name) > MAX_SHEET_NAME_LEN:
        await update.message.reply_text(f"Ad en fazla {MAX_SHEET_NAME_LEN} karakter olabilir, tekrar yazın.")
        return WAITING_SHEET_NAME

    repo = get_repo(context)
    try:
        await asyncio.to_thread(repo.create_sheet, name)
    except SheetExistsError:
        await update.message.reply_html(
            f"⚠️ <b>{esc(name)}</b> adında bir sekme zaten var. Başka bir ad yazın (veya /iptal)."
        )
        return WAITING_SHEET_NAME
    except Exception as e:  # noqa: BLE001
        logger.exception("Sekme oluşturulamadı")
        await update.message.reply_text(f"⚠️ Sekme oluşturulamadı: {describe_sheets_error(e)}")
        context.user_data.pop("after_create_entry", None)
        return ConversationHandler.END

    logger.info("Yeni sekme oluşturuldu: %s", name)
    await update.message.reply_html(
        f"✅ <b>{esc(name)}</b> sekmesi oluşturuldu.\nBaşlıklar: {esc(', '.join(HEADERS))}"
    )

    # Butondan gelindiyse bekleyen kaydı yeni sekmeye yaz
    entry_id = context.user_data.pop("after_create_entry", None)
    pending = get_pending(context).pop(entry_id, None) if entry_id else None
    if pending:
        try:
            await asyncio.to_thread(repo.append_entry, name, pending.entry, pending.date_str)
        except Exception as e:  # noqa: BLE001
            logger.exception("Kayıt yeni sekmeye yazılamadı")
            await update.message.reply_text(
                f"⚠️ Kayıt yazılamadı: {describe_sheets_error(e)}\nMesajı tekrar göndererek yeniden deneyin."
            )
            return ConversationHandler.END
        done_text = (
            f"✅ <b>{esc(name)}</b> sekmesine kaydedildi\n\n{entry_summary(pending.entry)}\n🕒 {pending.date_str}"
        )
        try:
            # Orijinal butonlu mesajı güncelle (butonları kaldır)
            await context.bot.edit_message_text(
                done_text, chat_id=pending.chat_id, message_id=pending.message_id, parse_mode="HTML"
            )
        except Exception:  # noqa: BLE001  (mesaj çok eski/silinmiş olabilir)
            await update.message.reply_html(done_text)

    return ConversationHandler.END


async def new_sheet_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("after_create_entry", None)
    await update.message.reply_text("İptal edildi. (Varsa önceki kaydın butonları hâlâ kullanılabilir.)")
    return ConversationHandler.END


async def new_sheet_other_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("after_create_entry", None)
    await update.message.reply_text("Sekme oluşturma iptal edildi. Komutu tekrar gönderin.")
    return ConversationHandler.END


# =============================================================================
# HATA YÖNETİMİ VE BAŞLANGIÇ
# =============================================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Yakalanmamış tüm hataları loglar; mümkünse kullanıcıya kısa bilgi verir."""
    logger.error("Yakalanmamış hata", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_user \
            and update.effective_user.id == ALLOWED_USER_ID:
        try:
            await context.bot.send_message(update.effective_chat.id, "⚠️ Bir hata oluştu, ayrıntılar bot.log'da.")
        except Exception:  # noqa: BLE001
            pass


async def on_startup(application: Application) -> None:
    """Bot başlarken: komut menüsünü ayarla, Sheets bağlantısını test et, sahibine haber ver."""
    await application.bot.set_my_commands(
        [
            BotCommand("yenisayfa", "Yeni sekme oluştur"),
            BotCommand("sayfalar", "Sekmeleri listele"),
            BotCommand("ozet", "Gelir/gider özeti"),
            BotCommand("iptal", "İşlemi iptal et"),
            BotCommand("yardim", "Yardım"),
        ]
    )
    repo: SheetsRepository = application.bot_data["repo"]
    try:
        title = await asyncio.to_thread(repo.check_connection)
        status = f"✅ Google Sheets bağlı: {title}"
    except Exception as e:  # noqa: BLE001
        logger.exception("Başlangıçta Sheets bağlantısı kurulamadı")
        status = f"⚠️ Google Sheets bağlantısı kurulamadı: {describe_sheets_error(e)}"
    logger.info(status)

    if NOTIFY_ON_START:
        try:
            await application.bot.send_message(ALLOWED_USER_ID, f"🤖 Bot çalışıyor.\n{status}")
        except Exception as e:  # noqa: BLE001
            # Bota hiç /start yazmadıysanız Telegram bot'un size mesaj atmasına izin vermez.
            logger.warning("Başlangıç bildirimi gönderilemedi (bota önce /start yazın): %s", e)


def validate_config() -> None:
    errors = []
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("BURAYA"):
        errors.append("TELEGRAM_TOKEN doldurulmamış.")
    if not isinstance(ALLOWED_USER_ID, int) or ALLOWED_USER_ID <= 0:
        errors.append("ALLOWED_USER_ID doldurulmamış (sayı olmalı).")
    if not SPREADSHEET_ID or SPREADSHEET_ID.startswith("BURAYA"):
        errors.append("SPREADSHEET_ID doldurulmamış.")
    try:
        with open(CREDENTIALS_FILE, encoding="utf-8"):
            pass
    except OSError:
        errors.append(f"'{CREDENTIALS_FILE}' bulunamadı (Service Account JSON anahtarı).")
    if errors:
        for err in errors:
            logger.critical("AYAR HATASI: %s", err)
        sys.exit("main.py başındaki AYARLAR bölümünü düzenleyin.")


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(on_startup).build()
    application.bot_data["repo"] = SheetsRepository(CREDENTIALS_FILE, SPREADSHEET_ID)

    # 1) Güvenlik kapısı — her şeyden önce çalışır
    application.add_handler(TypeHandler(Update, guard), group=-1)

    # 2) Yeni sekme konuşması — genel metin işleyicisinden ÖNCE eklenmeli
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("yenisayfa", new_sheet_start),
                CallbackQueryHandler(new_sheet_from_button, pattern=r"^yeni\|"),
            ],
            states={
                WAITING_SHEET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_sheet_receive_name)],
            },
            fallbacks=[
                CommandHandler("iptal", new_sheet_cancel),
                MessageHandler(filters.COMMAND, new_sheet_other_command),
            ],
            allow_reentry=True,
        )
    )

    # 3) Komutlar
    application.add_handler(CommandHandler(["start", "yardim", "help"], cmd_start))
    application.add_handler(CommandHandler("sayfalar", cmd_sayfalar))
    application.add_handler(CommandHandler("ozet", cmd_ozet))
    application.add_handler(CommandHandler("iptal", cmd_iptal_idle))

    # 4) Butonlar
    application.add_handler(CallbackQueryHandler(on_save, pattern=r"^kaydet\|"))
    application.add_handler(CallbackQueryHandler(on_cancel_entry, pattern=r"^iptal\|"))

    # 5) Serbest metin = hızlı veri girişi
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    validate_config()
    logger.info("Bot başlatılıyor...")
    application = build_application()
    # run_polling ağ kopmalarında kendisi yeniden bağlanır; Ctrl+C ile düzgün kapanır.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
