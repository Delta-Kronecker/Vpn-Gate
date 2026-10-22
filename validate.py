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
import threading
import subprocess
import requests
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

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

CONNECT_TIMEOUT = int(os.getenv('SSTP_CONNECT_TIMEOUT', '35'))  # wait for ppp interface
TEST_TIMEOUT    = int(os.getenv('SSTP_TEST_TIMEOUT', '15'))     # per geo request
MIN_LIVE        = int(os.getenv('SSTP_MIN_LIVE', '5'))          # safety threshold
ATTEMPTS        = max(1, int(os.getenv('SSTP_ATTEMPTS', '2')))  # retry count for dead hosts
PARALLEL        = max(1, int(os.getenv('SSTP_PARALLEL', '4')))  # concurrent dial groups

SSTPC = shutil.which('sstpc') or '/usr/sbin/sstpc'
IP    = shutil.which('ip') or '/usr/sbin/ip'

# sstpc options (the extra args after the server name are passed to pppd as is)
SSTP_OPTS = ['--log-stderr', '--log-level', '2', '--tls-ext', '--cert-warn']
PPPD_OPTS = ['noauth', 'noipdefault', 'nodefaultroute',
             'refuse-eap', 'refuse-chap', 'refuse-mschap',
             'noccp', 'nobsdcomp', 'nodeflate', 'novj', 'novjccomp']
# NOTE: do NOT pass require-mschap-v2 - in pppd it means "the PEER must
# authenticate itself with MSCHAPv2", which fails without a chap-secrets
# entry. The sstp-pppd-plugin already handles our own MSCHAPv2 auth using
# the user/password given to sstpc.

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
        r'Could not initialize PPP.*', r'Can\'t open .*', r'unrecognized option.*',
        r'The remote system is required to authenticate.*',
        r'suitable secret \(password\).*', r'Authentication failed.*',
        r'Connection established.*', r'Connect timed out.*', r'Connection timed out.*',
        r'Could not complete connect.*',
    ]
    for pat in patterns:
        m = re.findall(pat, text)
        if m:
            return m[0][:120]
    return 'unknown'


def compressed_tail(text, limit=260):
    """One-line, printable version of a log tail for the Actions console."""
    text = re.sub(r'[\r\n\t ]+', ' ', text).strip()
    return text[-limit:]


# ------------------------------------------------------------------------------
# PPP interface detection
# ------------------------------------------------------------------------------

def list_ppp_interfaces():
    try:
        r = subprocess.run([IP, '-o', 'link'], check=False,
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return set()
        return set(re.findall(r'\b(?:ppp\d+|sstp[0-9A-Za-z]+)\b', r.stdout))
    except Exception:
        return set()


# ------------------------------------------------------------------------------
# GeoIP
# ------------------------------------------------------------------------------

def geo_via_tunnel(iface, local_ip=None):
    """Query GeoIP providers through the ppp interface (host route).
    The routing table is process-wide and shared by all parallel dials, so
    the probe is serialized with a lock (routing races would mix tunnels)."""
    for geo_host, geo_url in GEO_PROVIDERS:
        geo_ip = resolve_host(geo_host)
        route_added = False
        try:
            GEO_LOCK.acquire()
            if geo_ip:
                r = sudo([IP, 'route', 'replace', geo_ip + '/32', 'dev', iface])
                route_added = (r.returncode == 0)
            if not route_added:
                continue  # routing failed; never leak the request off-tunnel
            resp = requests.get(geo_url, timeout=TEST_TIMEOUT,
                                headers={'User-Agent': 'vpngate-collector'})
            data = resp.json()
            code = data.get('countryCode') or data.get('country_code')
            name = data.get('country') or data.get('country_name') or code
            egress = data.get('query') or data.get('ip')
            if code:
                if egress and local_ip and egress == local_ip:
                    continue  # did not traverse the tunnel - try next provider
                return str(code).upper(), name, egress
        except Exception:
            continue
        finally:
            if geo_ip:
                sudo([IP, 'route', 'del', geo_ip + '/32'])
            if GEO_LOCK.locked():
                GEO_LOCK.release()
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
    """Terminate this dial's process group (sstpc + its pppd child)."""
    if proc is not None and proc.poll() is None:
        # Negative pid = process group (Popen used start_new_session=True)
        sudo(['kill', '-TERM', '-' + str(proc.pid)])
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            sudo(['kill', '-9', '-' + str(proc.pid)])
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def ppp_iface_ready(iface):
    """True once the ppp interface negotiated an IPv4 address (link UP)."""
    try:
        r = subprocess.run([IP, '-o', '-4', 'addr', 'show', 'dev', iface],
                           check=False, capture_output=True, text=True,
                           timeout=10)
        if r.returncode != 0:
            return False
        return ('inet ' in r.stdout)
    except Exception:
        return False


LOCAL_PUBLIC_IP = None
GEO_LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
IFACE_LOCK = threading.Lock()
ALL_IFACE_SNAPSHOT = set()

def local_public_ip():
    """The runner's direct egress IP (cached) - used to detect leaked probes."""
    global LOCAL_PUBLIC_IP
    if LOCAL_PUBLIC_IP is None:
        try:
            LOCAL_PUBLIC_IP = requests.get(
                'http://ip-api.com/json/?fields=query', timeout=10).json().get('query')
        except Exception:
            LOCAL_PUBLIC_IP = ''
    return LOCAL_PUBLIC_IP or None


def connect_test(host, port, line, log_path):
    """Bring up an SSTP tunnel, measure the egress country, tear it down.

    Runs under a dedicated pppd interface name (ifname) so several dials can
    be tested in parallel without interfering with each other."""
    result = {'host': host, 'port': port, 'line': line, 'live': False,
              'code': None, 'country': None, 'egress': None, 'reason': None}

    server_ip = resolve_host(host)
    if not server_ip:
        result['reason'] = 'DNS_FAIL'
        return result

    # sstpc natively parses host:port; default port is 443 when omitted
    target = f'{host}:{port}' if port else host

    # Unique interface name for this dial (pppd option 'ifname')
    index_tag = log_path.split(os.sep)[-1].split('_')[0]
    iface_name = f'sstp{index_tag}'[:15]

    log_file = open(log_path, 'wb')
    cmd = ['sudo', '-n', SSTPC] + SSTP_OPTS + [
        '--user', SSTP_USER, '--password', SSTP_PASS, target] + PPPD_OPTS + \
        ['ifname', iface_name]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
    except Exception as exc:
        result['reason'] = f'SPAWN_FAIL {exc}'[:120]
        log_file.close()
        return result

    ppp_iface = None
    started = time.time()
    while time.time() - started < CONNECT_TIMEOUT:
        if proc.poll() is not None:
            break  # sstpc exited -> tunnel never came up
        # Preferred: our dedicated, uniquely named interface
        if iface_name in list_ppp_interfaces() and ppp_iface_ready(iface_name):
            ppp_iface = iface_name
            break
        # Fallback: claim any newly appeared pppX (in case ifname was ignored)
        with IFACE_LOCK:
            new_ifaces = list_ppp_interfaces() - ALL_IFACE_SNAPSHOT
            if new_ifaces:
                claimed = sorted(new_ifaces)[0]
                ALL_IFACE_SNAPSHOT.add(claimed)
                if ppp_iface_ready(claimed):
                    ppp_iface = claimed
                    break
        time.sleep(0.5)

    if not ppp_iface:
        still_running = (proc.poll() is None)
        tail = log_tail(log_path)
        kill_session(proc)
        log_file.close()
        reason = failure_reason(tail)
        if reason == 'unknown':
            if still_running:
                reason = 'TIMEOUT_NO_PPP (sstpc alive but no tunnel)'
            else:
                reason = f'EXIT_FAIL(code={proc.returncode or "?"}) (unknown log)'
            log(f'      sstpc tail: {compressed_tail(tail)}')
        result['reason'] = reason
        return result

    # Tunnel is up. Send the country probe THROUGH it (host route via ppp).
    code, country, egress = geo_via_tunnel(ppp_iface, local_ip=local_public_ip())
    if code:
        result.update(live=True, code=code, country=country, egress=egress,
                      reason='OK')
    else:
        result['reason'] = 'NO_EGRESS'  # tunnel up but no forwarded traffic
        log(f'      sstpc tail: {compressed_tail(log_tail(log_path))}')

    kill_session(proc)
    log_file.close()

    # Give the kernel a moment to tear the interface down
    for _ in range(12):
        if ppp_iface not in list_ppp_interfaces():
            break
        time.sleep(0.5)
    with IFACE_LOCK:
        ALL_IFACE_SNAPSHOT.discard(ppp_iface)

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


def dedupe_lines(lines):
    """Normalize + remove duplicate hosts (one entry per hostname, all sources
    merged). Entries with an explicit port are kept over port-less equals."""
    chosen = {}
    for line in lines:
        line = line.strip()
        m = LINE_RE.match(line)
        if not m:
            continue
        host = m.group(1).lower()
        port = m.group(2) or ''
        if host not in chosen or (port and not chosen[host][1]):
            chosen[host] = (m.group(1), port)
    out = []
    for _, (host, port) in sorted(chosen.items()):
        out.append(f'{host}:{port}' if port else host)
    return out


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    raw_lines = load_raw_lines()
    if raw_lines is None:
        print(f'RAW_FILE not found: {RAW_FILE}')
        return 1

    raw_lines = dedupe_lines(raw_lines)
    print(f'After dedupe: {len(raw_lines)} unique hosts to test.')

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
    ALL_IFACE_SNAPSHOT.clear()
    ALL_IFACE_SNAPSHOT.update(list_ppp_interfaces())
    log(f'Starting SSTP connect-test on {len(raw_lines)} hosts '
        f'({PARALLEL} dials in parallel, up to {ATTEMPTS} attempts per host, '
        'country detected through the tunnel)...')

    results_by_index = {}
    live_count = 0

    def progress(index, row, attempt):
        flag = 'LIVE' if row['live'] else 'DEAD'
        extra = ''
        if row['live']:
            extra = f'  egress={row["egress"]}'
        elif row.get('reason') and row['reason'] != 'OK':
            extra = f'  ({row["reason"]})'
        if attempt > 1:
            extra += f'  [try {attempt}]'
        with PRINT_LOCK:
            log(f'[{index}/{len(raw_lines)}] {row["host"]} -> {flag} '
                f'{row["code"] or "-"}{extra}')

    def test_one(index, line):
        """Dial one host (with retries); returns the best result row."""
        m = LINE_RE.match(line.strip())
        if not m:
            return None
        host, port = m.group(1), m.group(2)
        best = None
        for attempt in range(1, ATTEMPTS + 1):
            log_path = os.path.join(
                LOG_DIR, f'{index:03d}_{host.replace(":", "_")}_{attempt}.log')
            row = connect_test(host, port, line, log_path)
            progress(index, row, attempt)
            if row['live']:
                return row
            best = row  # remember last failure reason
            if attempt < ATTEMPTS:
                time.sleep(1.5 * attempt)  # small backoff before the retry
        return best

    # Group-testing: several dials run concurrently, each on its own ppp
    # interface; the GeoIP probe itself stays serialized (shared routing).
    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {}
        for index, line in enumerate(raw_lines, start=1):
            futures[pool.submit(test_one, index, line)] = index, line
        for fut in as_completed(futures):
            row = fut.result()
            if row is None:
                continue
            index, line = futures[fut]
            results_by_index[index] = row
            if row['live']:
                live_count += 1

    results = [results_by_index[i] for i in sorted(results_by_index)]

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
