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

VPN_SOURCES = [
    {"type": "html", "url": "https://www.vpngate.net/en/"},
    {"type": "csv", "url": "http://www.vpngate.net/api/iphone/"}
]

OUTPUT_FILE = "sstp_hosts.txt"

if not IS_GITHUB:
    OUTPUT_FILE = os.path.join(os.getcwd(), OUTPUT_FILE)

def extract_from_html(url):
    """
    استخراج آدرس‌ها به همراه پورت دقیق از صفحه HTML
    خروجی: لیستی از رشته‌ها مثل 'vpn123.opengw.net:1661'
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    hosts = []
    print(f"Scraping HTML from {url}...")
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        # پترن برای پیدا کردن آدرس و پورت
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

def extract_from_csv(url, existing_domains):
    """
    استخراج از CSV با شرط:
    1. اگر public-vpn بود -> بدون پورت
    2. اگر غیر public-vpn بود -> چک کن اگر در HTML نبود، با پورت 443 اضافه کن
    """
    hosts = []
    print(f"Downloading CSV from {url}...")
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        
        content = response.text
        lines = content.splitlines()
        
        csv_clean_lines = []
        start_reading = False
        
        for line in lines:
            if line.startswith("#HostName"):
                start_reading = True
                csv_clean_lines.append(line[1:]) 
                continue
            
            if start_reading and line.strip() != "":
                csv_clean_lines.append(line)

        f = io.StringIO("\n".join(csv_clean_lines))
        reader = csv.DictReader(f)
        
        for row in reader:
            hostname = row.get("HostName")
            
            if hostname:
                # ساخت آدرس کامل
                if ".opengw.net" not in hostname:
                    full_domain = f"{hostname}.opengw.net"
                else:
                    full_domain = hostname
                
                # --- منطق فیلتر کردن طبق درخواست شما ---
                
                # حالت 1: اگر public-vpn است -> بدون پورت اضافه کن
                if "public-vpn" in full_domain:
                    hosts.append(full_domain)
                
                # حالت 2: اگر public-vpn نیست (مثل vpn123...)
                else:
                    # چک می‌کنیم آیا این دامنه قبلاً در HTML (که پورت دقیق دارد) پیدا شده؟
                    if full_domain not in existing_domains:
                        # اگر نبود، با پورت 443 اضافه کن
                        hosts.append(f"{full_domain}:443")
                    # اگر بود، کاری نمی‌کنیم (چون نسخه HTML پورت دقیق‌تری دارد)

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
    final_list = []
    
    # ست برای نگهداری دامنه‌هایی که در HTML پیدا شدند (برای جلوگیری از تکرار در CSV)
    html_domains_seen = set()

    # 1. دریافت از HTML (این‌ها پورت دقیق دارند مثل :1661)
    html_hosts = extract_from_html(VPN_SOURCES[0]['url'])
    
    for h in html_hosts:
        final_list.append(h)
        # جدا کردن دامنه از پورت برای بررسی بعدی
        # مثلا vpn123.opengw.net:1661 -> vpn123.opengw.net
        domain_only = h.split(':')[0]
        html_domains_seen.add(domain_only)

    # 2. دریافت از CSV (با منطق خاص: public-vpn بدون پورت)
    csv_hosts = extract_from_csv(VPN_SOURCES[1]['url'], html_domains_seen)
    
    for h in csv_hosts:
        # چک نهایی برای تکراری نبودن کل رشته
        if h not in final_list:
            final_list.append(h)

    # مرتب‌سازی الفبایی
    final_list.sort()
    
    # آمارگیری
    public_vpn_count = sum(1 for h in final_list if "public-vpn" in h)
    other_count = len(final_list) - public_vpn_count
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ذخیره در فایل
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for line in final_list:
            f.write(line + '\n')
    
    # گزارش
    summary_report = f"🌐 *VPN Gate Custom List*\n📅 Date: `{now}`\n\n"
    summary_report += f"📊 Breakdown:\n"
    summary_report += f"🔹 Public-VPN (No Port): {public_vpn_count}\n"
    summary_report += f"🔹 Others (With Port): {other_count}\n"
    summary_report += f"{'-'*25}\n"
    summary_report += f"✅ *Total Hosts:* `{len(final_list)}`\n"
    summary_report += f"⏱ Time: `{int(time.time() - start_time)}s`"

    print(summary_report)

    # فشرده‌سازی
    zip_name = "SSTP_List.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(OUTPUT_FILE):
            zipf.write(OUTPUT_FILE, os.path.basename(OUTPUT_FILE))

    if IS_GITHUB:
        send_to_telegram(zip_name, summary_report)

if __name__ == "__main__":
    main()
