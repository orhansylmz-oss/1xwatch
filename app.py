import os, time, requests, threading, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT",  "")
ODDS_KEY  = os.environ.get("ODDS_KEY", "")

state = {
    "monitoring":  False,
    "last_check":  None,
    "log":         [],
    "interval":    3600,  # 1 saat
    "min_value":   5.0,   # minimum value % eşiği
    "notified":    set(),
}

LEAGUES = {
    "Premier Lig":      "soccer_epl",
    "La Liga":          "soccer_spain_la_liga",
    "Bundesliga":       "soccer_germany_bundesliga",
    "Serie A":          "soccer_italy_serie_a",
    "Ligue 1":          "soccer_france_ligue_one",
    "Türkiye Süper Lig": "soccer_turkey_super_league",
}

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)

def get_odds(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_KEY,
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        log.warning(f"{sport_key} hata: {r.status_code}")
        return []
    return r.json()

def find_value_bets(games):
    value_bets = []
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        commence = game.get("commence_time", "")
        bookmakers = game.get("bookmakers", [])

        if not bookmakers:
            continue

        # Her sonuç için tüm bookmaker'lardan oran topla
        all_odds = {"home": [], "draw": [], "away": []}

        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for outcome in market.get("outcomes", []):
                    name = outcome["name"]
                    price = outcome["price"]
                    if name == home:
                        all_odds["home"].append(price)
                    elif name == away:
                        all_odds["away"].append(price)
                    elif name == "Draw":
                        all_odds["draw"].append(price)

        # En yüksek oran vs ortalama karşılaştır
        for result_key, label in [("home", home), ("draw", "Beraberlik"), ("away", away)]:
            odds_list = all_odds[result_key]
            if len(odds_list) < 3:
                continue

            avg = sum(odds_list) / len(odds_list)
            best = max(odds_list)
            value_pct = ((best / avg) - 1) * 100

            if value_pct >= state["min_value"]:
                # Kaç bookmaker bu oranı veriyor?
                best_bm = [bm["title"] for bm in bookmakers
                           for m in bm.get("markets", []) if m["key"] == "h2h"
                           for o in m.get("outcomes", [])
                           if o["name"] in [home, away, "Draw"] and
                           abs(o["price"] - best) < 0.01 and
                           o["name"] == (home if result_key == "home" else (away if result_key == "away" else "Draw"))]

                try:
                    dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                    kickoff = dt.strftime("%d.%m %H:%M")
                except:
                    kickoff = "?"

                value_bets.append({
                    "home": home,
                    "away": away,
                    "kickoff": kickoff,
                    "result": label,
                    "best_odds": round(best, 2),
                    "avg_odds": round(avg, 2),
                    "value_pct": round(value_pct, 1),
                    "bookmakers": best_bm[:3],
                    "bm_count": len(odds_list),
                })

    return sorted(value_bets, key=lambda x: x["value_pct"], reverse=True)

def check_all_leagues():
    state["last_check"] = datetime.now().strftime("%H:%M:%S")
    all_value_bets = []

    for league_name, sport_key in LEAGUES.items():
        try:
            games = get_odds(sport_key)
            vbs = find_value_bets(games)
            for vb in vbs:
                vb["league"] = league_name
            all_value_bets.extend(vbs)
            log.info(f"{league_name}: {len(vbs)} value bet")
            time.sleep(0.5)
        except Exception as e:
            log.error(f"{league_name} hata: {e}")

    all_value_bets.sort(key=lambda x: x["value_pct"], reverse=True)

    # Telegram'a gönder
    new_bets = []
    for vb in all_value_bets:
        key = f"{vb['home']}_{vb['away']}_{vb['result']}"
        if key not in state["notified"]:
            new_bets.append(vb)
            state["notified"].add(key)

    if new_bets:
        msg = "🎯 <b>VALUE BET BULUNDU!</b>\n\n"
        for vb in new_bets[:5]:
            msg += (
                f"⚽ <b>{vb['home']} vs {vb['away']}</b>\n"
                f"🏆 {vb['league']} · {vb['kickoff']}\n"
                f"✅ Sonuç: <b>{vb['result']}</b>\n"
                f"📈 En iyi oran: <b>{vb['best_odds']}</b> (ort: {vb['avg_odds']})\n"
                f"💰 Value: <b>%{vb['value_pct']}</b>\n"
                f"🏦 Bookmaker: {', '.join(vb['bookmakers'])}\n\n"
            )
        try:
            send_telegram(msg)
        except Exception as e:
            log.error(f"Telegram: {e}")

    # Log güncelle
    state["log"] = all_value_bets[:50]
    log.info(f"Toplam {len(all_value_bets)} value bet bulundu")
    return all_value_bets

def monitor_loop():
    while state["monitoring"]:
        try:
            check_all_leagues()
        except Exception as e:
            log.error(f"Monitor loop: {e}")
        time.sleep(state["interval"])

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scan", methods=["POST"])
def api_scan():
    threading.Thread(target=check_all_leagues, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/valuebets")
def api_valuebets():
    return jsonify(state["log"])

@app.route("/api/state")
def api_state():
    return jsonify({
        "monitoring":  state["monitoring"],
        "last_check":  state["last_check"],
        "count":       len(state["log"]),
        "interval":    state["interval"],
        "min_value":   state["min_value"],
    })

@app.route("/api/monitor", methods=["POST"])
def api_monitor():
    action = request.json.get("action")
    if action == "start" and not state["monitoring"]:
        state["monitoring"] = True
        threading.Thread(target=monitor_loop, daemon=True).start()
    elif action == "stop":
        state["monitoring"] = False
    return jsonify({"ok": True, "monitoring": state["monitoring"]})

@app.route("/api/settings", methods=["POST"])
def api_settings():
    d = request.json
    if "interval" in d:
        state["interval"] = int(d["interval"])
    if "min_value" in d:
        state["min_value"] = float(d["min_value"])
    return jsonify({"ok": True})

@app.route("/api/config")
def api_config():
    return jsonify({
        "has_token": bool(TG_TOKEN),
        "has_chat":  bool(TG_CHAT),
        "has_odds":  bool(ODDS_KEY),
    })

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    try:
        send_telegram("✅ <b>Value Bet Bulucu aktif!</b>\n\nValue betler bulununca buraya bildirim gelecek. 🎯")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
