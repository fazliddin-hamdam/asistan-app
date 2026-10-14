# Kurulum Rehberi — Kişisel Telegram Muhasebe Asistanı

Dosyalar:

| Dosya | Görevi |
|---|---|
| `main.py` | Botun tamamı (ayarlar en üstte) |
| `requirements.txt` | Python kütüphaneleri |
| `start.sh` | Botu çökünce otomatik yeniden başlatan döngü |
| `credentials.json` | **Sizin ekleyeceğiniz** Google Service Account anahtarı |

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
pkg install -y python tmux termux-api
termux-setup-storage
```

`termux-setup-storage` telefonun dahili hafızasına (`~/storage/downloads` vb.) erişim izni ister — **İzin ver** deyin.

### 1.2 Proje dosyalarını telefona aktarma

Bilgisayardaki `Asistan` klasörünü (içinde `credentials.json` dahil) telefonun **İndirilenler** klasörüne kopyalayın (USB, Google Drive, Telegram "Kayıtlı Mesajlar" vb.). Sonra:

```bash
mkdir -p ~/muhasebe
cp ~/storage/downloads/Asistan/* ~/muhasebe/
cd ~/muhasebe
chmod +x start.sh
```

### 1.3 Python kütüphaneleri

```bash
pip install -r requirements.txt
```

> Termux'ta `pip install --upgrade pip` **çalıştırmayın**; pip, `pkg` tarafından yönetilir.

### 1.4 Ayarları doldurma

```bash
nano main.py
```

En üstteki **AYARLAR** bölümünde 4 yer var:

| Ayar | Ne yazılacak |
|---|---|
| `TELEGRAM_TOKEN` | BotFather'dan aldığınız token (tırnak içinde) |
| `ALLOWED_USER_ID` | @userinfobot'tan aldığınız sayı (**tırnaksız**) |
| `SPREADSHEET_ID` | Google Sheets URL'sindeki ID (bkz. bölüm 2.5) |
| `CREDENTIALS_FILE` | `credentials.json` (dosya aynı klasördeyse değiştirmeyin) |

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
tmux new -d -s bot "$HOME/muhasebe/start.sh"
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
2. İnen dosyanın adını **`credentials.json`** yapıp `main.py` ile aynı klasöre koyun.
3. Bu dosyanın içindeki `"client_email"` değerini kopyalayın (`muhasebe-bot@....iam.gserviceaccount.com`).

> 🔒 `credentials.json` tablonuza tam erişim sağlar. Kimseyle paylaşmayın, GitHub'a yüklemeyin (`.gitignore`'da zaten hariç).

### 2.5 Google Sheets dosyasını yetkilendirme
1. https://sheets.google.com → yeni boş tablo oluşturun (örn. "Muhasebe").
2. Sağ üst **Paylaş** → 2.4'te kopyaladığınız `client_email` adresini yapıştırın → yetki **Düzenleyen** → "Bildirim gönder" işaretini kaldırın → **Paylaş**.
3. Adres çubuğundaki URL'den ID'yi alın:
   `https://docs.google.com/spreadsheets/d/`**`1AbC...xyz`**`/edit` → kalın kısım `SPREADSHEET_ID`'dir.
4. Varsayılan "Sayfa1" sekmesini isterseniz silin/yeniden adlandırın ya da `main.py`'deki `HIDDEN_SHEETS` içine ekleyip butonlardan gizleyin.

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

Mesajdan sonra bot sekmeleri **buton** olarak sunar; birine basınca satır eklenir. **➕ Yeni sayfa** butonu yeni sekme açıp kaydı doğrudan oraya yazar.

Komutlar: `/yenisayfa` (veya `/yenisayfa Borclar`), `/sayfalar`, `/ozet`, `/iptal`, `/yardim`.

### Tablo yapısı (her sekme)

| A: Tarih | B: Tür | C: Tutar | D: Açıklama |
|---|---|---|---|
| 14-10-2026 10:30 | Gider | 500 | A101 |
| 14-10-2026 11:05 | Gelir | 15000 | maaş |

> Not: Şartnamedeki başlıklar (Tarih, Tür, Tutar, Açıklama) ile sütun listesi (A/B/C) arasında fark vardı; gelir/gider ayrımı yapılabilsin diye 4 sütunlu yapı kullanıldı. Tutar daima pozitif sayı olarak yazılır, yönü **Tür** sütunu belirtir.

---

## 4. Sorun Giderme

| Belirti | Çözüm |
|---|---|
| `AYAR HATASI: ...` ile kapanıyor | `main.py` başındaki placeholder'ları doldurun |
| "Erişim reddedildi (403)" | Tabloyu `client_email` ile **Düzenleyen** olarak paylaşın; Sheets **ve** Drive API açık mı? |
| "Tablo bulunamadı" | `SPREADSHEET_ID` yanlış ya da paylaşım yapılmamış |
| Başlangıç mesajı gelmiyor | Bota önce `/start` yazın; `ALLOWED_USER_ID` doğru mu? |
| Bot birkaç saat sonra duruyor | Bölüm 1.7'deki pil ayarları |
| Butona basınca "artık geçerli değil" | Bot yeniden başladı; bekleyen kayıtlar bellekte tutulur, mesajı tekrar gönderin |
| Ayrıntılı hata | `tail -n 50 ~/muhasebe/bot.log` |
