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

## Sırada (onaylandı, yapılacak)
1. **Duyuru tıklama istatistiği** — duyurudaki butona kaç kişi tıkladı (/duyurular'da görünür).
2. **Hedefli duyuru** — `-aktif` (son 7 günde aktif kişiler), `-sec` (listeden belli grupları seçme).
3. **Hoş geldin olarak mesaj kopyalama** — yanıtlanan mesaj olduğu gibi (premium emoji dahil) hoş geldin olur; değişkenler bu modda çalışmaz.
4. **Haftalık büyüme raporu** — her pazartesi sahibine özelden: yeni/ayrılan grup, yeni kişi, en aktif gruplar.

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

## Ertelendi
- **Federasyon** — farklı sahiplerin grupları ortak ban listesine katılır. Grup ağı şu an sadece aynı sahibin gruplarında çalışıyor.
- **Klon token şifreleme** — bot sahibi şimdilik gerek görmedi.
