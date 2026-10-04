# ULUS — Yol haritası (kesinleşti)

Her faz bitince: testler → zip → canlı deneme → sonraki faz.

## Faz 1 — Zengin mesaj sistemi ✅ (tamamlandı)
Hoş geldin, veda, kurallar, notlar, /filter, duyuru ve zamanlanmış mesajlar tek ortak altyapıyı kullanır.
Sonraki fazlardaki mesajlar (çekiliş, kanal zorunluluğu uyarısı) da bunu kullanır; bu yüzden ilk sırada.

1. **Medya** — resim, video, GIF, sticker, dosya, ses. Medyaya yanıt verip `/setwelcome` yazmak yeterli.
2. **Butonlar** — iki yazım da desteklenir:
   ```
   Kanalımız - https://t.me/kanal && Destek - https://t.me/destek
   Kuralları Oku - rules
   ```
   ve Rose tarzı `[Kanal](buttonurl://t.me/kanal)` (`:same` ile aynı satır).
   - Özel butonlar: 📜 kurallar (özelden), 📝 not, 💬 popup uyarı, renkli butonlar.
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

## Faz 2 — Etkileşim ✅ (tamamlandı)
1. **`/etiket <mesaj>`** — gruptaki üyeleri etiketleyerek mesaj atar.
   - Grup başına 1, 5 veya 10 kişi aynı mesajda (varsayılan 5).
   - İsimle ya da emojiyle (gizli etiket) etiketleme.
   - Herkes ya da sadece aktifler (son 7 gün).
   - `/etiketdur` ile durur; aynı anda bir etiketleme; bitince 10 dk bekleme.
   - Üye `/etiketme` ile kendini listeden çıkarabilir.
   - Botlar ve silinmiş hesaplar atlanır; Admin ve üstü kullanabilir.
   - Telegram sınırı: grupta dakikada ~20 mesaj. 300 kişi, 5'erli = 60 mesaj ≈ 3–4 dk.
   - Bot API grubun tüm üye listesini vermez. Bot, gördüğü (yazan/katılan) üyeleri etiketler.
     Userbot (API_ID/API_HASH) açıksa ve o hesap gruptaysa tüm üye listesi kullanılır.
2. **Kanal zorunluluğu** — grupta yazmak için belirlenen kanala katılmak gerekir.
   - Katılmayanın mesajı silinir; "📢 Kanala katıl → ✅ Katıldım" butonlu uyarı gelir.
3. **AFK** — `/afk sebep`; etiketlenince "şu an AFK: sebep (2 saattir)"; tekrar yazınca kalkar.

## Faz 3 — Güvenlik ✅ (tamamlandı)
1. **Ortak spam kara listesi** — botun bir grubunda spam yüzünden banlanan hesap, diğer gruplara katılınca "şüpheli" işaretlenir.
   - İsteğe bağlı CAS (dünya çapında bilinen spam listesi) kontrolü.
2. **Kullanıcı sicili (`/sicil`)** — kişinin botun tüm gruplarındaki uyarı, susturma ve ban geçmişi; sadece yetkililere açık.
3. **İsim değişikliği takibi** — ad veya kullanıcı adı değişince log kanalına kayıt; eski isimler `/sicil`'de görünür.
4. **Oylamalı susturma (`/oylama`)** — admin yokken üyeler oyla geçici susturur (örn. 5 oy → 1 saat).
   - Yeni üyeler oy veremez; yetkililere karşı kullanılamaz.

## Faz 4 — Çekiliş ve istatistik ✅ (tamamlandı)
1. **Çekiliş sistemi** (temel `/cekilis` zaten var; şartlar ve sahte hesap engeli eklenecek) — `/cekilis` ile "🎁 Katıl" butonlu çekiliş; süre bitince kazanan rastgele seçilip duyurulur.
   - Katılım şartı konabilir: kanal üyeliği, en az X mesaj, X gündür grupta olmak.
   - Yeni açılmış ve sahte hesaplar katılamaz.
2. **Grafikli istatistik** — `/stats`: son 7 ve 30 günün aktivitesi, en aktif saatler, katılan/ayrılan; resim olarak grafik (matplotlib).

## Faz 5 — Telegram Mini App paneli ✅ (tamamlandı)
- Ayarlar Telegram içinde açılan web sayfasından yönetilir: sekmeler, açma/kapama anahtarları, önizleme.
- PythonAnywhere web uygulaması üzerinde çalışır; giriş Telegram doğrulamasıyla.

## Duyuru sistemi ✅ (tamamlandı)
- `/duyuru -kisiler -gruplar -kanallar "mesaj"` (birleştirilebilir), `all` = hepsi; varsayılan gruplar + kanallar.
- Mesaja yanıtla `/duyuru` → olduğu gibi kopyalanır (medya, biçim, premium emoji).
- Önizleme + ✅ Gönder / ❌ İptal; arka planda gönderim, tek ilerleme mesajı, rapor; `/duyurudur`.
- `-test`, `-sabitle`, `-sessiz`, `-saat 20:00`; `/duyurular` geçmiş; kişiler 🔕 / `/duyurukapat` ile kapatır.
- Özelden kullananlar kaydedilir; botu engelleyen ve başlatmamış olan bir kez denenip atlanır. Klon sahibi kendi kitlesine gönderir.

## Büyüme ve yönetim paketi ✅ (tamamlandı)
**A — Duyuru eklentileri**
- 👆 Buton tıklama sayacı (web panel adresinden yönlendirme; kişi bazında) → /duyurular
- 🔥 `-aktif [gün]`, 🎯 önizlemeden grup/kanal seçimi
- 💾 Şablonlar: `/duyuru -kaydet isim`, `/duyuru #isim`, `/duyurusablon`
- 🗳 Anket: `/duyuru -anket "Soru\nA\nB"` — tek anket iletilir, oylar tek yerde

**B — Rapor ve takip**
- 💎 Premium emojili mesaja yanıtla `/setwelcome` (`/setgoodbye`, `/setrules`) → birebir kopya
- 📈 Haftalık büyüme raporu (pazartesi 10:00, her bot kendi sahibine) + `/buyume`
- ➕/➖ Eklenme/çıkarılma bildirimi, ekleyene teşekkür
- ⚠️ 24 saatte yönetici yapılmazsa "beni yönetici yap" hatırlatması
- 🗄 Gece yedeği zip olarak

**C — Yönetim ve iletişim**
- 🔗 Davet yarışması: `/davet`, `/davetler [hafta]`
- 📋 Toplu ayar: /panel → grup → ayarlarını diğer gruplara uygula
- 🛠 Bakım modu: `/bakim 30|ac|kapat [-duyur]`
- 💬 Destek hattı: özelden gelen mesaj sahibine, yanıt kullanıcıya; `/destek ac|kapat`

## Admin denetimi ✅ (tamamlandı)
- ✏️ Geç düzenleme koruması kapsamı: üyeler / +admin / +üst admin / kurucu hariç herkes (sadece kurucu değiştirir)
- 📋 Günlük admin özeti (`/denetim`): kim kaç ban, susturma, uyarı, silme yaptı
- 🚨 Admin işlem sınırı: 1 saatte sınırı aşan yetkilinin yetkileri askıya alınır, banları tek tuşla geri alınır
- 🗑 `/del` ve `/purge` ile silinen mesajların log kanalına kopyası (Telegram elle silmeleri botlara bildirmez)
- 🕓 `/gecmis`: mesaj düzenleme geçmişi

## Çok dil ✅ (tamamlandı)
- Ana dil Türkçe; `/start` altında İngilizce kısa açıklama ve `/setlang` ipucu
- 15 dil: English, Русский, Українська, Azərbaycan, Oʻzbek, Қазақ, العربية, فارسی, Español, Português,
  Indonesia, Deutsch, Français, Italiano, हिन्दी
- Dil seçimi: özelde kişiye, grupta gruba (`/setlang`, ayar paneli, web panel); log kanalı bağlı grubun dilinde
- Komut menüsü Telegram uygulamasının diline göre; İngilizce komut adları (Türkçe adlar da çalışır)
- Yöneticilerin yazdığı içerik (hoş geldin, kurallar, notlar, filtreler) çevrilmez
- Katalogda olmayan satır İngilizceye düşer

## Önerildi, şimdilik seçilmedi
- Davet yarışması (haftalık/aylık davet sıralaması)
- Hesap güven puanı (hesap yaşı, profil fotoğrafı, biyografideki link, premium)
- Seviye ve rozet sistemi (XP, unvanlar)
- Destek hattı (üyeden yetkililere anonim mesaj)
- Telegram Stars ile premium (ücretli klon/özellikler)
- Yapay zekâ moderasyonu (anlamdan hakaret, dolandırıcılık ve spam tespiti)
- Admin taklitçisi koruması
- Yetkili performans raporu + pasif admin uyarısı
- Ayar şablonları + yedekle / geri yükle
- Konuya (topic) özel kurallar
- Bilgi yarışması
- Toplu saldırı alarmı (birden fazla grupta aynı anda spam/raid)
- Şüpheli hesap puanı ile otomatik doğrulama

## Ertelendi
- **Federasyon** — farklı sahiplerin grupları ortak ban listesine katılır. Grup ağı şu an sadece aynı sahibin gruplarında çalışıyor.
- **Klon token şifreleme** — bot sahibi şimdilik gerek görmedi.
