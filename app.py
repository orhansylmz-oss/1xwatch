import os, json, time, requests, threading, logging
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
}

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    data = r.json()
    if not data.get("ok"):
        raise Exception(data.get("description", "Telegram hatası"))
    return data

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

def fetch_fixtures():
    fixtures = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9",
        "Referer": "https://1xbet.com/",
    }
    for league_name, league_id in LEAGUES:
        try:
            url = f"https://1xbet.com/LineFeed/GetChampEvents?id={league_id}&lng=TR&tf=604800&tz=3&mode=4&country=tr"
            r = requests.get(url, headers=headers, timeout=8)
            data = r.json()
            events = data.get("Value", [])
            for ev in events[:10]:
                match_id = str(ev.get("Id", ""))
                home = ev.get("O1", "")
                away = ev.get("O2", "")
                kickoff_ts = ev.get("S", 0)
                kickoff = datetime.fromtimestamp(kickoff_ts).strftime("%d.%m %H:%M") if kickoff_ts else "?"
                available = _get_available_markets(match_id, headers)
                fixtures.append({
                    "id": match_id,
                    "league": league_name,
                    "home": home,
                    "away": away,
                    "kickoff": kickoff,
                    "kickoff_ts": kickoff_ts,
                    "available_markets": available,
                })
        except Exception as e:
            log.warning(f"Fixture fetch hatasi ({league_name}): {e}")
    fixtures.sort(key=lambda x: x["kickoff_ts"])
    return fixtures

def _get_available_markets(match_id, headers):
    available = []
    if not match_id:
        return available
    try:
        url = f"https://1xbet.com/LineFeed/GetGameZip?id={match_id}&lng=TR&isSubGames=true&GroupEvents=true&countevents=250&country=tr"
        r = requests.get(url, headers=headers, timeout=8)
        data = r.json()
        game = data.get("Value", {})
        groups = game.get("GE", []) or []
        group_ids = {g.get("G") for g in groups}
        for market_key, ids in MARKET_MAP.items():
            if any(gid in group_ids for gid in ids):
                available.append(market_key)
    except Exception as e:
        log.debug(f"Market check hatasi ({match_id}): {e}")
    return available

def check_loop():
    while state["monitoring"]:
        try:
            _do_check()
        except Exception as e:
            log.error(f"Check loop hatasi: {e}")
        time.sleep(state["interval"])

def _do_check():
    state["last_check"] = datetime.now().strftime("%H:%M:%S")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://1xbet.com/",
    }
    for match_id, markets in list(state["watched"].items()):
        available = _get_available_markets(match_id, headers)
        match_info = next((f for f in state["fixtures"] if f["id"] == match_id), None)
        match_name = f"{match_info['home']} vs {match_info['away']}" if match_info else match_id
        league = match_info["league"] if match_info else ""
        for market_key in markets:
            alert_key = f"{match_id}_{market_key}"
            if alert_key in state["notified"]:
                continue
            if market_key in available:
                market_labels = {
                    "fouls":    ("Faul Sayisi", "🟨"),
                    "offsides": ("Ofsayt", "🚩"),
                    "shots":    ("Sut / Isabetli Sut", "🎯"),
                    "corners":  ("Korner", "📐"),
                    "cards":    ("Kart", "🃏"),
                    "bookings": ("Ceza Puani", "📋"),
                }
                label, icon = market_labels.get(market_key, (market_key, "⚽"))
                msg = (
                    f"⚽ <b>BAHIS SECENEGI ACILDI!</b>\n\n"
                    f"🏟 <b>{match_name}</b>\n"
                    f"🏆 {league}\n"
                    f"{icon} <b>{label}</b> secenegi artik mevcut!\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"🔗 1xbet'e gir ve bahsini yap!"
                )
                try:
                    send_telegram(TG_TOKEN, TG_CHAT, msg)
                    log.info(f"Bildirim gönderildi: {match_name} - {label}")
                except Exception as e:
                    log.error(f"Telegram hatasi: {e}")
                state["notified"][alert_key] = True
                state["log"].insert(0, {
                    "match": match_name,
                    "market": label,
                    "icon": icon,
                    "time": datetime.now().strftime("%H:%M:%S"),
                })
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
    return jsonify({"ok": True, "count": len(state["fixtures"])})

@app.route("/api/state")
def api_state():
    return jsonify({
        "monitoring": state["monitoring"],
        "last_check": state["last_check"],
        "watched": {k: list(v) for k, v in state["watched"].items()},
        "log": state["log"][:20],
        "interval": state["interval"],
    })

@app.route("/api/watch", methods=["POST"])
def api_watch():
    data = request.json
    match_id   = data.get("match_id")
    market_key = data.get("market_key")
    active     = data.get("active", True)
    if match_id not in state["watched"]:
        state["watched"][match_id] = set()
    if active:
        state["watched"][match_id].add(market_key)
    else:
        state["watched"][match_id].discard(market_key)
        if not state["watched"][match_id]:
            del state["watched"][match_id]
    return jsonify({"ok": True})

@app.route("/api/monitor", methods=["POST"])
def api_monitor():
    data = request.json
    action = data.get("action")
    if action == "start" and not state["monitoring"]:
        state["monitoring"] = True
        t = threading.Thread(target=check_loop, daemon=True)
        t.start()
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
        send_telegram(TG_TOKEN, TG_CHAT,
            "✅ <b>1XWATCH baglanti testi basarili!</b>\n\nBahis secenekleri acildiginda bu hesaba bildirim gelecek. ⚽"
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/config")
def api_config():
    return jsonify({
        "has_token": bool(TG_TOKEN),
        "has_chat":  bool(TG_CHAT),
        "token_preview": TG_TOKEN[:8] + "..." if TG_TOKEN else "",
        "chat_id": TG_CHAT,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
