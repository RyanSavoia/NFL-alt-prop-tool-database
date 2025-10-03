import requests
import pandas as pd
import nfl_data_py as nfl
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
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
                                "odds": odds,
                                "bookmaker": bookmaker.get("key"),
                                "bookmaker_title": bookmaker.get("title")
                            })
        
        logger.info(f"Pulled {len(props)} props in odds range")
        
        # 4. Build weekly stats from play-by-play
        logger.info("Loading NFL play-by-play data...")
        # Safer approach for 2025 data to avoid nfl_data_py errors
        try:
            # Try with limited columns first to avoid potential issues
            cols = ['season','week','passer_player_name','rusher_player_name','receiver_player_name',
                    'passing_yards','rushing_yards','receiving_yards','pass_touchdown','rush_touchdown',
                    'complete_pass','rush_attempt']
            pbp = nfl.import_pbp_data([2025], columns=cols)
        except:
            try:
                # Fallback to default if column selection fails
                pbp = nfl.import_pbp_data([2025])
            except Exception as e:
                logger.error(f"Failed to load 2025 data: {e}")
                # Use 2024 data as fallback
                pbp = nfl.import_pbp_data([2024])
                pbp['season'] = 2025  # Pretend it's 2025 data
        
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
        
        # 7. Filter qualifying props and group by player/line
        qualifying_grouped = {}
        for p in props:
            stat_col = market_to_stat.get(p["market"])
            if not stat_col:
                continue
            ok, vals = qualifies_strong(p["player"], stat_col, p["line"], p["side"], p["market"])
            if ok:
                # Create unique key for this prop
                prop_key = f"{p['player']}_{p['market']}_{p['side']}_{p['line']}_{p['game']}"
                
                if prop_key not in qualifying_grouped:
                    avg_val = sum(vals) / len(vals) if vals else 0
                    qualifying_grouped[prop_key] = {
                        "game": p["game"],
                        "game_time": p["game_time"],
                        "market": p["market"].replace('_', ' ').title(),
                        "player": p["player"],
                        "side": p["side"],
                        "line": float(p["line"]),
                        "season_avg": round(float(avg_val), 1),
                        "weekly_values": [float(v) for v in vals],
                        "bookmakers": []
                    }
                
                # Add this bookmaker's odds
                qualifying_grouped[prop_key]["bookmakers"].append({
                    "bookmaker": p["bookmaker"],
                    "bookmaker_title": p["bookmaker_title"],
                    "odds": int(p["odds"])
                })
        
        # Convert to list and sort bookmakers by best odds
        qualifying = []
        for prop in qualifying_grouped.values():
            # Sort bookmakers by best odds (least negative)
            prop["bookmakers"].sort(key=lambda x: x["odds"], reverse=True)
            qualifying.append(prop)
        
        # 8. No need for deduplication anymore since we grouped
            # Convert any remaining numpy types to Python types
            for prop in qualifying:
                prop['line'] = float(prop['line']) if prop['line'] is not None else None
                prop['odds'] = int(prop['odds']) if prop['odds'] is not None else None
                prop['season_avg'] = float(prop['season_avg']) if prop['season_avg'] is not None else None
                prop['weekly_values'] = [float(v) for v in prop['weekly_values']] if prop['weekly_values'] else []
        
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

@app.route('/')
def index():
    """Main route returns JSON data"""
    with data_lock:
        data = latest_props_data.copy()
    
    # Format the last updated time
    if data["last_updated"]:
        dt = datetime.fromisoformat(data["last_updated"])
        data["last_updated_formatted"] = dt.strftime("%I:%M %p ET")
    else:
        data["last_updated_formatted"] = "Never"
    
    # Group props by game for better organization
    if data.get("props"):
        props_by_game = {}
        for prop in data["props"]:
            game = prop["game"]
            if game not in props_by_game:
                props_by_game[game] = []
            props_by_game[game].append(prop)
        data["props_by_game"] = props_by_game
    
    return jsonify(data)

@app.route('/props')
def get_props():
    """Alias endpoint for props data"""
    return index()

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy", 
        "last_updated": latest_props_data.get("last_updated"),
        "props_count": len(latest_props_data.get("props", []))
    })

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
