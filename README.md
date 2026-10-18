# 🤖 Telegram Group Manager — v2

یک سیستم حرفه‌ای و تخصصی برای مدیریت خودکار گروه‌های تلگرام.

---

## ✨ ویژگی‌های کامل

| دسته | قابلیت |
|------|---------|
| 🔍 کشف | لینک‌های پیام‌ها و بیوگرافی اعضای فعال و غیرفعال گروه‌های عضو‌شده |
| 🚀 عضویت | صف FIFO با تأخیر تصادفی ۴–۸ دقیقه (ضد شناسایی) |
| 📊 محدودیت | حداکثر ۵۰ عضویت در روز (قابل تنظیم) |
| 🔄 تلاش مجدد | عضویت‌های ناموفق را خودکار retry می‌کند |
| 📢 ارسال | ارسال همگانی به گروه‌ها یا کاربران در پس‌زمینه |
| 💾 بکاپ | بکاپ روزانه اتوماتیک + آپلود به S3/Cloudflare R2 |
| ❤️ سلامت | مانیتورینگ مداوم client + هشدار فوری به ادمین |
| 📥 خروجی | Export CSV گروه‌ها و کاربران |
| 🗂️ مدیریت | صفحه‌بندی کامل لیست گروه‌ها |
| 📋 آمار | گزارش روزانه خودکار + داشبورد آمار لحظه‌ای |
| 🔐 امنیت | Redis FSM + Audit log کامل + SIGTERM handler |
| 🔎 فیلتر | فیلتر کلمات کلیدی برای کشف هوشمند |

---

## 📋 پیش‌نیازها

- Python 3.12+
- PostgreSQL 15+
- Redis 7+ (برای FSM — اختیاری اما توصیه‌شده)
- Docker + Docker Compose

---

## 🚀 راه‌اندازی محلی

### ۱. تنظیم محیط
```bash
cd telegram-manager
cp .env.example .env
# فایل .env را ویرایش کنید
```

### ۲. دریافت Session String (یک بار)
```bash
pip install telethon
python -m app.cli login
# کد دریافت‌شده را وارد کنید
# Session String را کپی کرده در TELEGRAM_SESSION_STRING بگذارید
```

### ۳. اجرا با Docker Compose
```bash
docker compose up -d
```

---

## ☁️ استقرار روی Render

### ۱. Push به GitHub
```bash
git init && git add . && git commit -m "init"
git remote add origin <your-repo>
git push -u origin main
```

### ۲. ایجاد سرویس در Render
- New → Blueprint → فایل `render.yaml` را انتخاب کنید

### ۳. تنظیم متغیرهای محیطی
```
TELEGRAM_API_ID       ← از my.telegram.org
TELEGRAM_API_HASH     ← از my.telegram.org
TELEGRAM_PHONE        ← شماره تلفن با کد کشور
TELEGRAM_SESSION_STRING ← از مرحله login
BOT_TOKEN             ← از @BotFather
ADMIN_IDS             ← شناسه عددی تلگرام شما
```

---

## ⚙️ تنظیمات مهم (`.env`)

```env
# کشف فقط پیام‌های حاوی این کلمات (خالی = همه)
DISCOVERY_KEYWORDS=پیج,گروه,join

# اسکن تدریجی بیو اعضای گروه‌ها (بدون ذخیره متن بیو)
BIO_SCAN_INTERVAL_HOURS=6
BIO_SCAN_MAX_USERS_PER_RUN=100
BIO_SCAN_MAX_USERS_PER_GROUP=25
BIO_SCAN_RECHECK_DAYS=30
BIO_SCAN_GROUP_RECHECK_DAYS=7
BIO_SCAN_DELAY_SECONDS=1

# حداکثر عضویت در روز
MAX_JOINS_PER_DAY=50

# تأخیر تصادفی بین عضویت‌ها (ثانیه)
JOIN_DELAY_MIN=240   # 4 دقیقه
JOIN_DELAY_MAX=480   # 8 دقیقه

# بکاپ روزانه (ساعت UTC)
BACKUP_DAILY_HOUR=3

# S3/Cloudflare R2 (اختیاری اما توصیه‌شده)
S3_BUCKET=your-bucket
S3_ENDPOINT=https://xxx.r2.cloudflarestorage.com
S3_ACCESS_KEY=xxx
S3_SECRET_KEY=xxx
```

---

## 🤖 دستورات ربات

| دکمه | عملکرد |
|------|---------|
| 📊 آمار | داشبورد آمار لحظه‌ای |
| 📋 لیست گروه‌ها | لیست با صفحه‌بندی ۱۵ تایی |
| ⏳ در انتظار | تایید / رد گروه‌ها |
| 🔴 ناموفق‌ها | گروه‌های join ناموفق + دکمه retry |
| 📨 ارسال | ارسال به یک یا همه گروه‌ها |
| 📢 ارسال همگانی | broadcast به گروه‌ها یا کاربران |
| 📥 خروجی | CSV گروه‌ها / CSV کاربران |
| 🚨 خطاها | آخرین ۲۰ خطای ثبت‌شده |
| 💾 بکاپ | دستی + لیست بکاپ‌ها |
| ❤️ وضعیت | سلامت client + صف |
| ▶️ / ⏹ | شروع / توقف user client |

---

## 🗄️ ساختار پروژه

```
telegram-manager/
├── app/
│   ├── config/          ← تنظیمات (pydantic-settings)
│   ├── database/        ← اتصال async SQLAlchemy
│   ├── handlers/        ← هندلرهای ربات (aiogram)
│   ├── middlewares/     ← احراز هویت ادمین
│   ├── models/          ← مدل‌های DB (SQLAlchemy)
│   ├── repositories/    ← لایه دسترسی داده
│   ├── services/        ← منطق اصلی کسب‌وکار
│   └── utils/           ← لاگر، validators
├── migrations/          ← Alembic migrations
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── render.yaml
└── requirements.txt
```

---

## ⚠️ نکات مهم

1. **Session String** را هرگز commit نکنید — فقط در env var
2. **ADMIN_IDS** باید ID عددی باشد (از @userinfobot بگیرید)
3. در production حتماً **Redis** را فعال کنید (FSM پایدار)
4. اگر اکانت soft-ban شود، ربات فوراً هشدار می‌دهد
5. بکاپ‌های `/tmp` بعد از restart پاک می‌شوند — **S3 را حتماً تنظیم کنید**
