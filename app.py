import os, time, requests, threading, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT",  "")

state = {
    "matches":   [],   # manuel eklenen maclar: {id, home, away, league, kickoff}
    "watched":   {},   # { matchId: set([marketKey,...]) }
    "notified":  {},
    "monitoring": False,
    "last_check": None,
    "log":        [],
    "interval":   60,
}

MARKET_MAP = {
    "fouls":    [1994, 1995, 1996],
    "offsides": [1979, 1980],
    "shots":    [1565, 1566, 1969],
    "corners":  [39, 40, 41],
    "cards":    [1170, 1171, 1172],
    "bookings": [2339, 2340],
}

DOMAINS = ["1xbet.com","1xbet.co.ke","1xbet.ng","1x001.com","1xbet.cm"]

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    data = r.json()
    if not data.get("ok"):
        raise Exception(data.get("description", "Telegram hatasi"))
    return data

def get_markets_for_match(match_id):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for domain in DOMAINS:
        try:
            url = f"https://{domain}/LineFeed/GetGameZip?id={match_id}&lng=EN&isSubGames=true&GroupEvents=true&countevents=250"
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            game = data.get("Value", {})
            if not game:
                continue
            groups = game.get("GE", []) or []
            group_ids = {g.get("G") for g in groups}
            available = []
            for mkey, ids in MARKET_MAP.items():
                if any(gid in group_ids for gid in ids):
                    available.append(mkey)
            log.info(f"Match {match_id} via {domain}: {available}")
            return available
        except Exception as e:
            log.debug(f"{domain} hatasi: {e}")
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
    for match_id, markets in list(state["watched"].items()):
        available = get_markets_for_match(match_id)
        match_info = next((m for m in state["matches"] if m["id"] == match_id), None)
        match_name = f"{match_info['home']} vs {match_info['away']}" if match_info else match_id
        league = match_info.get("league","") if match_info else ""
        for mkey in markets:
            alert_key = f"{match_id}_{mkey}"
            if alert_key in state["notified"]:
                continue
            if mkey in available:
                labels = {
                    "fouls":("Faul Sayisi","🟨"),
                    "offsides":("Ofsayt","🚩"),
                    "shots":("Sut","🎯"),
                    "corners":("Korner","📐"),
                    "cards":("Kart","🃏"),
                    "bookings":("Ceza Puani","📋"),
                }
                label, icon = labels.get(mkey,(mkey,"⚽"))
                msg = (f"⚽ <b>BAHIS SECENEGI ACILDI!</b>\n\n"
                       f"🏟 <b>{match_name}</b>\n🏆 {league}\n"
                       f"{icon} <b>{label}</b> secenegi artik mevcut!\n"
                       f"⏰ {datetime.now().strftime('%H:%M:%S')}\n\n"
                       f"🔗 1xbet'e gir ve bahsini yap!")
                try:
                    send_telegram(TG_TOKEN, TG_CHAT, msg)
                except Exception as e:
                    log.error(f"Telegram hatasi: {e}")
                state["notified"][alert_key] = True
                state["log"].insert(0,{"match":match_name,"market":label,"icon":icon,"time":datetime.now().strftime("%H:%M:%S")})
                state["log"] = state["log"][:100]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/matches")
def api_matches():
    return jsonify(state["matches"])

@app.route("/api/matches/add", methods=["POST"])
def api_add_match():
    d = request.json
    mid = str(d.get("id","")).strip()
    if not mid:
        return jsonify({"ok":False,"error":"ID gerekli"}), 400
    if any(m["id"]==mid for m in state["matches"]):
        return jsonify({"ok":False,"error":"Zaten eklendi"}), 400
    # Match bilgisini 1xbet'ten al
    headers = {"User-Agent":"Mozilla/5.0"}
    match_info = {"id":mid,"home":d.get("home","?"),"away":d.get("away","?"),"league":d.get("league",""),"kickoff":d.get("kickoff","")}
    for domain in DOMAINS:
        try:
            url = f"https://{domain}/LineFeed/GetGameZip?id={mid}&lng=EN&isSubGames=false&GroupEvents=false&countevents=5"
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code==200:
                v = r.json().get("Value",{})
                if v:
                    match_info["home"] = v.get("O1", match_info["home"])
                    match_info["away"] = v.get("O2", match_info["away"])
                    ts = v.get("S",0)
                    if ts:
                        match_info["kickoff"] = datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")
                    break
        except:
            continue
    state["matches"].append(match_info)
    return jsonify({"ok":True,"match":match_info})

@app.route("/api/matches/remove", methods=["POST"])
def api_remove_match():
    mid = str(request.json.get("id",""))
    state["matches"] = [m for m in state["matches"] if m["id"]!=mid]
    state["watched"].pop(mid, None)
    return jsonify({"ok":True})

@app.route("/api/state")
def api_state():
    return jsonify({
        "monitoring":state["monitoring"],
        "last_check":state["last_check"],
        "watched":{k:list(v) for k,v in state["watched"].items()},
        "log":state["log"][:20],
        "interval":state["interval"],
    })

@app.route("/api/watch", methods=["POST"])
def api_watch():
    d = request.json
    mid, mkey, active = str(d.get("match_id","")), d.get("market_key",""), d.get("active",True)
    if mid not in state["watched"]:
        state["watched"][mid] = set()
    if active:
        state["watched"][mid].add(mkey)
    else:
        state["watched"][mid].discard(mkey)
        if not state["watched"][mid]:
            del state["watched"][mid]
    return jsonify({"ok":True})

@app.route("/api/monitor", methods=["POST"])
def api_monitor():
    action = request.json.get("action")
    if action=="start" and not state["monitoring"]:
        state["monitoring"] = True
        threading.Thread(target=check_loop, daemon=True).start()
    elif action=="stop":
        state["monitoring"] = False
    return jsonify({"ok":True,"monitoring":state["monitoring"]})

@app.route("/api/interval", methods=["POST"])
def api_interval():
    state["interval"] = int(request.json.get("seconds",60))
    return jsonify({"ok":True})

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    try:
        send_telegram(TG_TOKEN, TG_CHAT, "✅ <b>1XWATCH test basarili!</b>\n\nBildirimler bu hesaba gelecek. ⚽")
        return jsonify({"ok":True})
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)}), 400

@app.route("/api/config")
def api_config():
    return jsonify({
        "has_token":bool(TG_TOKEN),
        "has_chat":bool(TG_CHAT),
        "token_preview":TG_TOKEN[:8]+"..." if TG_TOKEN else "",
        "chat_id":TG_CHAT,
    })

if __name__=="__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0", port=port, debug=False)
