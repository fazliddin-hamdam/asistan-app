# Kurulum Rehberi — Kişisel Telegram Muhasebe Asistanı

Dosyalar:

| Dosya | Görevi |
|---|---|
| `main.py` | Botun tamamı |
| `config.example.py` | Ayar şablonu → `config.py` olarak kopyalanıp doldurulur |
| `requirements.txt` | Python kütüphaneleri |
| `start.sh` | Botu çökünce otomatik yeniden başlatan döngü |
| `index.html` | Mini App formu (GitHub Pages'te yayınlanır) |
| `config.py` | **Sizin oluşturacağınız** gizli ayarlar (git'e eklenmez) |
| `credentials.json` | **Sizin ekleyeceğiniz** Google Service Account anahtarı (git'e eklenmez) |

Sırası: **0 → 2 → 1 → 3 → 4** (önce token'ları ve Google anahtarını hazırlamak, sonra telefona kurmak en pratiği).

---

## 0. Telegram Hazırlığı

1. Telegram'da **@BotFather** → `/newbot` → isim ve kullanıcı adı verin → size verilen **token**'ı kopyalayın (`123456789:AA...` biçiminde).
2. **@userinfobot**'a herhangi bir mesaj atın → size verdiği **Id** sayısı sizin `ALLOWED_USER_ID`'nizdir.
3. Yeni botunuza bir kere `/start` yazın (bot size ilk mesajı ancak bundan sonra atabilir).

---

## 1. Termux Kurulum Adımları

> ⚠️ Termux'u **F-Droid**'den (veya GitHub sürümlerinden) kurun. Play Store sürümü eski ve paket depoları çalışmıyor.

### 1.1 Paketler

```bash
pkg update && pkg upgrade -y
pkg install -y python git tmux termux-api python-cryptography
termux-setup-storage
```

> `python-cryptography` Termux'un hazır derlenmiş paketidir. Bu olmadan `pip install` adımı, `cryptography` kütüphanesini telefonda derlemeye çalışırken **"Rust not found / Failed to build 'cryptography'"** hatası verir.

`termux-setup-storage` telefonun dahili hafızasına (`~/storage/downloads` vb.) erişim izni ister — **İzin ver** deyin.

### 1.2 Projeyi telefona indirme

```bash
git clone https://github.com/fazliddin-hamdam/asistan-app.git ~/Asistan
cd ~/Asistan
```

Gizli dosyalar GitHub'da yoktur; `credentials.json` dosyasını (bölüm 2.4) telefonun **İndirilenler** klasörüne koyup kopyalayın:

```bash
cp ~/storage/downloads/credentials.json ~/Asistan/
```

İleride kod güncellenince tek komut yeterlidir (gizli dosyalarınıza dokunmaz):

```bash
cd ~/Asistan && git pull && pkill -f main.py
```

### 1.3 Python kütüphaneleri

```bash
pip install -r requirements.txt
```

> Termux'ta `pip install --upgrade pip` **çalıştırmayın**; pip, `pkg` tarafından yönetilir.

### 1.4 Ayarları doldurma

```bash
cp config.example.py config.py
nano config.py
```

| Ayar | Ne yazılacak |
|---|---|
| `TELEGRAM_TOKEN` | BotFather'dan aldığınız token (tırnak içinde) |
| `ALLOWED_USER_ID` | @userinfobot'tan aldığınız sayı (**tırnaksız**) |
| `SPREADSHEET_ID` | Google Sheets URL'sindeki ID (bkz. bölüm 2.5) |
| `WEBAPP_URL` | Mini App adresi (bölüm 3.1); kullanmayacaksanız boş bırakın |

> 🔒 `config.py` ve `credentials.json` `.gitignore`'dadır; `git add .` yapsanız bile GitHub'a gitmez.

Kaydet: `Ctrl+O` → `Enter`, çık: `Ctrl+X`.

### 1.5 İlk deneme (ön planda)

```bash
python main.py
```

Telefonunuza "🤖 Bot çalışıyor. ✅ Google Sheets bağlı: ..." mesajı gelmeli. `Ctrl+C` ile durdurun.

### 1.6 Arka planda 7/24 çalıştırma

**Yöntem A — tmux (önerilen, logları canlı izleyebilirsiniz):**

```bash
tmux new -s bot
./start.sh
```

- Oturumdan ayrılmak (bot çalışmaya devam eder): `Ctrl+B`, sonra `D`
- Geri bağlanmak: `tmux attach -t bot`
- Durdurmak: bağlanıp `Ctrl+C` (iki kez — önce bot, sonra döngü)

**Yöntem B — nohup (tmux'suz):**

```bash
nohup ./start.sh > nohup.out 2>&1 &
```

- Durdurmak: `pkill -f start.sh; pkill -f main.py`
- Log: `tail -f bot.log`

### 1.7 Android'in botu öldürmesini engelleme (çok önemli)

1. **Ayarlar → Uygulamalar → Termux → Pil → Kısıtlanmamış / Optimize etme**.
2. `start.sh` zaten `termux-wake-lock` çalıştırır; bildirim çubuğunda Termux bildiriminde "Wake lock held" görünmeli.
3. Xiaomi/Huawei/Samsung gibi markalarda ayrıca: son uygulamalar ekranında Termux'u **kilitleyin** ve "Otomatik başlatma" iznini açın. (Marka bazlı ayrıntı: https://dontkillmyapp.com)

### 1.8 (İsteğe bağlı) Telefon açılınca otomatik başlatma

F-Droid'den **Termux:Boot** uygulamasını kurun, bir kere açın, sonra:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/muhasebe.sh << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
tmux new -d -s bot "$HOME/Asistan/start.sh"
EOF
chmod +x ~/.termux/boot/muhasebe.sh
```

---

## 2. Google Cloud & Sheets Ayarları

### 2.1 Proje oluşturma
1. https://console.cloud.google.com adresine gidin.
2. Üstteki proje seçici → **Yeni Proje** → ad: `muhasebe-bot` → **Oluştur**.

### 2.2 API'leri etkinleştirme
**API'ler ve Hizmetler → Kitaplık** bölümünde arayıp ikisini de **Etkinleştir**in:
- **Google Sheets API**
- **Google Drive API**

### 2.3 Service Account oluşturma
1. **API'ler ve Hizmetler → Kimlik Bilgileri → + Kimlik Bilgisi Oluştur → Hizmet hesabı**.
2. Ad: `muhasebe-bot` → **Oluştur ve Devam Et** → rol seçmeden **Bitti**.

### 2.4 JSON anahtarını alma
1. Oluşan hizmet hesabına tıklayın → **Anahtarlar** sekmesi → **Anahtar Ekle → Yeni anahtar oluştur → JSON**.
2. Bir JSON dosyası iner. Adını `credentials.json` yapıp `main.py`'nin yanına koyun (dosyayı elle düzenlemeyin).
3. `"client_email"` değerini (`muhasebe-bot@....iam.gserviceaccount.com`) bir sonraki adım için not edin.

> 🔒 `credentials.json` tablonuza tam erişim sağlar. Kimseyle paylaşmayın, GitHub'a yüklemeyin (`.gitignore`'da zaten hariç).

### 2.5 Google Sheets dosyasını yetkilendirme
1. https://sheets.google.com → yeni boş tablo oluşturun (örn. "Muhasebe").
2. Sağ üst **Paylaş** → 2.4'te kopyaladığınız `client_email` adresini yapıştırın → yetki **Düzenleyen** → "Bildirim gönder" işaretini kaldırın → **Paylaş**.
3. Adres çubuğundaki URL'den ID'yi alın:
   `https://docs.google.com/spreadsheets/d/`**`1AbC...xyz`**`/edit` → kalın kısım `SPREADSHEET_ID`'dir.
4. Bot ilk sekmeyi oluşturduktan sonra (`/yenisayfa Kasa`) varsayılan boş "Sayfa1" sekmesini silin; yoksa butonlarda o da görünür.

---

## 3. Kullanım

| Yazdığınız | Sonuç |
|---|---|
| `A101 500 TL` | 500 ₺ **gider**, açıklama "A101" |
| `Banyo musluğu 400` | 400 ₺ gider |
| `Market 1.250,50` | 1.250,50 ₺ gider |
| `+15000 maaş` | 15.000 ₺ **gelir** |
| `gelir kira 8000` | 8.000 ₺ gelir |
| `2 adet ampul 150` | 150 ₺ (birim yoksa **son sayı** tutar kabul edilir) |

Mesajdan sonra bot sekmeleri **buton** olarak sunar; birine basınca satır eklenir. Tür yanlış tahmin edildiyse önce **🔄** butonuyla Gider/Gelir değiştirilir.

Komutlar: `/yenisayfa` (veya `/yenisayfa Borclar`), `/ozet`, `/iptal`, `/yardim`.

### Tablo yapısı (her sekme)

| A: Tarih | B: Tür | C: Tutar | D: Açıklama |
|---|---|---|---|
| 14-10-2026 10:30 | Gider | 500 | A101 |
| 14-10-2026 11:05 | Gelir | 15000 | maaş |

> Not: Şartnamedeki başlıklar (Tarih, Tür, Tutar, Açıklama) ile sütun listesi (A/B/C) arasında fark vardı; gelir/gider ayrımı yapılabilsin diye 4 sütunlu yapı kullanıldı. Tutar daima pozitif sayı olarak yazılır, yönü **Tür** sütunu belirtir.

---

## 3.1 (İsteğe bağlı) Telegram Mini App ile form

Yazarak kayıt yerine Telegram içinde açılan bir form (Gider/Gelir, tutar, açıklama, sekme) kullanılabilir. Telegram Mini App'leri yalnızca **herkese açık HTTPS** adresinden açtığı için `index.html` dosyası ücretsiz **GitHub Pages** ile yayınlanır. Sayfada gizli bilgi yoktur; Sheets'e yazmayı yine telefondaki bot yapar.

1. Projeyi kendi GitHub hesabınıza **Public** bir depo olarak yükleyin (ya da bu depoyu *fork*'layın).
2. Depoda **Settings → Pages** → *Branch*: `main`, klasör: **`/ (root)`** → **Save**.
3. 1-2 dakika sonra aynı sayfanın üstünde adres görünür: `https://KULLANICIADI.github.io/asistan-app/`
4. Bu adresi `config.py`'deki `WEBAPP_URL = ""` tırnaklarının içine yazın, botu yeniden başlatın.

Bot açılınca sohbetin altında **📝 Kayıt Ekle** butonu belirir. Formu doldurup **Kaydet**'e basınca pencere kapanır ve bot "✅ kaydedildi" yanıtı verir. Yeni sekmeyi formdaki **➕ Yeni sekme** ile de açabilirsiniz.

> Buton görünmezse bota `/start` yazın. Sekme listesi eskiyse (örn. Sheets'te elle sekme eklediyseniz) yine `/start` yazmanız listeyi yeniler.

---

## 3.2 (İsteğe bağlı) Sesli mesajla kayıt

Bota sesli mesaj gönderirsiniz ("A101'e beş yüz lira"); bot sesi **Gemini API** ile metne çevirir ve yazılı mesajdaki gibi sekme butonlarını gösterir. Ses telefonda işlenmez, yalnızca Google'a gönderilir; telefon yorulmaz.

1. https://aistudio.google.com/apikey adresine Google hesabınızla girin → **Create API key** → anahtarı kopyalayın. **Kart istemez.**
2. `config.py`'ye ekleyin:
   ```python
   GEMINI_API_KEY = "kopyaladığınız_anahtar"
   ```
3. Botu yeniden başlatın: `pkill -f main.py`

> Ücretsiz katmanda Google, gönderilen içeriği ürünlerini geliştirmek için kullanabilir. Sesli notlara hassas bilgi (kart numarası vb.) söylemeyin.

---

## 4. Sorun Giderme

| Belirti | Çözüm |
|---|---|
| `config.py bulunamadı` | `cp config.example.py config.py` ve doldurun (bölüm 1.4) |
| `AYAR HATASI: ...` ile kapanıyor | `config.py`'deki `BURAYA_...` alanlarını doldurun |
| "Tabloya erişim yok" | Tabloyu `client_email` ile **Düzenleyen** olarak paylaşın; Sheets **ve** Drive API açık mı? |
| "Tablo bulunamadı" | `SPREADSHEET_ID` yanlış ya da paylaşım yapılmamış |
| Başlangıç mesajı gelmiyor | Bota önce `/start` yazın; `ALLOWED_USER_ID` doğru mu? |
| Bot birkaç saat sonra duruyor | Bölüm 1.7'deki pil ayarları |
| Butona basınca "artık geçerli değil" | Bot yeniden başladı; bekleyen kayıtlar bellekte tutulur, mesajı tekrar gönderin |
| Ayrıntılı hata | `tail -n 50 ~/Asistan/bot.log` |
