#!/usr/bin/env python3
"""
Kişisel Telegram muhasebe botu.

Termux'ta 7/24 çalışır, yalnızca sahibine yanıt verir ve gelir/gider kayıtlarını
Google Sheets sekmelerine yazar. Kayıt iki yoldan girilir:
  - serbest metin: "A101 500 TL", "+15000 maaş"  -> sekme butonlardan seçilir
  - Mini App formu (docs/index.html)            -> veri sendData ile gelir
Komutlar: /yenisayfa [ad], /ozet, /iptal, /yardim
"""

import asyncio
import html
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

import gspread
import requests
from gspread.utils import ValueRenderOption
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
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

# =============================================================================
# AYARLAR
# =============================================================================

# Gizli ayarlar config.py'de (git'e eklenmez); şablon: config.example.py
try:
    from config import ALLOWED_USER_ID, SPREADSHEET_ID, TELEGRAM_TOKEN, WEBAPP_URL
except ImportError:
    sys.exit("config.py bulunamadı: config.example.py'yi config.py adıyla kopyalayıp doldurun.")

# Google Cloud'dan indirilen Service Account anahtarı (git'e eklenmez)
CREDENTIALS_FILE = "credentials.json"

TIMEZONE = timezone(timedelta(hours=3))  # Türkiye sabit UTC+3; Termux'ta tz verisine gerek kalmaz
DATE_FORMAT = "%d-%m-%Y %H:%M"
HEADERS = ["Tarih", "Tür", "Tutar", "Açıklama"]
NETWORK_TIMEOUT = 30  # sn; PTB'nin 5 sn varsayılanı mobil internette kısa kalıyor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler("bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # her polling isteğini loglamasın
logger = logging.getLogger("muhasebe")

KIND_EXPENSE = "Gider"
KIND_INCOME = "Gelir"


def now_str() -> str:
    return datetime.now(TIMEZONE).strftime(DATE_FORMAT)


def fmt_money(value: float) -> str:
    """1250.5 -> '1.250,50 ₺'"""
    s = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{s} ₺"


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


# =============================================================================
# METİN AYRIŞTIRICI
# =============================================================================

CURRENCY_WORDS = {"tl", "try", "₺", "lira", "tl."}
INCOME_KEYWORDS = {"gelir", "+gelir"}
EXPENSE_KEYWORDS = {"gider", "harcama"}

# Metinde geçince kaydı gelir sayan kelime başları. "aldım" bilerek yok:
# "ekmek aldım" gider, "müşteriden aldım" gelir. Yanlış tahmin 🔄 ile düzeltilir.
INCOME_HINTS = (
    "maaş", "maas", "gelir", "tahsil", "kazanç", "kazanc", "kazandım", "kazandim",
    "sattım", "sattim", "satış", "satis", "müşteriden", "musteriden",
    "ikramiye", "prim", "harçlık", "harclik", "temettü",
)

# İşaret, ₺ ön eki, rakamla başlayan sayı, birim son eki. "A101" rakamla başlamadığı için tutar sayılmaz.
AMOUNT_RE = re.compile(r"^([+-]?)₺?(\d[\d.,]*)(tl|try|₺)?$", re.IGNORECASE)


@dataclass
class ParsedEntry:
    amount: float
    kind: str
    description: str


def tr_lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def parse_number(raw: str) -> float | None:
    """'500', '1.250', '1.250,50', '1,250.50', '99,90', '12.5' biçimlerini okur."""
    if "." in raw and "," in raw:
        decimal_sep = "." if raw.rfind(".") > raw.rfind(",") else ","  # sondaki ayırıcı ondalıktır
        thousands_sep = "," if decimal_sep == "." else "."
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in raw:
        whole, *rest = raw.split(",")
        raw = f"{whole}.{rest[0]}" if len(rest) == 1 and 1 <= len(rest[0]) <= 2 else raw.replace(",", "")
    elif raw.count(".") > 1 or ("." in raw and len(raw.split(".")[1]) == 3):
        raw = raw.replace(".", "")  # 1.250 / 1.250.000 -> binlik
    try:
        return float(raw)
    except ValueError:
        return None


def parse_entry(text: str) -> ParsedEntry | None:
    """
    Serbest metinden tutar/tür/açıklama çıkarır; tutar yoksa None.
    Birimli (TL/₺) sayı varsa o, yoksa SON sayı tutardır: "2 adet ampul 150" -> 150.
    """
    tokens = text.split()
    if not tokens:
        return None

    kind, explicit = KIND_EXPENSE, False  # explicit: tür kelimeyle/işaretle açıkça belirtildi
    first = tr_lower(tokens[0])
    if first in INCOME_KEYWORDS:
        kind, tokens, explicit = KIND_INCOME, tokens[1:], True
    elif first in EXPENSE_KEYWORDS:
        tokens, explicit = tokens[1:], True

    candidates = []  # (indeks, değer, işaret, birimli_mi, birim_sonraki_kelimede_mi)
    for i, tok in enumerate(tokens):
        m = AMOUNT_RE.match(tok.rstrip(".,;:!?"))
        value = parse_number(m.group(2)) if m else None
        if not value or value <= 0:
            continue
        currency_next = i + 1 < len(tokens) and tokens[i + 1].lower() in CURRENCY_WORDS
        has_currency = bool(m.group(3)) or tok.lstrip("+-").startswith("₺") or currency_next
        candidates.append((i, value, m.group(1), has_currency, currency_next))
    if not candidates:
        return None

    idx, value, sign, _, currency_next = ([c for c in candidates if c[3]] or candidates)[-1]
    if sign:
        kind, explicit = (KIND_INCOME if sign == "+" else KIND_EXPENSE), True

    skip = {idx, idx + 1} if currency_next else {idx}
    description = " ".join(t for i, t in enumerate(tokens) if i not in skip)
    if description.startswith("+"):  # "+ maaş 15000"
        kind, explicit, description = KIND_INCOME, True, description[1:].strip()
    if not explicit and any(tr_lower(w).strip(".,;:!?'\"").startswith(INCOME_HINTS) for w in description.split()):
        kind = KIND_INCOME

    return ParsedEntry(round(value, 2), kind, description or "(açıklama yok)")


# =============================================================================
# GOOGLE SHEETS
# =============================================================================


class SheetExistsError(Exception):
    """args[0]: mevcut sekmenin gerçek adı (büyük/küçük harfi farklı olabilir)."""


def check_credentials_file(path: str) -> str | None:
    """Anahtar dosyası kullanılamıyorsa hata mesajı döndürür."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            info = json.load(f)
    except OSError:
        return f"'{path}' bulunamadı; main.py ile aynı klasörde olmalı."
    except json.JSONDecodeError:
        return f"'{path}' geçerli bir JSON değil."
    if "@" not in str(info.get("client_email")) or "PRIVATE KEY" not in str(info.get("private_key")):
        return f"'{path}' içinde client_email/private_key yok; Google'dan indirilen anahtarı kullanın."
    return None


class SheetsRepository:
    """gspread bloklayan bir kütüphane; metotlar bottan asyncio.to_thread ile çağrılır."""

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, credentials_file: str, spreadsheet_id: str):
        self._credentials_file = credentials_file
        self._spreadsheet_id = spreadsheet_id
        self._spreadsheet: gspread.Spreadsheet | None = None

    def _call(self, fn):
        """fn(spreadsheet) çalıştırır; geçici ağ/kota hatasında bağlantıyı yenileyip bir kez daha dener."""
        for attempt in (1, 2):
            try:
                if self._spreadsheet is None:
                    client = gspread.service_account(filename=self._credentials_file)
                    self._spreadsheet = client.open_by_key(self._spreadsheet_id)
                return fn(self._spreadsheet)
            except (requests.ConnectionError, requests.Timeout, gspread.exceptions.APIError) as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                transient = not isinstance(e, gspread.exceptions.APIError) or status in self.RETRYABLE_STATUS
                if attempt == 2 or not transient:
                    raise
                logger.warning("Geçici Sheets hatası, yeniden deneniyor: %s", e)
                self._spreadsheet = None
                time.sleep(2)

    def check_connection(self) -> str:
        return self._call(lambda sh: sh.title)

    def list_sheet_titles(self) -> list[str]:
        return self._call(lambda sh: [ws.title for ws in sh.worksheets()])

    def create_sheet(self, title: str) -> None:
        def create(sh: gspread.Spreadsheet):
            for ws in sh.worksheets():
                if ws.title.casefold() == title.casefold():
                    raise SheetExistsError(ws.title)
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(HEADERS))
            ws.update([HEADERS], "A1")
            try:  # yalnızca görünüm; başarısız olsa da sekme kullanılabilir
                ws.format("A1:D1", {"textFormat": {"bold": True}})
                ws.freeze(rows=1)
            except Exception as e:  # noqa: BLE001
                logger.warning("Başlık biçimlendirilemedi: %s", e)

        self._call(create)

    def append_entry(self, title: str, entry: ParsedEntry, date_str: str) -> None:
        row = [date_str, entry.kind, entry.amount, entry.description]
        # RAW: tarih metin, tutar sayı olarak yazılır; tablonun yerel ayarı yorumlamaz
        self._call(lambda sh: sh.worksheet(title).append_row(row, value_input_option="RAW", table_range="A1"))

    def summarize(self) -> list[tuple[str, float, float]]:
        """Her sekme için (ad, toplam gelir, toplam gider)."""

        def summarize(sh: gspread.Spreadsheet):
            result = []
            for ws in sh.worksheets():
                income = expense = 0.0
                for row in ws.get("A2:D", value_render_option=ValueRenderOption.unformatted):
                    amount = row[2] if len(row) > 2 else None
                    if isinstance(amount, str):  # elle girilmiş metin tutarlar
                        amount = parse_number(amount.replace("₺", "").strip())
                    if not isinstance(amount, (int, float)):
                        continue
                    if str(row[1]).strip().lower() == KIND_INCOME.lower():
                        income += amount
                    else:
                        expense += amount
                result.append((ws.title, income, expense))
            return result

        return self._call(summarize)


def describe_sheets_error(e: Exception) -> str:
    """Sheets hatalarını kullanıcıya anlaşılır mesaja çevirir."""
    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return "Bu sekme artık yok (silinmiş ya da adı değişmiş)."
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return "Tablo bulunamadı; SPREADSHEET_ID'yi kontrol edin."
    if isinstance(e, PermissionError) and isinstance(e.__cause__, gspread.exceptions.APIError):
        e = e.__cause__  # gspread, open_by_key'deki 403'ü PermissionError'a çevirir
    if isinstance(e, gspread.exceptions.APIError):
        status = e.response.status_code
        if status == 403:
            return "Tabloya erişim yok: tabloyu service account e-postasıyla 'Düzenleyen' olarak paylaşın."
        if status == 429:
            return "Google kota sınırı aşıldı, bir dakika sonra tekrar deneyin."
        return f"Google Sheets API hatası ({status})."
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return "İnternet yok ya da Google'a ulaşılamıyor."
    return f"Beklenmeyen hata: {type(e).__name__}"


# =============================================================================
# ORTAK YARDIMCILAR
# =============================================================================

MAX_PENDING = 20  # sekmesi seçilmeyi bekleyen en fazla kayıt


@dataclass
class PendingEntry:
    entry: ParsedEntry
    date_str: str  # mesajın geldiği an
    sheets: list[str]  # butonların gösterildiği andaki sekmeler; buton sekmeyi indeksle seçer


def repo_of(context: ContextTypes.DEFAULT_TYPE) -> SheetsRepository:
    return context.application.bot_data["repo"]


def pending_store(context: ContextTypes.DEFAULT_TYPE) -> dict[str, PendingEntry]:
    return context.user_data.setdefault("pending", {})


def entry_summary(entry: ParsedEntry) -> str:
    icon = "🟢" if entry.kind == KIND_INCOME else "🔴"
    return f"{icon} <b>{entry.kind}</b>: {fmt_money(entry.amount)}\n📝 {esc(entry.description)}"


def saved_text(sheet: str, entry: ParsedEntry, date_str: str, note: str = "") -> str:
    return f"✅ <b>{esc(sheet)}</b> sekmesine kaydedildi{note}\n\n{entry_summary(entry)}\n🕒 {date_str}"


def pending_text(pending: PendingEntry) -> str:
    return f"{entry_summary(pending.entry)}\n\n📂 Hangi sekmeye kaydedilsin?"


def sheet_keyboard(entry_id: str, pending: PendingEntry) -> InlineKeyboardMarkup:
    """
    Kayıt butonları. callback_data 64 baytla sınırlı olduğundan sekme adı yerine
    kaydın kısa ID'si ve sekmenin indeksi taşınır.
    """
    other = KIND_INCOME if pending.entry.kind == KIND_EXPENSE else KIND_EXPENSE
    sheet_buttons = [
        InlineKeyboardButton(title, callback_data=f"kaydet|{entry_id}|{i}") for i, title in enumerate(pending.sheets)
    ]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🔄 {other} olarak değiştir", callback_data=f"tur|{entry_id}")]]
        + [sheet_buttons[i : i + 2] for i in range(0, len(sheet_buttons), 2)]
        + [[InlineKeyboardButton("❌ İptal", callback_data=f"iptal|{entry_id}")]]
    )


# =============================================================================
# GÜVENLİK
# =============================================================================


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Her güncellemeden önce (group=-1) çalışır; sahibi dışındakiler sessizce yok sayılır."""
    user = update.effective_user
    if user is None or user.id != ALLOWED_USER_ID:
        if user is not None:
            logger.warning("Yetkisiz erişim denemesi: id=%s username=%s", user.id, user.username)
        raise ApplicationHandlerStop


# =============================================================================
# KOMUTLAR VE METİNLE KAYIT
# =============================================================================

HELP_TEXT = (
    "<b>💰 Muhasebe Asistanı</b>\n\n"
    "Kayıt için yazın: <code>A101 500 TL</code>, <code>Market 1.250,50</code>\n"
    "Gelir: <code>+15000 maaş</code>, <code>gelir kira 8000</code> ya da "
    "maaş, tahsilat, müşteriden, sattım gibi kelimeler.\n"
    "Sonra sekmeyi seçin; tür yanlışsa önce 🔄 ile değiştirin.\n\n"
    "/yenisayfa – Yeni sekme oluştur\n"
    "/ozet – Sekme bazında gelir/gider\n"
    "/iptal – İşlemi iptal et"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = await webapp_keyboard(repo_of(context))
    text = HELP_TEXT + (f"\n\nForm için alttaki <b>{WEBAPP_BUTTON_TEXT}</b> butonunu kullanın." if keyboard else "")
    await update.message.reply_html(text, reply_markup=keyboard)


async def cmd_ozet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Hesaplanıyor...")
    try:
        summary = await asyncio.to_thread(repo_of(context).summarize)
    except Exception as e:  # noqa: BLE001
        logger.exception("Özet alınamadı")
        await msg.edit_text(f"⚠️ {describe_sheets_error(e)}")
        return

    parts = ["<b>📊 Özet</b>"]
    for title, income, expense in summary:
        parts.append(
            f"\n<b>{esc(title)}</b>\n  🟢 Gelir: {fmt_money(income)}\n"
            f"  🔴 Gider: {fmt_money(expense)}\n  ⚖️ Net: {fmt_money(income - expense)}"
        )
    total_in = sum(s[1] for s in summary)
    total_out = sum(s[2] for s in summary)
    parts.append(
        f"\n<b>TOPLAM</b>\n  🟢 {fmt_money(total_in)}  🔴 {fmt_money(total_out)}  ⚖️ {fmt_money(total_in - total_out)}"
    )
    await msg.edit_text("\n".join(parts), parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Serbest metni ayrıştırır, sekme butonlarını gösterir."""
    entry = parse_entry(update.message.text)
    if entry is None:
        await update.message.reply_html("🤔 Tutar bulamadım. Örnek: <code>A101 500 TL</code>")
        return
    try:
        titles = await asyncio.to_thread(repo_of(context).list_sheet_titles)
    except Exception as e:  # noqa: BLE001
        logger.exception("Sekmeler alınamadı")
        await update.message.reply_text(f"⚠️ {describe_sheets_error(e)}")
        return
    if not titles:
        await update.message.reply_text("Henüz sekme yok. Önce /yenisayfa ile bir sekme oluşturun.")
        return

    store = pending_store(context)
    while len(store) >= MAX_PENDING:
        store.pop(next(iter(store)))  # en eski bekleyen kaydı at
    entry_id = uuid.uuid4().hex[:8]
    pending = store[entry_id] = PendingEntry(entry, now_str(), titles)
    await update.message.reply_html(pending_text(pending), reply_markup=sheet_keyboard(entry_id, pending))


async def on_entry_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kayıt mesajındaki butonlar: 'kaydet|<id>|<sekme no>', 'tur|<id>', 'iptal|<id>'."""
    query = update.callback_query
    action, entry_id, *args = query.data.split("|")
    store = pending_store(context)
    pending = store.get(entry_id)
    if pending is None:
        await query.answer()
        await query.edit_message_text("⌛ Bu kayıt artık geçerli değil (kaydedilmiş ya da bot yeniden başlamış).")
        return

    if action == "iptal":
        del store[entry_id]
        await query.answer()
        await query.edit_message_text("❌ Kayıt iptal edildi.")
    elif action == "tur":
        pending.entry.kind = KIND_INCOME if pending.entry.kind == KIND_EXPENSE else KIND_EXPENSE
        await query.answer(f"{pending.entry.kind} olarak değiştirildi")
        await query.edit_message_text(
            pending_text(pending), parse_mode="HTML", reply_markup=sheet_keyboard(entry_id, pending)
        )
    else:  # kaydet — PTB güncellemeleri sırayla işler; çift tıklama ikinci kez buraya gelemez
        await query.answer()
        title = pending.sheets[int(args[0])]
        try:
            await asyncio.to_thread(repo_of(context).append_entry, title, pending.entry, pending.date_str)
        except Exception as e:  # noqa: BLE001
            logger.exception("Kayıt yazılamadı")
            await query.edit_message_text(
                f"{pending_text(pending)}\n\n⚠️ {esc(describe_sheets_error(e))}",
                parse_mode="HTML",
                reply_markup=sheet_keyboard(entry_id, pending),
            )
            return
        del store[entry_id]
        logger.info("Kaydedildi -> %s: %s", title, pending.entry)
        await query.edit_message_text(saved_text(title, pending.entry, pending.date_str), parse_mode="HTML")


# =============================================================================
# YENİ SEKME (/yenisayfa)
# =============================================================================

WAITING_SHEET_NAME = 1
MAX_SHEET_NAME_LEN = 100  # Google Sheets sınırı


def normalize_sheet_name(raw: str) -> str:
    return " ".join(str(raw).split())


async def new_sheet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if context.args:  # "/yenisayfa Borclar"
        return await create_sheet_from(update, context, " ".join(context.args))
    await update.message.reply_html("📄 Yeni sekmenin adını yazın (örn. <code>Borclar</code>). Vazgeçmek için /iptal")
    return WAITING_SHEET_NAME


async def new_sheet_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await create_sheet_from(update, context, update.message.text)


async def create_sheet_from(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_name: str) -> int:
    name = normalize_sheet_name(raw_name)
    if not 0 < len(name) <= MAX_SHEET_NAME_LEN:
        await update.message.reply_text(f"Ad 1-{MAX_SHEET_NAME_LEN} karakter olmalı, tekrar yazın (veya /iptal).")
        return WAITING_SHEET_NAME

    repo = repo_of(context)
    try:
        await asyncio.to_thread(repo.create_sheet, name)
    except SheetExistsError as e:
        await update.message.reply_html(f"⚠️ <b>{esc(e.args[0])}</b> zaten var. Başka bir ad yazın (veya /iptal).")
        return WAITING_SHEET_NAME
    except Exception as e:  # noqa: BLE001
        logger.exception("Sekme oluşturulamadı")
        await update.message.reply_text(f"⚠️ Sekme oluşturulamadı: {describe_sheets_error(e)}")
        return ConversationHandler.END

    logger.info("Yeni sekme: %s", name)
    await update.message.reply_html(
        f"✅ <b>{esc(name)}</b> sekmesi oluşturuldu.",
        reply_markup=await webapp_keyboard(repo),  # Mini App'teki sekme listesini yenile
    )
    return ConversationHandler.END


async def cmd_iptal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("İptal edildi.")
    return ConversationHandler.END


# =============================================================================
# TELEGRAM MINI APP
# =============================================================================
# docs/index.html statik bir sayfadır (GitHub Pages). Sekme listesini butonun
# adresindeki ?sheets=[...] parametresinden okur ve formu Telegram.WebApp.sendData
# ile gönderir; veri bota web_app_data mesajı olarak gelir. Böylece telefonun
# dışarıya açık bir sunucusu olmasına gerek kalmaz. sendData yalnızca alttaki
# (reply) klavye butonuyla açılan Mini App'lerde çalışır.

WEBAPP_BUTTON_TEXT = "📝 Kayıt Ekle"
MAX_DESCRIPTION_LEN = 200


async def webapp_keyboard(repo: SheetsRepository) -> ReplyKeyboardMarkup | None:
    """Mini App butonu; sekme listesi adrese gömülü olduğundan sekmeler değişince yeniden gönderilir."""
    if not WEBAPP_URL:
        return None
    try:
        titles = await asyncio.to_thread(repo.list_sheet_titles)
    except Exception:  # noqa: BLE001  (form "Yeni sekme" ile yine de kullanılabilir)
        logger.warning("Mini App için sekme listesi alınamadı", exc_info=True)
        titles = []
    url = f"{WEBAPP_URL}?sheets={quote(json.dumps(titles, ensure_ascii=False), safe='')}"
    return ReplyKeyboardMarkup(
        [[KeyboardButton(WEBAPP_BUTTON_TEXT, web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
        is_persistent=True,
    )


def parse_webapp_payload(data: str) -> tuple[ParsedEntry, str, bool]:
    """{"type", "amount", "desc", "sheet", "new"} JSON'unu doğrular; hatalıysa ValueError."""
    try:
        payload = json.loads(data)
        kind, amount = payload["type"], payload["amount"]
        sheet = normalize_sheet_name(payload["sheet"])
        description = " ".join(str(payload.get("desc") or "").split())[:MAX_DESCRIPTION_LEN]
    except (ValueError, KeyError, TypeError):
        raise ValueError("Mini App'ten gelen veri okunamadı.") from None
    if kind not in (KIND_EXPENSE, KIND_INCOME):
        raise ValueError("Kayıt türü geçersiz.")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount < 1e12:
        raise ValueError("Tutar geçersiz.")
    if not 0 < len(sheet) <= MAX_SHEET_NAME_LEN:
        raise ValueError("Sekme adı geçersiz.")
    return ParsedEntry(round(float(amount), 2), kind, description or "(açıklama yok)"), sheet, bool(payload.get("new"))


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    try:
        entry, sheet, is_new = parse_webapp_payload(message.web_app_data.data)
    except ValueError as e:
        logger.warning("Geçersiz Mini App verisi: %s", message.web_app_data.data)
        await message.reply_text(f"⚠️ {e}")
        return

    repo = repo_of(context)
    date_str = now_str()
    note = ""
    try:
        if is_new:
            try:
                await asyncio.to_thread(repo.create_sheet, sheet)
                note = f"\n🆕 <b>{esc(sheet)}</b> sekmesi oluşturuldu."
            except SheetExistsError as e:
                sheet = e.args[0]  # "kasa" yazıldıysa mevcut "Kasa"ya yaz
                note = f"\nℹ️ <b>{esc(sheet)}</b> zaten vardı, kayıt oraya eklendi."
        await asyncio.to_thread(repo.append_entry, sheet, entry, date_str)
    except Exception as e:  # noqa: BLE001
        logger.exception("Mini App kaydı yazılamadı")
        await message.reply_text(f"⚠️ Kayıt yazılamadı: {describe_sheets_error(e)}")
        return

    logger.info("Kaydedildi (Mini App) -> %s: %s", sheet, entry)
    await message.reply_html(
        saved_text(sheet, entry, date_str, note),
        reply_markup=await webapp_keyboard(repo) if is_new else None,  # yeni sekme forma eklensin
    )


# =============================================================================
# BAŞLANGIÇ
# =============================================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Yakalanmamış hata", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:  # guard'dan geçtiyse sohbet sahibinindir
        await context.bot.send_message(update.effective_chat.id, "⚠️ Bir hata oluştu, ayrıntılar bot.log'da.")


async def on_startup(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("yenisayfa", "Yeni sekme oluştur"),
            BotCommand("ozet", "Gelir/gider özeti"),
            BotCommand("iptal", "İşlemi iptal et"),
            BotCommand("yardim", "Yardım"),
        ]
    )
    repo: SheetsRepository = application.bot_data["repo"]
    try:
        status = f"✅ Google Sheets bağlı: {await asyncio.to_thread(repo.check_connection)}"
    except Exception as e:  # noqa: BLE001
        logger.exception("Sheets bağlantısı kurulamadı")
        status = f"⚠️ Google Sheets bağlantısı kurulamadı: {describe_sheets_error(e)}"
    logger.info(status)
    try:  # bildirim, Mini App butonunu da getirir
        await application.bot.send_message(
            ALLOWED_USER_ID, f"🤖 Bot çalışıyor.\n{status}", reply_markup=await webapp_keyboard(repo)
        )
    except Exception as e:  # noqa: BLE001  (bota hiç /start yazılmadıysa Telegram izin vermez)
        logger.warning("Başlangıç bildirimi gönderilemedi: %s", e)


def validate_config() -> None:
    errors = []
    if TELEGRAM_TOKEN.startswith("BURAYA"):
        errors.append("TELEGRAM_TOKEN doldurulmamış.")
    if ALLOWED_USER_ID <= 0:
        errors.append("ALLOWED_USER_ID doldurulmamış.")
    if SPREADSHEET_ID.startswith("BURAYA"):
        errors.append("SPREADSHEET_ID doldurulmamış.")
    if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
        errors.append("WEBAPP_URL https:// ile başlamalı.")
    if credentials_error := check_credentials_file(CREDENTIALS_FILE):
        errors.append(credentials_error)
    if errors:
        for error in errors:
            logger.critical("AYAR HATASI: %s", error)
        sys.exit("main.py başındaki AYARLAR bölümünü düzenleyin.")


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(NETWORK_TIMEOUT)
        .read_timeout(NETWORK_TIMEOUT)
        .write_timeout(NETWORK_TIMEOUT)
        .pool_timeout(NETWORK_TIMEOUT)
        .get_updates_connect_timeout(NETWORK_TIMEOUT)
        .get_updates_read_timeout(NETWORK_TIMEOUT)
        .post_init(on_startup)
        .build()
    )
    application.bot_data["repo"] = SheetsRepository(CREDENTIALS_FILE, SPREADSHEET_ID)

    application.add_handler(TypeHandler(Update, guard), group=-1)
    # Konuşma, serbest metin işleyicisinden önce gelmeli: ad beklerken metin sekme adı sayılır
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("yenisayfa", new_sheet_start)],
            states={WAITING_SHEET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_sheet_name)]},
            fallbacks=[CommandHandler("iptal", cmd_iptal)],
            allow_reentry=True,
        )
    )
    application.add_handler(CommandHandler(["start", "yardim"], cmd_start))
    application.add_handler(CommandHandler("ozet", cmd_ozet))
    application.add_handler(CommandHandler("iptal", cmd_iptal))
    application.add_handler(CallbackQueryHandler(on_entry_button, pattern=r"^(kaydet|tur|iptal)\|"))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    validate_config()
    logger.info("Bot başlatılıyor...")
    # bootstrap_retries=-1: açılışta internet yoksa kapanmak yerine bağlanana kadar dener
    build_application().run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
