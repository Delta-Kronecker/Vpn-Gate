import requests
from bs4 import BeautifulSoup
import re
import time
import os
import zipfile
from datetime import datetime

# --- Configuration ---
IS_GITHUB = os.getenv('GITHUB_ACTIONS') == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

VPN_SOURCE = {
    "url": "https://www.vpngate.net/en/",
    "output_file": "sstp_hosts.txt"
}

if not IS_GITHUB:
    VPN_SOURCE['output_file'] = os.path.join(os.getcwd(), VPN_SOURCE['output_file'])

def extract_sstp_hosts(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    hosts = []
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        pattern = re.compile(r"SSTP Hostname\s*:\s*([a-zA-Z0-9\.\-]+(?::\d+)?)")
        
        elements = soup.find_all(string=re.compile("SSTP Hostname"))

        for element in elements:
            parent_text = element.parent.get_text()
            match = pattern.search(parent_text)
            if match:
                host = match.group(1)
                if host not in hosts:
                    hosts.append(host)
    except Exception as e:
        print(f"Error scraping: {e}")
    
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
    generated_files = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    summary_report = f"🌐 *VPN Gate SSTP Update Report*\n📅 Date: `{now}`\n\n"
    summary_report += f"{'Type':<12} | {'Count'}\n"
    summary_report += f"{'-'*25}\n"

    # Process Source
    print(f"Scraping {VPN_SOURCE['url']}...")
    sstp_list = extract_sstp_hosts(VPN_SOURCE['url'])
    
    with open(VPN_SOURCE['output_file'], 'w', encoding='utf-8') as f:
        for line in sstp_list:
            f.write(line + '\n')
    
    generated_files.append(VPN_SOURCE['output_file'])
    
    summary_report += f"{'SSTP':<12} | {len(sstp_list)}\n"
    summary_report += f"{'-'*25}\n"
    summary_report += f"⏱ Time: `{int(time.time() - start_time)}s`"

    zip_name = "SSTP_Configs.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file in generated_files:
            if os.path.exists(file):
                zipf.write(file, os.path.basename(file))
    
    print(summary_report)

    if IS_GITHUB:
        send_to_telegram(zip_name, summary_report)

if __name__ == "__main__":
    main()
