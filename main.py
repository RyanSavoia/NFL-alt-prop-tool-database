import requests
import pandas as pd
import nfl_data_py as nfl
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import threading
import os
import logging
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# API Key - use environment variable in production
API_KEY = os.getenv("ODDS_API_KEY", "d8ba5d45eca27e710d7ef2680d8cb452")

# Global variable to store the latest props data
latest_props_data = {
    "last_updated": None,
    "props": [],
    "summary": {},
    "error": None
}
data_lock = threading.Lock()

# --- Timezone helpers
ET = timezone(timedelta(hours=-5))  # Eastern Time

def get_upcoming_games_filter():
    """
    Determines which games to show based on current day of week.
    """
    now_et = datetime.now(ET)
    weekday = now_et.weekday()  # 0=Monday, 6=Sunday
    
    def should_include_game(game_time_str):
        dt = datetime.fromisoformat(game_time_str.replace("Z", "+00:00"))
        dt_et = dt.astimezone(ET)
        game_date = dt_et.date()
        today = now_et.date()
        
        # Calculate days until game
        days_until = (game_date - today).days
        
        # Monday (0)
        if weekday == 0:
            return days_until == 0
        # Tuesday or Wednesday (1, 2)
        elif weekday in [1, 2]:
            game_weekday = dt_et.weekday()
            return game_weekday == 3 and 0 < days_until <= 7
        # Thursday (3)
        elif weekday == 3:
            return days_until == 0
        # Friday or Saturday (4, 5)
        elif weekday in [4, 5]:
            return 0 <= days_until <= 3
        # Sunday (6)
        else:
            return 0 <= days_until <= 1
    
    return should_include_game

def format_game_time(game_time_str):
    """Format game time for display"""
    dt = datetime.fromisoformat(game_time_str.replace("Z", "+00:00"))
    dt_et = dt.astimezone(ET)
    return dt_et.strftime("%a %m/%d %I:%M%p ET")

def fetch_nfl_props():
    """Main function to fetch and process NFL props"""
    global latest_props_data
    
    try:
        logger.info("Starting props update...")
        
        # 1. Get NFL events
        events_url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?apiKey={API_KEY}"
        resp = requests.get(events_url, timeout=10)
        resp.raise_for_status()
        events = resp.json()
        
        # Filter to relevant games based on day of week
        game_filter = get_upcoming_games_filter()
        events_to_check = [ev for ev in events if game_filter(ev["commence_time"])]
        
        if not events_to_check:
            with data_lock:
                latest_props_data = {
                    "last_updated": datetime.now(ET).isoformat(),
                    "props": [],
                    "summary": {"total_games": 0, "total_props": 0},
                    "error": "No relevant NFL games found"
                }
            return
        
        games_info = []
        for ev in events_to_check:
            games_info.append({
                "matchup": f"{ev['away_team']} @ {ev['home_team']}",
                "time": format_game_time(ev["commence_time"])
            })
        
        # 2. Markets to check
        markets = ",".join([
            "player_pass_tds_alternate",
            "player_pass_yds_alternate",
            "player_rush_yds_alternate",
            "player_receptions_alternate",
            "player_reception_yds_alternate",
            "player_rush_attempts_alternate"
        ])
        
        # 3. Collect props
        props = []
        for ev in events_to_check:
            event_id = ev["id"]
            home, away = ev["home_team"], ev["away_team"]
            game_time = format_game_time(ev["commence_time"])
            
            odds_url = (
                f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
                f"?regions=us&oddsFormat=american&markets={markets}&apiKey={API_KEY}"
            )
            odds_resp = requests.get(odds_url, timeout=10)
            odds_resp.raise_for_status()
            game_data = odds_resp.json()
            
            for bookmaker in game_data.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        player = outcome.get("description")
                        side = outcome.get("name")
                        line = outcome.get("point")
                        odds = outcome.get("price")
                        
                        if odds is not None and -600 <= odds <= -150:
                            props.append({
                                "game": f"{away} @ {home}",
                                "game_time": game_time,
                                "market": market["key"],
                                "player": player,
                                "side": side,
                                "line": line,
                                "odds": odds
                            })
        
        logger.info(f"Pulled {len(props)} props in odds range")
        
        # 4. Build weekly stats from play-by-play
        logger.info("Loading NFL play-by-play data...")
        pbp = nfl.import_pbp_data([2025])
        weekly_stats = (
            pd.concat([
                pbp.groupby(["season","week","passer_player_name"])
                   .agg(passing_yards=("passing_yards","sum"), passing_tds=("pass_touchdown","sum"))
                   .reset_index().rename(columns={"passer_player_name":"player"}),
                pbp.groupby(["season","week","rusher_player_name"])
                   .agg(rushing_yards=("rushing_yards","sum"), rush_attempts=("rush_attempt","sum"))
                   .reset_index().rename(columns={"rusher_player_name":"player"}),
                pbp.groupby(["season","week","receiver_player_name"])
                   .agg(receiving_yards=("receiving_yards","sum"), receptions=("complete_pass","sum"))
                   .reset_index().rename(columns={"receiver_player_name":"player"})
            ], ignore_index=True)
            .groupby(["season","week","player"]).sum().reset_index()
        )
        current_week = weekly_stats["week"].max()
        
        # 5. Market → stat mapping
        market_to_stat = {
            "player_pass_yds_alternate": "passing_yards",
            "player_pass_tds_alternate": "passing_tds",
            "player_rush_yds_alternate": "rushing_yards",
            "player_rush_attempts_alternate": "rush_attempts",
            "player_receptions_alternate": "receptions",
            "player_reception_yds_alternate": "receiving_yards"
        }
        
        # 6. Qualification check
        def qualifies_strong(player_full_name, stat_col, line, side, market):
            last_name = player_full_name.split()[-1]
            player_games = weekly_stats[
                (weekly_stats["season"] == 2025) &
                (weekly_stats["player"].str.contains(last_name, case=False, na=False))
            ]
            if player_games.empty or len(player_games) < current_week - 1:
                return False, []
            
            vals = list(player_games[stat_col].values)
            
            for val in vals:
                if side == "Over":
                    if "yds" in market:
                        if not (val > line * 1.2):
                            return False, vals
                    elif "attempts" in market or "receptions" in market:
                        if not (val > line * 1.3):
                            return False, vals
                    else:
                        if not (val > line):
                            return False, vals
                else:
                    return False, vals
            
            return True, vals
        
        # 7. Filter qualifying props
        qualifying = []
        for p in props:
            stat_col = market_to_stat.get(p["market"])
            if not stat_col:
                continue
            ok, vals = qualifies_strong(p["player"], stat_col, p["line"], p["side"], p["market"])
            if ok:
                avg_val = sum(vals) / len(vals) if vals else 0
                qualifying.append({
                    "game": p["game"],
                    "game_time": p["game_time"],
                    "market": p["market"].replace('_', ' ').title(),
                    "player": p["player"],
                    "side": p["side"],
                    "line": p["line"],
                    "odds": p["odds"],
                    "season_avg": round(avg_val, 1),
                    "weekly_values": vals
                })
        
        # 8. Deduplicate
        df = pd.DataFrame(qualifying)
        if not df.empty:
            df = df.drop_duplicates(subset=["player","market","line","side"])
            qualifying = df.to_dict('records')
        
        # Update global data
        with data_lock:
            latest_props_data = {
                "last_updated": datetime.now(ET).isoformat(),
                "current_day": datetime.now(ET).strftime('%A, %B %d'),
                "current_week": int(current_week),
                "games": games_info,
                "props": qualifying,
                "summary": {
                    "total_games": len(events_to_check),
                    "total_props": len(qualifying),
                    "odds_range": "-600 to -150",
                    "cushion": "20% for yards, 30% for attempts/receptions"
                },
                "error": None
            }
        
        logger.info(f"Update complete! Found {len(qualifying)} qualifying props")
        
    except Exception as e:
        logger.error(f"Error updating props: {str(e)}")
        with data_lock:
            latest_props_data["error"] = str(e)
            latest_props_data["last_updated"] = datetime.now(ET).isoformat()

# HTML template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NFL Alt Props Tracker</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        .header {
            background: white;
            border-radius: 12px;
            padding: 30px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }
        h1 {
            color: #2d3748;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .status {
            display: flex;
            gap: 20px;
            margin-top: 15px;
            flex-wrap: wrap;
        }
        .status-item {
            display: flex;
            align-items: center;
            gap: 8px;
            color: #718096;
            font-size: 14px;
        }
        .badge {
            background: #805ad5;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-weight: 600;
        }
        .games-list {
            background: rgba(255,255,255,0.95);
            border-radius: 8px;
            padding: 15px;
            margin: 20px 0;
        }
        .game-item {
            padding: 8px 0;
            border-bottom: 1px solid #e2e8f0;
            color: #4a5568;
        }
        .game-item:last-child { border-bottom: none; }
        .props-grid {
            display: grid;
            gap: 20px;
        }
        .game-section {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }
        .game-title {
            color: #2d3748;
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e2e8f0;
        }
        .prop-card {
            background: #f7fafc;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 12px;
            border-left: 4px solid #805ad5;
        }
        .prop-player {
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 8px;
        }
        .prop-details {
            display: grid;
            gap: 6px;
            font-size: 14px;
            color: #4a5568;
        }
        .prop-line {
            background: white;
            padding: 4px 8px;
            border-radius: 4px;
            display: inline-block;
        }
        .odds {
            color: #38a169;
            font-weight: 600;
        }
        .weekly-values {
            font-size: 12px;
            color: #718096;
            margin-top: 6px;
        }
        .error {
            background: #fed7d7;
            color: #c53030;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
        }
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: white;
        }
        .refresh-btn {
            background: #805ad5;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            transition: background 0.2s;
        }
        .refresh-btn:hover {
            background: #6b46c1;
        }
        @media (max-width: 768px) {
            .header { padding: 20px; }
            .status { gap: 10px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>
                🏈 NFL Alt Props Tracker
                <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
            </h1>
            <div class="status">
                <div class="status-item">
                    📅 {{ data.current_day }}
                </div>
                <div class="status-item">
                    🏈 Week <span class="badge">{{ data.current_week }}</span>
                </div>
                <div class="status-item">
                    ⏰ Updated: {{ data.last_updated_formatted }}
                </div>
                <div class="status-item">
                    📊 Props: <span class="badge">{{ data.summary.total_props }}</span>
                </div>
            </div>
            
            {% if data.games %}
            <div class="games-list">
                <strong>Analyzing {{ data.summary.total_games }} games:</strong>
                {% for game in data.games %}
                <div class="game-item">{{ game.matchup }} - {{ game.time }}</div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        
        {% if data.error %}
        <div class="error">
            ⚠️ Error: {{ data.error }}
        </div>
        {% endif %}
        
        {% if data.props %}
        <div class="props-grid">
            {% for game, game_props in data.props_by_game.items() %}
            <div class="game-section">
                <div class="game-title">
                    {{ game }} - {{ game_props[0].game_time }}
                </div>
                {% for prop in game_props %}
                <div class="prop-card">
                    <div class="prop-player">{{ prop.player }}</div>
                    <div class="prop-details">
                        <div>
                            <span class="prop-line">{{ prop.market }}: {{ prop.side }} {{ prop.line }}</span>
                        </div>
                        <div>
                            Odds: <span class="odds">{{ prop.odds }}</span> | 
                            Season Avg: <strong>{{ prop.season_avg }}</strong>
                        </div>
                        <div class="weekly-values">
                            Weekly: {{ prop.weekly_values }}
                        </div>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% endfor %}
        </div>
        {% elif not data.error %}
        <div class="empty-state">
            <h2>No qualifying props found</h2>
            <p>Check back later for updates!</p>
        </div>
        {% endif %}
    </div>
    
    <script>
        // Auto-refresh every 5 minutes
        setTimeout(() => location.reload(), 300000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    """Main route to display props"""
    with data_lock:
        data = latest_props_data.copy()
    
    # Format the last updated time
    if data["last_updated"]:
        dt = datetime.fromisoformat(data["last_updated"])
        data["last_updated_formatted"] = dt.strftime("%I:%M %p ET")
    else:
        data["last_updated_formatted"] = "Never"
    
    # Group props by game
    props_by_game = {}
    for prop in data.get("props", []):
        game = prop["game"]
        if game not in props_by_game:
            props_by_game[game] = []
        props_by_game[game].append(prop)
    
    data["props_by_game"] = props_by_game
    
    return render_template_string(HTML_TEMPLATE, data=data)

@app.route('/api/props')
def api_props():
    """API endpoint to get props as JSON"""
    with data_lock:
        return jsonify(latest_props_data)

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "last_updated": latest_props_data.get("last_updated")})

def init_scheduler():
    """Initialize the background scheduler"""
    scheduler = BackgroundScheduler()
    
    # Run immediately on startup
    fetch_nfl_props()
    
    # Schedule to run every 30 minutes
    scheduler.add_job(
        func=fetch_nfl_props,
        trigger="interval",
        minutes=30,
        id='fetch_props',
        name='Fetch NFL Props',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("Scheduler started - will update every 30 minutes")

if __name__ == '__main__':
    # Initialize scheduler
    init_scheduler()
    
    # Get port from environment variable (Railway sets this)
    port = int(os.getenv('PORT', 5000))
    
    # Run Flask app
    app.run(host='0.0.0.0', port=port, debug=False)
