# موقع الاستعلام عن النتيجة

## يوم صدور النتيجة (5 دقائق)
```bash
pip install -r requirements.txt
python ingest.py civil_s1.pdf --program civil --term 1 --label "الهندسة المدنية - الفصل الأول"
python ingest.py arch_s1.pdf  --program arch  --term 1 --label "العمارة - الفصل الأول"
# افتح review/<name>/report.html (ملف واحد مستقل) وقارن صور الصفوف بالبيانات
git add data && git commit -m results && git push
```
- لا يُنشر شيء إذا فشل أي فحص (خروج برمز 1). `--force` فقط بعد مراجعة التقرير.
- المدنية والعمارة والفصلان منفصلة تماماً (ملف لكل `program_sN`)، والنظام يعرف البرنامج من وجود الرقم الجامعي في ملفه.
- `data/demo_s8.*` نتيجة تجريبية: احذفها قبل النشر.

## التشغيل
```bash
python server.py                                  # محلياً: http://localhost:8000
gunicorn server:app --workers 1 --threads 4       # إنتاج (Render/Railway)
```
متغيرات البيئة: `TRUST_PROXY=1` خلف وسيط، و`TURNSTILE_SITE_KEY` + `TURNSTILE_SECRET` لتفعيل CAPTCHA.
**المستودع يجب أن يكون خاصاً (Private)** لأن مجلد data يحوي النتائج. الخادم يرفض الإقلاع إذا لم يطابق الـJSON بصمة الـPDF.
