# Bu dosyayı config.py adıyla kopyalayıp doldurun:  cp config.example.py config.py
# config.py git'e eklenmez (.gitignore); gizli bilgiler yalnızca sizin cihazınızda kalır.

# @BotFather'dan alınan bot token'ı
TELEGRAM_TOKEN = "BURAYA_TELEGRAM_BOT_TOKENINI_YAZIN"

# Bot yalnızca bu kullanıcıya yanıt verir (ID'nizi @userinfobot söyler)
ALLOWED_USER_ID = 0

# Tablo adresindeki .../spreadsheets/d/<ID>/edit kısmı
SPREADSHEET_ID = "BURAYA_GOOGLE_SHEETS_ID_YAZIN"

# index.html'in GitHub Pages adresi (örn. "https://kullanici.github.io/asistan-app/")
# Boş bırakılırsa Mini App butonu gösterilmez.
WEBAPP_URL = ""

# Sesli kayıt (isteğe bağlı): https://aistudio.google.com/apikey adresinden ücretsiz anahtar
# (kart istemez). Boş bırakılırsa sesli mesajlar işlenmez.
GEMINI_API_KEY = ""
# Sesi metne çeviren model; "gemini-flash-latest" her zaman güncel Flash modelini gösterir
GEMINI_MODEL = "gemini-flash-latest"
