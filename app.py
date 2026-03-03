import os, time, requests, threading, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT",  "")

state = {
    "watched":   {},
    "notified":  {},
    "monitoring": False,
    "last_check": None,
    "log":        [],
    "fixtures":   [],
    "interval":   60,
    "last_domain": None,
}

MARKET_MAP = {
    "fouls":    [1994, 1995, 1996],
    "offsides": [1979, 1980],
    "shots":    [1565, 1566, 1969],
    "corners":  [39, 40, 41],
    "cards":    [1170, 1171, 1172],
    "bookings": [2339, 2340],
}

LEAGUES = [
    ("Premier Lig", 1365),
    ("La Liga", 2417),
    ("Bundesliga", 1366),
    ("Serie A", 1843),
    ("Ligue 1", 2415),
]

# Bilinen tüm 1xlite domain'leri - uygulama hepsini dener
DOMAINS = [
    "1xlite-989182.top",
    "1xlite-949285.top",
    "1xlite-628181.top",
    "1xlite-506423.top",
    "1xlite-394299.top",
    "1xbet.com",
    "1xbet.co.ke",
    "1xbet.ng",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://1xbet.com/",
    "Origin": "https://1xbet.com",
}

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    data = r.json()
    if not data.get("ok"):
        raise Exception(data.get("description", "Telegram hatasi"))
    return data

def get_working_domain():
    """Calisan domain bul ve cache'le"""
    # Onceki calisan domain'i once dene
    if state["last_domain"]:
        try:
            url = f"https://{state['last_domain']}/LineFeed/GetSportsShortZip?sports=1&lng=EN&tf=10000&tz=0"
            r = requests.get(url, headers=HEADERS, timeout=5)
            if r.status_code == 200 and r.json():
                return state["last_domain"]
        except:
            pass

    for domain in DOMAINS:
        try:
            url = f"https://{domain}/LineFeed/GetSportsShortZip?sports=1&lng=EN&tf=10000&tz=0"
            r = requests.get(url, headers=HEADERS, timeout=6)
            if r.status_code == 200:
                data = r.json()
                if data and (data.get("Value") or data.get("OK")):
                    log.info(f"Calisan domain bulundu: {domain}")
                    state["last_domain"] = domain
                    return domain
        except Exception as e:
            log.debug(f"{domain} basarisiz: {e}")
    return None

def fetch_fixtures():
    domain = get_working_domain()
    if not domain:
        log.warning("Hicbir domain'e ulasilamadi")
        return []

    fixtures = []
    for league_name, league_id in LEAGUES:
        try:
            url = f"https://{domain}/LineFeed/GetChampEvents?id={league_id}&lng=EN&tf=604800&tz=0&mode=4"
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            events = data.get("Value", []) or []
            for ev in events[:8]:
                mid = str(ev.get("Id", ""))
                home = ev.get("O1", "")
                away = ev.get("O2", "")
                ts = ev.get("S", 0)
                kickoff = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "?"
                available = get_available_markets(mid, domain)
                fixtures.append({
                    "id": mid, "league": league_name,
                    "home": home, "away": away,
                    "kickoff": kickoff, "kickoff_ts": ts,
                    "available_markets": available,
                })
        except Exception as e:
            log.warning(f"{league_name} hatasi: {e}")

    fixtures.sort(key=lambda x: x["kickoff_ts"])
    log.info(f"Toplam {len(fixtures)} mac yuklendi")
    return fixtures

def get_available_markets(match_id, domain=None):
    if not match_id:
        return []
    if not domain:
        domain = get_working_domain()
    if not domain:
        return []
    try:
        url = f"https://{domain}/LineFeed/GetGameZip?id={match_id}&lng=EN&isSubGames=true&GroupEvents=true&countevents=250"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        game = data.get("Value", {}) or {}
        groups = game.get("GE", []) or []
        group_ids = {g.get("G") for g in groups}
        return [k for k, ids in MARKET_MAP.items() if any(gid in group_ids for gid in ids)]
    except Exception as e:
        log.debug(f"Market check hatasi ({match_id}): {e}")
        return []

def check_loop():
    while state["monitoring"]:
        try:
            _do_check()
        except Exception as e:
            log.error(f"Check loop hatasi: {e}")
        time.sleep(state["interval"])

def _do_check():
    state["last_check"] = datetime.now().strftime("%H:%M:%S")
    domain = get_working_domain()
    for match_id, markets in list(state["watched"].items()):
        available = get_available_markets(match_id, domain)
        match_info = next((f for f in state["fixtures"] if f["id"] == match_id), None)
        match_name = f"{match_info['home']} vs {match_info['away']}" if match_info else match_id
        league = match_info.get("league", "") if match_info else ""
        for mkey in markets:
            alert_key = f"{match_id}_{mkey}"
            if alert_key in state["notified"] or mkey not in available:
                continue
            labels = {
                "fouls":    ("Faul Sayisi", "??"),
                "offsides": ("Ofsayt", "??"),
                "shots":    ("Sut", "??"),
                "corners":  ("Korner", "??"),
                "cards":    ("Kart", "??"),
                "bookings": ("Ceza Puani", "??"),
            }
            label, icon = labels.get(mkey, (mkey, "?"))
            msg = (f"? <b>BAHIS SECENEGI ACILDI!</b>\n\n"
                   f"?? <b>{match_name}</b>\n?? {league}\n"
                   f"{icon} <b>{label}</b> secenegi artik mevcut!\n"
                   f"? {datetime.now().strftime('%H:%M:%S')}\n\n"
                   f"?? 1xbet'e gir ve bahsini yap!")
            try:
                send_telegram(TG_TOKEN, TG_CHAT, msg)
                log.info(f"Bildirim: {match_name} - {label}")
            except Exception as e:
                log.error(f"Telegram hatasi: {e}")
            state["notified"][alert_key] = True
            state["log"].insert(0, {"match": match_name, "market": label, "icon": icon, "time": datetime.now().strftime("%H:%M:%S")})
            state["log"] = state["log"][:100]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/fixtures")
def api_fixtures():
    if not state["fixtures"]:
        state["fixtures"] = fetch_fixtures()
    return jsonify(state["fixtures"])

@app.route("/api/fixtures/refresh", methods=["POST"])
def api_refresh_fixtures():
    state["fixtures"] = fetch_fixtures()
    return jsonify({"ok": True, "count": len(state["fixtures"]), "domain": state["last_domain"]})

@app.route("/api/state")
def api_state():
    return jsonify({
        "monitoring": state["monitoring"],
        "last_check": state["last_check"],
        "watched": {k: list(v) for k, v in state["watched"].items()},
        "log": state["log"][:20],
        "interval": state["interval"],
        "domain": state["last_domain"],
    })

@app.route("/api/watch", methods=["POST"])
def api_watch():
    d = request.json
    mid, mkey, active = str(d.get("match_id", "")), d.get("market_key", ""), d.get("active", True)
    if mid not in state["watched"]:
        state["watched"][mid] = set()
    if active:
        state["watched"][mid].add(mkey)
    else:
        state["watched"][mid].discard(mkey)
        if not state["watched"][mid]:
            del state["watched"][mid]
    return jsonify({"ok": True})

@app.route("/api/monitor", methods=["POST"])
def api_monitor():
    action = request.json.get("action")
    if action == "start" and not state["monitoring"]:
        state["monitoring"] = True
        threading.Thread(target=check_loop, daemon=True).start()
    elif action == "stop":
        state["monitoring"] = False
    return jsonify({"ok": True, "monitoring": state["monitoring"]})

@app.route("/api/interval", methods=["POST"])
def api_interval():
    state["interval"] = int(request.json.get("seconds", 60))
    return jsonify({"ok": True})

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    try:
        send_telegram(TG_TOKEN, TG_CHAT, "? <b>1XWATCH test basarili!</b>\n\nBildirimler bu hesaba gelecek. ?")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/config")
def api_config():
    return jsonify({
        "has_token": bool(TG_TOKEN),
        "has_chat": bool(TG_CHAT),
        "token_preview": TG_TOKEN[:8] + "..." if TG_TOKEN else "",
        "chat_id": TG_CHAT,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=lambda: state.update({"fixtures": fetch_fixtures()}), daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
