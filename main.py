import requests
from bs4 import BeautifulSoup
import re
import time
import os
import zipfile
import csv
import io
from datetime import datetime

# --- Configuration ---
IS_GITHUB = os.getenv('GITHUB_ACTIONS') == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# لیست منابع: هم سایت اصلی (برای پورت‌های خاص) و هم API (برای لیست کامل)
VPN_SOURCES = [
    {"type": "html", "url": "https://www.vpngate.net/en/"},
    {"type": "csv", "url": "http://www.vpngate.net/api/iphone/"} # این لینک دیتابیس اصلی برنامه است
]

OUTPUT_FILE = "sstp_hosts.txt"

if not IS_GITHUB:
    OUTPUT_FILE = os.path.join(os.getcwd(), OUTPUT_FILE)

def extract_from_html(url):
    """استخراج از صفحه وب برای پیدا کردن پورت‌های خاص"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    hosts = []
    print(f"Scraping HTML from {url}...")
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        # پترن برای پیدا کردن آدرس به همراه پورت
        pattern = re.compile(r"SSTP Hostname\s*:\s*([a-zA-Z0-9\.\-]+(?::\d+)?)")
        
        elements = soup.find_all(string=re.compile("SSTP Hostname"))

        for element in elements:
            parent_text = element.parent.get_text()
            match = pattern.search(parent_text)
            if match:
                host = match.group(1)
                hosts.append(host)
    except Exception as e:
        print(f"Error scraping HTML: {e}")
    
    return hosts

def extract_from_csv(url):
    """استخراج از فایل CSV (دیتابیس اصلی برنامه SoftEther)"""
    hosts = []
    print(f"Downloading CSV from {url}...")
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        # پردازش فایل CSV
        # خطوط اول فایل توضیحات هستند، باید رد شوند تا به هدر برسیم
        content = response.text
        lines = content.splitlines()
        
        # پیدا کردن خط شروع دیتا (معمولا با HostName شروع می‌شود یا بعد از *vpn_servers)
        csv_data = []
        start_reading = False
        
        for line in lines:
            if line.startswith("#HostName") or line.startswith("HostName"):
                start_reading = True
                # حذف # از ابتدای هدر اگر باشد
                csv_data.append(line.replace("#", "")) 
                continue
            
            if start_reading and line.strip() != "":
                csv_data.append(line)

        # تبدیل به فرمت قابل خواندن برای ماژول csv
        f = io.StringIO("\n".join(csv_data))
        reader = csv.DictReader(f)
        
        for row in reader:
            # ستون HostName حاوی آدرس است (مثلا vpn123.opengw.net)
            hostname = row.get("HostName")
            if hostname and "opengw.net" in hostname:
                # در فایل CSV پورت SSTP معمولا ذکر نمی‌شود چون پیش‌فرض 443 است.
                # اما ما خود آدرس را اضافه می‌کنیم.
                # اگر بخواهید پورت 443 را زورکی اضافه کنید: f"{hostname}:443"
                hosts.append(f"{hostname}:443") 

    except Exception as e:
        print(f"Error downloading CSV: {e}")
    
    return hosts

def send_to_telegram(file_path, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            response = requests.post(url, data={
                'chat_id': TELEGRAM_CHAT_ID, 
                'caption': caption,
                'parse_mode': 'Markdown'
            }, files={'document': f})
        print(f"Telegram Response: {response.status_code}")
    except Exception as e:
        print(f"Telegram Error: {e}")

def main():
    start_time = time.time()
    all_hosts = set() # استفاده از set برای حذف تکراری‌ها
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1. دریافت از HTML (برای پورت‌های خاص)
    html_hosts = extract_from_html(VPN_SOURCES[0]['url'])
    for h in html_hosts:
        all_hosts.add(h)

    # 2. دریافت از CSV (برای لیست کامل مشابه برنامه)
    csv_hosts = extract_from_csv(VPN_SOURCES[1]['url'])
    for h in csv_hosts:
        all_hosts.add(h)

    # تبدیل به لیست و مرتب‌سازی
    sorted_hosts = sorted(list(all_hosts))

    # ذخیره در فایل
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for line in sorted_hosts:
            f.write(line + '\n')
    
    # گزارش
    summary_report = f"🌐 *VPN Gate Full Update Report*\n📅 Date: `{now}`\n\n"
    summary_report += f"Sources:\n"
    summary_report += f"🔹 HTML Scrape: {len(html_hosts)}\n"
    summary_report += f"🔹 CSV API: {len(csv_hosts)}\n"
    summary_report += f"{'-'*25}\n"
    summary_report += f"✅ *Total Unique Hosts:* `{len(sorted_hosts)}`\n"
    summary_report += f"⏱ Time: `{int(time.time() - start_time)}s`"

    print(summary_report)

    # فشرده‌سازی و ارسال
    zip_name = "SSTP_Full_List.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(OUTPUT_FILE):
            zipf.write(OUTPUT_FILE, os.path.basename(OUTPUT_FILE))

    if IS_GITHUB:
        send_to_telegram(zip_name, summary_report)

if __name__ == "__main__":
    main()
