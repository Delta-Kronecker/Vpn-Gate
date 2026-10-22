# validate.py
# Combined with the SSTP collector (main.py), this script:
#   1. Connects one by one to every collected SSTP host using sstp-client (Linux)
#   2. Verifies the server really established a tunnel
#   3. Determines the actual country of the server by sending a GeoIP request
#      through the tunnel (egress IP), then disconnects
#   4. Writes sstp_hosts.txt grouped by country (only connection-verified hosts)
#
# Fallbacks:
#   - If sstpc is not available (e.g. local Windows run) or --dns-only is given,
#     countries are determined via DNS-resolution + GeoIP "UNVERIFIED" mode so
#     the grouped output file is still produced.
#   - If too few hosts verify live (< SSTP_MIN_LIVE), the file is regenerated
#     in UNVERIFIED mode instead of committing a nearly-empty list.

import os
import re
import sys
import json
import time
import shutil
import socket
import subprocess
import requests
from datetime import datetime, timezone

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------

IS_GITHUB = os.getenv('GITHUB_ACTIONS') == 'true'


def _p(name):
    """Same path convention as main.py: plain name on GitHub, absolute locally."""
    return name if IS_GITHUB else os.path.join(os.getcwd(), name)


RAW_FILE = _p(os.getenv('SSTP_RAW_FILE') or 'sstp_hosts_raw.txt')
OUT_FILE = _p(os.getenv('SSTP_OUT_FILE') or 'sstp_hosts.txt')
LOG_DIR  = _p(os.getenv('SSTP_LOG_DIR') or 'validate_logs')

SSTP_USER = os.getenv('SSTP_USER', 'vpn')
SSTP_PASS = os.getenv('SSTP_PASSWORD', 'vpn')

CONNECT_TIMEOUT = int(os.getenv('SSTP_CONNECT_TIMEOUT', '30'))  # wait for ppp interface
TEST_TIMEOUT    = int(os.getenv('SSTP_TEST_TIMEOUT', '15'))     # per geo request
MIN_LIVE        = int(os.getenv('SSTP_MIN_LIVE', '5'))          # safety threshold

SSTPC = shutil.which('sstpc') or '/usr/sbin/sstpc'
IP    = shutil.which('ip') or '/usr/sbin/ip'

# sstpc options (the extra args after the server name are passed to pppd as is)
SSTP_OPTS = ['--log-stderr', '--log-level', '2', '--tls-ext', '--cert-warn']
PPPD_OPTS = ['noauth', 'noipdefault', 'nodefaultroute',
             'refuse-eap', 'refuse-chap', 'refuse-mschap',
             'require-mschap-v2',
             'noccp', 'nobsdcomp', 'nodeflate', 'novj', 'novjccomp']

# GeoIP services used through the tunnel. The first one that answers wins.
GEO_PROVIDERS = [
    ('ip-api.com', 'http://ip-api.com/json/?fields=status,country,countryCode,query'),
    ('ipwho.is',   'https://ipwho.is/'),
    ('ipapi.co',   'https://ipapi.co/json/'),
]

LINE_RE = re.compile(r'^([a-zA-Z0-9\.\-]+)(?::(\d+))?$')


# ------------------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------------------

def resolve_host(host):
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def sudo(cmd):
    """Run a command as root (runner allows password-less sudo)."""
    return subprocess.run(['sudo', '-n'] + cmd, check=False,
                          capture_output=True, text=True)


def log(msg):
    print(msg, flush=True)


def log_tail(path, limit=500):
    try:
        with open(path, 'rb') as f:
            data = f.read()
        text = data.decode(errors='replace')
        return text[-limit:]
    except OSError:
        return ''


def failure_reason(text):
    """Pull the most meaningful sstpc error line out of its log tail."""
    patterns = [
        r'Could not resolve host.*', r'Could not connect.*', r'Verification of server.*',
        r'HTTP handshake.*', r'Connection was aborted.*', r'Negotiation with server.*',
        r'Failed to receive .*', r'CHAP authentication failed.*', r'Could not start PPP.*',
        r'Could not initialize PPP.*', r'Connection established.*',
    ]
    for pat in patterns:
        m = re.findall(pat, text)
        if m:
            return m[0][:120]
    return 'unknown'


# ------------------------------------------------------------------------------
# PPP interface detection
# ------------------------------------------------------------------------------

def list_ppp_interfaces():
    try:
        r = subprocess.run([IP, '-o', 'link'], check=False,
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return set()
        return set(re.findall(r'\bppp\d+', r.stdout))
    except Exception:
        return set()


# ------------------------------------------------------------------------------
# GeoIP
# ------------------------------------------------------------------------------

def geo_via_tunnel(iface):
    """Query GeoIP providers through the ppp interface (host route)."""
    for geo_host, geo_url in GEO_PROVIDERS:
        geo_ip = resolve_host(geo_host)
        route_added = False
        try:
            if geo_ip:
                r = sudo([IP, 'route', 'replace', geo_ip + '/32', 'dev', iface])
                route_added = (r.returncode == 0)
            resp = requests.get(geo_url, timeout=TEST_TIMEOUT,
                                headers={'User-Agent': 'vpngate-collector'})
            data = resp.json()
            code = data.get('countryCode') or data.get('country_code')
            name = data.get('country') or data.get('country_name') or code
            egress = data.get('query') or data.get('ip')
            if code:
                return str(code).upper(), name, egress
        except Exception:
            continue
        finally:
            if route_added:
                sudo([IP, 'route', 'del', geo_ip + '/32'])
    return None, None, None


def geo_batch(ips):
    """GeoIP for a list of resolved IPs (used by the UNVERIFIED fallback mode)."""
    lookup = {}
    chunks = [ips[i:i + 100] for i in range(0, len(ips), 100)]
    for chunk in chunks:
        for attempt in range(2):
            try:
                r = requests.post(
                    'http://ip-api.com/batch'
                    '?fields=status,country,countryCode,query',
                    json=chunk, timeout=25)
                rows = r.json()
                for ip_value, row in zip(chunk, rows):
                    if row.get('status') == 'success':
                        lookup[ip_value] = (str(row['countryCode']).upper(),
                                            row.get('country') or row['countryCode'])
                break
            except Exception:
                time.sleep(2)
        time.sleep(1.5)  # stay under the free batch rate limit
    return lookup


# ------------------------------------------------------------------------------
# Single-host connection test
# ------------------------------------------------------------------------------

def kill_session(proc):
    if proc is not None and proc.poll() is None:
        sudo(['kill', '-TERM', str(proc.pid)])
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            sudo(['kill', '-9', str(proc.pid)])
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    # Ensure nothing lingers for the next round
    sudo(['pkill', '-TERM', 'pppd'])


def connect_test(host, port, line, log_path):
    """Bring up an SSTP tunnel, measure the egress country, tear it down."""
    result = {'host': host, 'port': port, 'line': line, 'live': False,
              'code': None, 'country': None, 'egress': None, 'reason': None}

    server_ip = resolve_host(host)
    if not server_ip:
        result['reason'] = 'DNS_FAIL'
        return result

    # sstpc natively parses host:port; default port is 443 when omitted
    target = f'{host}:{port}' if port else host

    before = list_ppp_interfaces()
    log_file = open(log_path, 'wb')
    cmd = ['sudo', '-n', SSTPC] + SSTP_OPTS + [
        '--user', SSTP_USER, '--password', SSTP_PASS, target] + PPPD_OPTS
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file,
                                stdin=subprocess.DEVNULL)
    except Exception as exc:
        result['reason'] = f'SPAWN_FAIL {exc}'[:120]
        log_file.close()
        return result

    ppp_iface = None
    started = time.time()
    while time.time() - started < CONNECT_TIMEOUT:
        if proc.poll() is not None:
            break  # sstpc exited -> tunnel never came up
        new_ifaces = list_ppp_interfaces() - before
        if new_ifaces:
            ppp_iface = sorted(new_ifaces)[-1]
            break
        time.sleep(0.5)

    if not ppp_iface:
        result['reason'] = failure_reason(log_tail(log_path)) or 'CONNECT_FAIL'
        kill_session(proc)
        log_file.close()
        return result

    # Tunnel is up (ppp interface exists). Send the country probe THROUGH it.
    code, country, egress = geo_via_tunnel(ppp_iface)
    if code:
        result.update(live=True, code=code, country=country, egress=egress,
                      reason='OK')
    else:
        result['reason'] = 'NO_EGRESS'  # tunnel up but no forwarded traffic

    kill_session(proc)
    log_file.close()

    # Let the kernel fully clean the interface before the next dial
    for _ in range(12):
        if not (list_ppp_interfaces() - before):
            break
        time.sleep(0.5)

    return result


# ------------------------------------------------------------------------------
# Output file building
# ------------------------------------------------------------------------------

def build_output_file(results, verified):
    """Write the grouped-by-country output file and return stats."""
    total = len(results)
    live = [r for r in results if r['code']]
    dead = total - len(live)

    groups = {}
    for row in live:
        code = row['code']
        groups.setdefault(code, {'name': row['country'] or code, 'hosts': []})
        groups[code]['hosts'].append(row['line'])

    # Countries with most servers first, then alphabetical
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]['hosts']), kv[0]))

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    if verified:
        method = 'Real SSTP connection test (sstp-client), country detected via tunnel egress IP'
    else:
        method = 'UNVERIFIED - GeoIP of DNS-resolved IP only (no connection test)'

    lines = [
        '# ' + '=' * 68,
        '# VPN Gate SSTP Hosts - grouped by country',
        '# Generated : ' + now,
        '# Tested    : ' + str(total) + '   Live: ' + str(len(live)) + '   Dead: ' + str(dead),
        '# Method    : ' + method,
        '# ' + '=' * 68,
        '',
    ]

    for code, group in ordered:
        lines.append(f'=== {group["name"]} ({code}) - {len(group["hosts"])} servers ===')
        for host_line in sorted(group['hosts']):
            lines.append(host_line)
        lines.append('')

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).rstrip('\n') + '\n')

    country_counts = {code: len(g['hosts']) for code, g in ordered}
    stats = {'total': total, 'live': len(live), 'dead': dead,
             'verified': verified, 'countries': country_counts}
    return stats


def summary_from_country_counts(counts):
    sorted_counts = sorted(counts.items(), key=lambda kv: -kv[1])
    parts = [f'{code}:{n}' for code, n in sorted_counts]
    return ', '.join(parts)


# ------------------------------------------------------------------------------
# UNVERIFIED (DNS GeoIP) mode
# ------------------------------------------------------------------------------

def dns_geo_rows(raw_lines):
    rows = []
    resolved = {}
    for line in raw_lines:
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        host, port = m.group(1), m.group(2)
        ip_value = resolve_host(host)
        if ip_value:
            resolved[host] = ip_value
        rows.append({'host': host, 'port': port, 'line': line, 'ip': ip_value})

    unique_ips = sorted(set(resolved.values()))
    lookup = geo_batch(unique_ips)

    for row in rows:
        geo = lookup.get(resolved.get(row['host']))
        if geo:
            row.update(code=geo[0], country=geo[1], live=False, reason='GEO_DNS')
        else:
            row.update(code=None, country=None, live=False, reason='DNS_FAIL' if not row['ip'] else 'GEO_FAIL')
    return rows


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

def load_raw_lines():
    if not os.path.exists(RAW_FILE):
        return None
    with open(RAW_FILE, 'r', encoding='utf-8', errors='replace') as f:
        return [ln.strip() for ln in f if ln.strip()]


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    raw_lines = load_raw_lines()
    if raw_lines is None:
        print(f'RAW_FILE not found: {RAW_FILE}')
        return 1

    force_dns = '--dns-only' in sys.argv or os.getenv('SSTP_SKIP_TEST') == '1'
    sstpc_available = os.path.exists(SSTPC)

    # Clean up anything left from a previous run
    if sstpc_available:
        sudo(['pkill', '-9', 'sstpc'])
        sudo(['pkill', '-9', 'pppd'])

    if not sstpc_available or force_dns or not (IS_GITHUB or sys.platform.startswith('linux')):
        if force_dns:
            log('Connect-test skipped by request; using DNS-GeoIP UNVERIFIED mode.')
        elif not sstpc_available:
            log(f'sstpc not found ({SSTPC}); using DNS-GeoIP UNVERIFIED mode.')
        else:
            log('Non-Linux host; using DNS-GeoIP UNVERIFIED mode.')
        rows = dns_geo_rows(raw_lines)
        stats = build_output_file(rows, verified=False)
        log(json.dumps({'VALIDATION_SUMMARY': stats}))
        return 0

    # ------------------------------------------------------------------ full test
    log(f'Starting sequential SSTP connect-test on {len(raw_lines)} hosts '
        f'(each host is dialed alone, country detected through the tunnel)...')

    results = []
    live_count = 0
    for index, line in enumerate(raw_lines, start=1):
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        host, port = m.group(1), m.group(2)
        log_path = os.path.join(LOG_DIR, f'{index:03d}_{host.replace(":", "_")}.log')

        row = connect_test(host, port, line, log_path)
        flag = 'LIVE' if row['live'] else 'DEAD'
        extra = ''
        if row['live']:
            live_count += 1
            extra = f'  egress={row["egress"]}'
        elif row.get('reason') and row['reason'] != 'OK':
            extra = f'  ({row["reason"]})'
        log(f'[{index}/{len(raw_lines)}] {host} -> {flag} {row["code"] or "-"}{extra}')
        results.append(row)
        time.sleep(0.3)

    log(f'Connect-test finished: live={live_count} / total={len(results)}')

    verified = live_count >= MIN_LIVE
    if verified:
        stats = build_output_file(results, verified=True)
    else:
        log(f'Less than {MIN_LIVE} live servers; regenerating the file as UNVERIFIED '
            'so the list is not lost (DNS-GeoIP mode).')
        rows = dns_geo_rows(raw_lines)
        stats = build_output_file(rows, verified=False)
        stats['connect_live'] = live_count

    log('Country breakdown: ' + (summary_from_country_counts(stats['countries']) or '-'))
    print(json.dumps({'VALIDATION_SUMMARY': stats}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
