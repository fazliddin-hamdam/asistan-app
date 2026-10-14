#!/data/data/com.termux/files/usr/bin/bash
# Botu çöktüğünde otomatik yeniden başlatan döngü.
# Kullanım: ./start.sh   (tmux içinde veya Termux:Boot ile)

cd "$(dirname "$0")" || exit 1
termux-wake-lock 2>/dev/null   # Telefon uykudayken CPU'nun durmasını engeller

while true; do
    echo "[$(date '+%d-%m-%Y %H:%M:%S')] Bot başlatılıyor..."
    python main.py
    echo "[$(date '+%d-%m-%Y %H:%M:%S')] Bot durdu (çıkış kodu $?). 10 sn sonra yeniden başlatılacak..."
    sleep 10
done
