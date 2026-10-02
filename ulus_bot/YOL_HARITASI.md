# ULUS — Yol haritası

## Sırada: Zengin mesaj sistemi (onaylandı, henüz yazılmadı)
Hoş geldin, veda, kurallar, notlar, /filter ve duyuru tek ortak altyapıyı kullanacak.

1. **Medya** — resim, video, GIF, sticker, dosya, ses. Medyaya yanıt verip `/setwelcome` yazmak yeterli.
2. **Butonlar**
   - Satır satır yazım; `&&` ile aynı satıra yan yana:
     ```
     Kanalımız - https://t.me/kanal && Destek - https://t.me/destek
     Kuralları Oku - rules
     ```
   - Özel butonlar: 📜 kurallar (özelden gösterir), 📝 not, 💬 popup uyarı, renkli butonlar.
   - Karar bekliyor: Rose tarzı `[Kanal](buttonurl://t.me/kanal)` yazımı da desteklensin mi?
3. **Biçimlendirme** — kalın, italik, link, spoiler, alıntı, premium emoji; adminin mesajı nasıl biçimlendirdiyse öyle kaydedilir.
4. **Değişkenler** — `{kullanıcı}` `{ad}` `{soyad}` `{username}` `{id}` `{grup}` `{uye_sayisi}` `{tarih}` `{saat}`
5. **Rastgele hoş geldin** — birden fazla mesaj, her yeni üyeye rastgele biri.
6. **Panelden düzenleme** — ⚙️ Hoş geldin: Metin / Medya / Butonlar / 👁 Önizle / Sıfırla.
7. **Hoş geldin ekstraları**
   - Yeni hoş geldin gelince eskisini silme.
   - X dakika sonra otomatik silme.
   - Toplu katılımda tek mesaj.
   - 👋 Veda mesajı.
   - Hoş geldini özelden gönderme.
8. **Zamanlanmış mesajlar** — örn. "her 6 saatte bir kuralları butonlarıyla at".

Karar bekliyor: hepsi tek seferde mi, yoksa önce 1–7, sonra 8 mi?

## Ertelendi
- **Federasyon** — farklı sahiplerin grupları ortak ban listesine katılır. Grup ağı şu an sadece aynı sahibin gruplarında çalışıyor.
- **Klon token şifreleme** — bot sahibi şimdilik gerek görmedi.
