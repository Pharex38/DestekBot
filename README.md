# DestekBot

Telegram **Business** hesabına bağlanan, müşteri mesajlarına OpenAI ile cevap taslağı üreten
destek asistanı. Taslak önce hesap sahibine onay butonlarıyla gelir; onaylanınca cevap
**hesap sahibinin kendi adına** gönderilir. İstenirse (`/otomatik`) onaysız çalışır.

Userbot değildir: Telegram'ın resmî Business "Chatbots" özelliğini Bot API üzerinden kullanır,
bu yüzden hesap otomasyonu kaynaklı ban riski taşımaz.

## Özellikler

- **Onaylı akış:** taslak → onay / ret butonları → müşteriye hesap adına cevap.
- **Bilgi dosyası:** ürün, fiyat ve komut bilgisi `bilgi.md`'den her taslakta yeniden okunur;
  düzenleyince restart gerekmez. Model yazılmayanı uydurmaz, "not aldım, dönerim" der.
- **Bölünmüş mesaj birleştirme:** müşteri art arda yazınca son mesajdan `BEKLEME_SN` saniye
  sonra hepsi tek mesaj olarak işlenir.
- **Bağlam:** müşteri başına son 40 mesaj saklanır.
- **Görsel desteği:** ekran görüntüsü gibi görseller vision ile okunur; müşteri başına günlük
  `GORSEL_LIMIT` hakkı, spam ile API bakiyesinin boşaltılmasını engeller.
- **"Yazıyor…" göstergesi:** `YAZIYOR_GECIKME` saniye sonra başlar, cevap gidene kadar sürer.
- **Kalıcı mod:** `/otomatik` ile açılıp kapanır, `durum.json`'da restart'tan sonra da korunur.

## Kurulum

1. Telegram Premium + Business hesabı gerekir. @BotFather'dan bot aç, **Business Mode**'u etkinleştir.
2. Telegram → Ayarlar → Business → Chatbots → botu seç → izin ver.
3. Kur ve çalıştır:

```bash
pip install -r requirements.txt
cp .env.example .env          # BOT_TOKEN, OWNER_ID (kendi Telegram ID'n), OPENAI_API_KEY
cp bilgi.ornek.md bilgi.md    # kendi ürün/fiyat bilgilerin
python3 destek.py
```

Komutlar (yalnız hesap sahibi): `/start`, `/otomatik`, `/durum`.

`.env`, `bilgi.md` ve `durum.json` git'e girmez.

## Yazar

Alperen Kocakaplan — [@Pharex38](https://github.com/Pharex38)
