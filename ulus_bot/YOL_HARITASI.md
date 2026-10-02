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

## Onaylanan yeni özellikler (henüz yazılmadı)
1. **Kanal zorunluluğu** — grupta yazmak için belirlenen kanala katılmak gerekir.
   - Katılmayanın mesajı silinir; "📢 Kanala katıl → ✅ Katıldım" butonlu uyarı gelir.
2. **Çekiliş sistemi** — `/cekilis` ile "🎁 Katıl" butonlu çekiliş; süre bitince kazanan rastgele seçilip duyurulur.
   - Katılım şartı konabilir: kanal üyeliği, en az X mesaj, X gündür grupta olmak.
   - Yeni açılmış ve sahte hesaplar katılamaz.
3. **Ortak spam kara listesi** — botun bir grubunda spam yüzünden banlanan hesap, diğer gruplara katılınca "şüpheli" işaretlenir.
   - İsteğe bağlı CAS (dünya çapında bilinen spam listesi) kontrolü.
4. **Kullanıcı sicili (`/sicil`)** — kişinin botun tüm gruplarındaki uyarı, susturma ve ban geçmişi; sadece yetkililere açık.

## Önerildi, şimdilik seçilmedi
- Davet yarışması (haftalık/aylık davet sıralaması)
- Hesap güven puanı (hesap yaşı, profil fotoğrafı, biyografideki link, premium)
- Seviye ve rozet sistemi (XP, unvanlar)
- Destek hattı (üyeden yetkililere anonim mesaj)
- Telegram Stars ile premium (ücretli klon/özellikler)
- Yapay zekâ moderasyonu (anlamdan hakaret, dolandırıcılık ve spam tespiti)

## Ertelendi
- **Federasyon** — farklı sahiplerin grupları ortak ban listesine katılır. Grup ağı şu an sadece aynı sahibin gruplarında çalışıyor.
- **Klon token şifreleme** — bot sahibi şimdilik gerek görmedi.
