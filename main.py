import requests
from bs4 import BeautifulSoup
import re
import time
import os
import zipfile
import csv
import io
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
IS_GITHUB = os.getenv('GITHUB_ACTIONS') == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

MAIN_URL = "https://www.vpngate.net/en/"
MIRROR_LIST_URL = "https://www.vpngate.net/en/sites.aspx"
API_PATH = "api/iphone/"

OUTPUT_FILE = "sstp_hosts.txt"

if not IS_GITHUB:
    OUTPUT_FILE = os.path.join(os.getcwd(), OUTPUT_FILE)

# هدر مرورگر برای جلوگیری از مسدود شدن
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def get_active_mirrors():
    """
    دریافت لیست سایت‌های آینه‌ای فعال از صفحه مخصوص آن
    """
    mirrors = []
    print(f"🔍 Fetching mirror list from {MIRROR_LIST_URL}...")
    try:
        response = requests.get(MIRROR_LIST_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # پیدا کردن لینک‌هایی که شبیه آدرس میرور هستند
        # معمولا در تگ‌های <strong> یا لیست‌ها قرار دارند
        # الگوی میرورها معمولا http://vpngate-IP... است
        for link in soup.find_all('a', href=True):
            href = link['href']
            if "vpngate" in href and "http" in href and href.count('/') <= 3:
                # تمیز کردن URL (حذف اسلش آخر)
                clean_url = href.rstrip('/')
                if clean_url not in mirrors:
                    mirrors.append(clean_url)
        
        print(f"✅ Found {len(mirrors)} mirrors.")
    except Exception as e:
        print(f"⚠️ Error fetching mirrors: {e}")
    
    # همیشه سایت اصلی را هم به عنوان اولین گزینه اضافه می‌کنیم
    if "http://www.vpngate.net" not in mirrors:
        mirrors.insert(0, "http://www.vpngate.net")
        
    return mirrors

def extract_from_html(url):
    """استخراج از HTML برای پورت‌های خاص"""
    hosts = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200: return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        pattern = re.compile(r"SSTP Hostname\s*:\s*([a-zA-Z0-9\.\-]+(?::\d+)?)")
        elements = soup.find_all(string=re.compile("SSTP Hostname"))

        for element in elements:
            parent_text = element.parent.get_text()
            match = pattern.search(parent_text)
            if match:
                hosts.append(match.group(1))
    except:
        pass
    return hosts

def fetch_csv_from_mirror(base_url):
    """دانلود و پردازش CSV از یک آدرس خاص"""
    csv_url = f"{base_url}/{API_PATH}"
    hosts = []
    try:
        response = requests.get(csv_url, timeout=10)
        if response.status_code != 200: return []
        
        content = response.text
        lines = content.splitlines()
        csv_clean_lines = [line[1:] if line.startswith("#HostName") else line 
                          for line in lines if (line.startswith("#HostName") or (not line.startswith("*") and not line.startswith("#")))]
        
        f = io.StringIO("\n".join(csv_clean_lines))
        reader = csv.DictReader(f)
        
        for row in reader:
            hostname = row.get("HostName")
            if hostname:
                if ".opengw.net" not in hostname:
                    hosts.append(f"{hostname}.opengw.net")
                else:
                    hosts.append(hostname)
    except:
        pass
    return hosts

def send_to_telegram(file_path, caption):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, 'rb') as f:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}, files={'document': f})
    except Exception as e:
        print(f"Telegram Error: {e}")

def main():
    start_time = time.time()
    
    # 1. دریافت لیست میرورها
    mirrors = get_active_mirrors()
    
    # لیست نهایی (استفاده از دیکشنری برای جلوگیری از تکرار و مدیریت پورت)
    # Key: Domain, Value: Port (or None)
    final_hosts_map = {}

    # 2. دریافت از HTML سایت اصلی (برای پورت‌های دقیق)
    print("📥 Scraping Main HTML...")
    html_hosts = extract_from_html(MAIN_URL)
    for h in html_hosts:
        if ":" in h:
            domain, port = h.split(":")
            final_hosts_map[domain] = port
        else:
            final_hosts_map[h] = "443"

    # 3. دریافت CSV از تمام میرورها (به صورت همزمان برای سرعت بیشتر)
    print(f"📥 Downloading CSVs from {len(mirrors)} sources (Parallel)...")
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {executor.submit(fetch_csv_from_mirror, url): url for url in mirrors}
        
        for future in as_completed(future_to_url):
            found_hosts = future.result()
            for domain in found_hosts:
                # اگر دامنه قبلاً با پورت خاص (از HTML) اضافه نشده بود، اضافه کن
                if domain not in final_hosts_map:
                    final_hosts_map[domain] = "DEFAULT" # علامت‌گذاری برای پردازش نهایی

    # 4. فرمت‌دهی نهایی طبق درخواست شما
    # public-vpn -> بدون پورت
    # بقیه -> اگر پورت خاص داشت (از HTML) همان پورت، اگر نه -> 443
    
    output_list = []
    for domain, port in final_hosts_map.items():
        if "public-vpn" in domain:
            output_list.append(domain) # بدون پورت
        else:
            if port == "DEFAULT":
                output_list.append(f"{domain}:443")
            else:
                output_list.append(f"{domain}:{port}")

    output_list.sort()
    
    # آمار
    public_vpn_count = sum(1 for h in output_list if "public-vpn" in h)
    
    # ذخیره
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for line in output_list:
            f.write(line + '\n')
            
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary_report = f"🌐 *VPN Gate Ultimate Collector*\n📅 Date: `{now}`\n\n"
    summary_report += f"🔍 Sources Checked: {len(mirrors)} Mirrors\n"
    summary_report += f"📊 Stats:\n"
    summary_report += f"🔹 Public-VPN (No Port): {public_vpn_count}\n"
    summary_report += f"🔹 Others (With Port): {len(output_list) - public_vpn_count}\n"
    summary_report += f"{'-'*25}\n"
    summary_report += f"✅ *Total Unique Hosts:* `{len(output_list)}`\n"
    summary_report += f"⏱ Time: `{int(time.time() - start_time)}s`"

    print(summary_report)

    zip_name = "SSTP_Ultimate.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(OUTPUT_FILE):
            zipf.write(OUTPUT_FILE, os.path.basename(OUTPUT_FILE))

    if IS_GITHUB:
        send_to_telegram(zip_name, summary_report)

if __name__ == "__main__":
    main()
