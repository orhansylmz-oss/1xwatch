import os, time, requests, threading, logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT  = os.environ.get("TG_CHAT",  "")
ODDS_KEY = os.environ.get("ODDS_KEY", "")

state = {
    "monitoring":       False,
    "last_check":       None,
    "value_bets":       [],
    "sure_bets":        [],
    "interval":         3600,
    "min_value":        5.0,
    "min_profit":       1.0,
    "notified_value":   set(),
    "notified_sure":    set(),
    "selected_books":   {"Bet365", "Pinnacle", "Betfair", "1xBet", "Bwin", "Unibet"},
}

LEAGUES = {
    "Premier Lig":       "soccer_epl",
    "La Liga":           "soccer_spain_la_liga",
    "Bundesliga":        "soccer_germany_bundesliga",
    "Serie A":           "soccer_italy_serie_a",
    "Ligue 1":           "soccer_france_ligue_one",
    "Türkiye Süper Lig": "soccer_turkey_super_league",
}

ALL_BOOKMAKERS = [
    "Bet365", "Pinnacle", "Betfair", "1xBet", "Bwin",
    "Unibet", "William Hill", "Betway", "Marathon Bet",
    "Matchbook", "Coolbet", "NordicBet", "Unibet (NL)",
]

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")

def get_odds(sport_key):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_KEY,
        "regions": "eu,uk",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    r = requests.get(url, params=params, timeout=12)
    if r.status_code != 200:
        log.warning(f"{sport_key}: {r.status_code} {r.text[:100]}")
        return []
    return r.json()

def filter_bookmakers(bookmakers):
    """Sadece seçili bookmaker'ları döndür"""
    selected = state["selected_books"]
    if not selected:
        return bookmakers
    return [b for b in bookmakers if b["title"] in selected]

def find_value_bets(games, league_name):
    results = []
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        commence = game.get("commence_time", "")
        bookmakers = filter_bookmakers(game.get("bookmakers", []))
        if len(bookmakers) < 2:
            continue

        all_odds = {"home": [], "draw": [], "away": []}
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    if o["name"] == home:
                        all_odds["home"].append((o["price"], bm["title"]))
                    elif o["name"] == away:
                        all_odds["away"].append((o["price"], bm["title"]))
                    else:
                        all_odds["draw"].append((o["price"], bm["title"]))

        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
            kickoff = dt.strftime("%d.%m %H:%M")
        except:
            kickoff = "?"

        for result_key, label in [("home", home), ("draw", "Beraberlik"), ("away", away)]:
            odds_list = all_odds[result_key]
            if len(odds_list) < 2:
                continue
            prices = [p for p, _ in odds_list]
            avg = sum(prices) / len(prices)
            best_price, best_bm = max(odds_list, key=lambda x: x[0])
            value_pct = ((best_price / avg) - 1) * 100

            if value_pct >= state["min_value"]:
                results.append({
                    "type": "value",
                    "home": home, "away": away,
                    "league": league_name, "kickoff": kickoff,
                    "result": label,
                    "best_odds": round(best_price, 2),
                    "best_bm": best_bm,
                    "avg_odds": round(avg, 2),
                    "value_pct": round(value_pct, 1),
                    "bm_count": len(odds_list),
                    "all_odds": [{"bm": bm, "odds": round(p, 2)} for p, bm in sorted(odds_list, key=lambda x: -x[0])[:5]],
                })
    return results

def find_sure_bets(games, league_name):
    results = []
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        commence = game.get("commence_time", "")
        bookmakers = filter_bookmakers(game.get("bookmakers", []))
        if len(bookmakers) < 2:
            continue

        # Her sonuç için en iyi oranı bul
        best = {
            "home":  {"price": 0, "bm": ""},
            "draw":  {"price": 0, "bm": ""},
            "away":  {"price": 0, "bm": ""},
        }
        for bm in bookmakers:
            for market in bm.get("markets", []):
                if market["key"] != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    if o["name"] == home and o["price"] > best["home"]["price"]:
                        best["home"] = {"price": o["price"], "bm": bm["title"]}
                    elif o["name"] == away and o["price"] > best["away"]["price"]:
                        best["away"] = {"price": o["price"], "bm": bm["title"]}
                    elif o["name"] not in [home, away] and o["price"] > best["draw"]["price"]:
                        best["draw"] = {"price": o["price"], "bm": bm["title"]}

        h = best["home"]["price"]
        d = best["draw"]["price"]
        a = best["away"]["price"]

        if h <= 0 or d <= 0 or a <= 0:
            continue

        # Arbitraj yüzdesi hesapla
        arb = (1/h + 1/d + 1/a)
        profit_pct = round((1 - arb) * 100, 2)

        if profit_pct >= state["min_profit"]:
            try:
                dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                kickoff = dt.strftime("%d.%m %H:%M")
            except:
                kickoff = "?"

            # 100 birim üzerinden stake hesapla
            stake = 100
            s1 = round(stake / (h * arb), 2)
            s2 = round(stake / (d * arb), 2)
            s3 = round(stake / (a * arb), 2)

            results.append({
                "type": "sure",
                "home": home, "away": away,
                "league": league_name, "kickoff": kickoff,
                "profit_pct": profit_pct,
                "arb": round(arb, 4),
                "home_odds": h, "home_bm": best["home"]["bm"], "home_stake": s1,
                "draw_odds": d, "draw_bm": best["draw"]["bm"], "draw_stake": s2,
                "away_odds": a, "away_bm": best["away"]["bm"], "away_stake": s3,
            })

    return sorted(results, key=lambda x: -x["profit_pct"])

def scan_all():
    state["last_check"] = datetime.now().strftime("%H:%M:%S")
    all_value = []
    all_sure  = []

    for league_name, sport_key in LEAGUES.items():
        try:
            games = get_odds(sport_key)
            vbs = find_value_bets(games, league_name)
            sbs = find_sure_bets(games, league_name)
            all_value.extend(vbs)
            all_sure.extend(sbs)
            log.info(f"{league_name}: {len(vbs)} value, {len(sbs)} sure")
            time.sleep(0.3)
        except Exception as e:
            log.error(f"{league_name}: {e}")

    all_value.sort(key=lambda x: -x["value_pct"])
    all_sure.sort(key=lambda x: -x["profit_pct"])

    # Value bet bildirimleri
    new_value = []
    for vb in all_value:
        key = f"{vb['home']}_{vb['away']}_{vb['result']}"
        if key not in state["notified_value"]:
            new_value.append(vb)
            state["notified_value"].add(key)

    if new_value:
        msg = "🎯 <b>VALUE BET BULUNDU!</b>\n\n"
        for vb in new_value[:5]:
            msg += (
                f"⚽ <b>{vb['home']} vs {vb['away']}</b>\n"
                f"🏆 {vb['league']} · {vb['kickoff']}\n"
                f"✅ <b>{vb['result']}</b>\n"
                f"📈 En iyi oran: <b>{vb['best_odds']}</b> @ {vb['best_bm']}\n"
                f"📊 Ortalama: {vb['avg_odds']} | Bookmaker: {vb['bm_count']}\n"
                f"💰 Value: <b>%{vb['value_pct']}</b>\n\n"
            )
        send_telegram(msg)

    # Sure bet bildirimleri
    new_sure = []
    for sb in all_sure:
        key = f"{sb['home']}_{sb['away']}_sure"
        if key not in state["notified_sure"]:
            new_sure.append(sb)
            state["notified_sure"].add(key)

    if new_sure:
        for sb in new_sure[:3]:
            msg = (
                f"🔒 <b>SURE BET! Garantili Kâr!</b>\n\n"
                f"⚽ <b>{sb['home']} vs {sb['away']}</b>\n"
                f"🏆 {sb['league']} · {sb['kickoff']}\n\n"
                f"💰 Kâr: <b>%{sb['profit_pct']}</b>\n\n"
                f"1️⃣ {sb['home']}: <b>{sb['home_odds']}</b> @ {sb['home_bm']} → {sb['home_stake']} birim\n"
                f"🤝 Beraberlik: <b>{sb['draw_odds']}</b> @ {sb['draw_bm']} → {sb['draw_stake']} birim\n"
                f"2️⃣ {sb['away']}: <b>{sb['away_odds']}</b> @ {sb['away_bm']} → {sb['away_stake']} birim\n"
            )
            send_telegram(msg)

    state["value_bets"] = all_value[:100]
    state["sure_bets"]  = all_sure[:50]
    return all_value, all_sure

def monitor_loop():
    while state["monitoring"]:
        try:
            scan_all()
        except Exception as e:
            log.error(f"Monitor: {e}")
        time.sleep(state["interval"])

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/scan", methods=["POST"])
def api_scan():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/valuebets")
def api_valuebets():
    return jsonify(state["value_bets"])

@app.route("/api/surebets")
def api_surebets():
    return jsonify(state["sure_bets"])

@app.route("/api/state")
def api_state():
    return jsonify({
        "monitoring":     state["monitoring"],
        "last_check":     state["last_check"],
        "value_count":    len(state["value_bets"]),
        "sure_count":     len(state["sure_bets"]),
        "interval":       state["interval"],
        "min_value":      state["min_value"],
        "min_profit":     state["min_profit"],
        "selected_books": list(state["selected_books"]),
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
    if "interval"       in d: state["interval"]       = int(d["interval"])
    if "min_value"      in d: state["min_value"]       = float(d["min_value"])
    if "min_profit"     in d: state["min_profit"]      = float(d["min_profit"])
    if "selected_books" in d: state["selected_books"]  = set(d["selected_books"])
    return jsonify({"ok": True})

@app.route("/api/bookmakers")
def api_bookmakers():
    return jsonify({
        "all":      ALL_BOOKMAKERS,
        "selected": list(state["selected_books"]),
    })

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
        send_telegram("✅ <b>ValueBet Bulucu aktif!</b>\n\nSure bet ve value betler bulununca buraya bildirim gelecek. 🎯🔒")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
