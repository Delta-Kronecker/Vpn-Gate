import requests
from bs4 import BeautifulSoup
import re
import time
import os
import sys
import subprocess
import zipfile
import csv
import io
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

IS_GITHUB = os.getenv('GITHUB_ACTIONS') == 'true'
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

MAIN_URL = "https://www.vpngate.net/en/"
MIRROR_LIST_URL = "https://www.vpngate.net/en/sites.aspx"
API_PATH = "api/iphone/"

OUTPUT_FILE = "sstp_hosts.txt"
RAW_FILE = "sstp_hosts_raw.txt"

if not IS_GITHUB:
    OUTPUT_FILE = os.path.join(os.getcwd(), OUTPUT_FILE)
    RAW_FILE = os.path.join(os.getcwd(), RAW_FILE)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}

def get_active_mirrors():
    
    mirrors = []
    print(f"🔍 Fetching mirror list from {MIRROR_LIST_URL}...")
    try:
        response = requests.get(MIRROR_LIST_URL, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
    
        for link in soup.find_all('a', href=True):
            href = link['href']
            if "vpngate" in href and "http" in href and href.count('/') <= 3:
                clean_url = href.rstrip('/')
                if clean_url not in mirrors:
                    mirrors.append(clean_url)
        
        print(f" Found {len(mirrors)} mirrors.")
    except Exception as e:
        print(f" Error fetching")
    if "http://www.vpngate.net" not in mirrors:
        mirrors.insert(0, "http://www.vpngate.net")
        
    return mirrors

def extract_from_html(url):

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

    csv_url = f"{base_url}/{API_PATH}"
    hosts = []
    try:
        response = requests.get(csv_url, timeout=10)
        if response.status_code != 200: return []
        return parse_vpngate_csv(response.text)
    except:
        pass
    return hosts


def parse_vpngate_csv(content):
    """Shared parser for any mirror of the official /api/iphone/ CSV."""
    hosts = []
    try:
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


# Additional VPN Gate mirror / data sources
GEORGEXIE_CSV = ("https://raw.githubusercontent.com/GeorgeXie2333/"
                 "vpngate-list-mirror/master/data/vpngate.csv")
FDCIABDUL_README = ("https://raw.githubusercontent.com/fdciabdul/"
                    "Vpngate-Scraper-API/main/README.md")

def fetch_georgexie_mirror():
    """data/vpngate.csv mirror of the official API (vpngate-list-mirror)."""
    hosts = []
    try:
        response = requests.get(GEORGEXIE_CSV, headers=HEADERS, timeout=15)
        if response.status_code == 200:
            return parse_vpngate_csv(response.text)
    except:
        pass
    return hosts

def fetch_fdciabdul_list():
    """README markdown table of Vpngate-Scraper-API (hostnames + IPs)."""
    hosts = []
    try:
        response = requests.get(FDCIABDUL_README, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return []
        for m in re.finditer(r'\|\s*([A-Za-z0-9\-_]+)\s*\|\s*(\d+\.\d+\.\d+\.\d+)\s*\|',
                             response.text):
            hostname = m.group(1)
            if ("." not in hostname) and "vpn" in hostname.lower():
                hostname = f"{hostname}.opengw.net"
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

def run_validation():
    """Run validate.py: connect-test each host with sstp-client and build
    sstp_hosts.txt grouped by country. Falls back gracefully if validation
    cannot run (returns False)."""
    cmd = [sys.executable, 'validate.py']
    try:
        result = subprocess.run(cmd, timeout=7200)
        if result.returncode == 0 and os.path.exists(OUTPUT_FILE):
            with open(OUTPUT_FILE, 'r', encoding='utf-8', errors='replace') as f:
                return any(ln.strip() for ln in f)
    except Exception as e:
        print(f"Validation pipeline error: {e}")
    return False


def write_flat_fallback(output_list):
    """Old-style plain list, in case the connection-validated file is missing."""
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        for line in sorted(output_list):
            f.write(line + '\n')


def parse_final_summary():
    """Read the generated sstp_hosts.txt: server entries + per-country counts."""
    servers = []
    countries = {}
    verified = False
    current = None
    with open(OUTPUT_FILE, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                if 'Real SSTP connection test' in line:
                    verified = True
                continue
            m = re.match(r'^=== (.+?) \(([A-Z]{2,3})\) ', line)
            if m:
                current = m.group(1)
                countries.setdefault(current, 0)
                continue
            if current:
                countries[current] += 1
            servers.append(line)
    return servers, countries, verified


def main():
    start_time = time.time()


    mirrors = get_active_mirrors()
    final_hosts_map = {}

    print("📥 Scraping Main HTML...")
    html_hosts = extract_from_html(MAIN_URL)
    for h in html_hosts:
        if ":" in h:
            domain, port = h.split(":")
            final_hosts_map[domain] = port
        else:
            final_hosts_map[h] = "443"

    print(f"📥 Downloading CSVs from {len(mirrors)} mirrors + GitHub sources (Parallel)...")

    fetch_jobs = [(fetch_csv_from_mirror, (url,)) for url in mirrors]
    fetch_jobs += [(fetch_georgexie_mirror, ()), (fetch_fdciabdul_list, ())]

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_source = {
            executor.submit(func, *args): args
            for func, args in fetch_jobs
        }

        for future in as_completed(future_to_source):
            found_hosts = future.result()
            for domain in found_hosts:
                if domain not in final_hosts_map:
                    final_hosts_map[domain] = "DEFAULT"

    # ---- Explicit dedupe pass (one entry per hostname, after collecting all) ----
    print(f"🧹 Deduplicating collected hosts ({len(final_hosts_map)} raw entries)...")
    deduped = {}
    for domain, port in final_hosts_map.items():
        domain = domain.strip().lower()
        if not domain or "." not in domain:
            continue
        if domain not in deduped:
            deduped[domain] = port
        elif deduped[domain] == "DEFAULT" and port != "DEFAULT":
            deduped[domain] = port  # prefer the real port from the HTML source
    final_hosts_map = deduped
    print(f"   -> {len(final_hosts_map)} unique hosts after dedupe.")

    output_list = []
    for domain, port in final_hosts_map.items():
        if "public-vpn" in domain:
            output_list.append(domain)
        else:
            if port == "DEFAULT":
                output_list.append(f"{domain}:443")
            else:
                output_list.append(f"{domain}:{port}")

    output_list.sort()

    # 1) Persist the raw collected list for the validator
    with open(RAW_FILE, 'w', encoding='utf-8') as f:
        for line in output_list:
            f.write(line + '\n')

    # 2) Connect-test every host with sstp-client and build the
    #    country-grouped sstp_hosts.txt
    print("🧪 Validating servers one-by-one over real SSTP connections...")
    validated = run_validation()
    if not validated:
        print("⚠️ Validation unavailable, writing plain list and continuing.")
        write_flat_fallback(output_list)

    servers, country_counts, verified = parse_final_summary()
    total_hosts = len(servers)

    if verified:
        top_names = sorted(country_counts.items(), key=lambda kv: -kv[1])
        print("📊 Countries:")
        for name, count in top_names:
            print(f"   - {name}: {count}")

    public_vpn_count = sum(1 for h in servers if "public-vpn" in h)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary_report = f"*VPN Gate sstp Collector*\nDate: `{now}`\n\n"
    summary_report += f"*Verified Live (Connected):* `{total_hosts}`\n"
    summary_report += f"Public-VPN (443 Port): {public_vpn_count}\n"
    summary_report += f"Others (With Port): {total_hosts - public_vpn_count}\n"
    summary_report += "Countries:\n"
    summary_lines = [f"- {name}: {cnt}" for name, cnt in
                     sorted(country_counts.items(), key=lambda kv: -kv[1])]
    summary_report += '\n'.join(summary_lines[:15])
    summary_report += f"\n{'-'*25}\n"
    summary_report += f"*Total Unique Hosts:* `{total_hosts}`"

    print(summary_report)

    zip_name = "vpnGate_SSTP.zip"
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(OUTPUT_FILE):
            zipf.write(OUTPUT_FILE, os.path.basename(OUTPUT_FILE))

    if IS_GITHUB:
        send_to_telegram(zip_name, summary_report)

if __name__ == "__main__":
    main()
