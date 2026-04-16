#!/usr/bin/env python3
"""NWS Dashboard - Web interface for weather discussions."""

from flask import Flask, render_template_string
from datetime import datetime
import threading
import time
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global cache
_cache = {
    "last_update": None,
    "tucson_afd": None,
    "status": "initializing"
}


def fetch_and_cache_afd():
    """Background task to fetch AFD and update cache."""
    global _cache
    
    try:
        from nws_afd_fetcher import fetch_tucson_afd
        
        text = fetch_tucson_afd()
        
        data_dir = Path.home() / ".nws_dashboard"
        data_dir.mkdir(exist_ok=True)
        
        with open(data_dir / "latest_afd.txt", "w") as f:
            f.write(f"# Last updated: {datetime.now().isoformat()}\n\n{text}")
        
        _cache["tucson_afd"] = text
        _cache["last_update"] = datetime.now().isoformat()
        _cache["status"] = "ok"
        logger.info("AFD cache updated successfully")
    except Exception as e:
        _cache["status"] = f"error: {str(e)}"
        logger.error(f"Failed to update AFD cache: {e}")


def background_refresh(interval=300):
    """Refresh AFD every N seconds in background."""
    while True:
        fetch_and_cache_afd()
        time.sleep(interval)


DASHBOARD_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NWS Dashboard - Tucson AFD</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin: 0;
            padding: 20px;
            min-height: 100vh;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 10px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(90deg, #2c3e50, #3498db);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 {
            margin: 0;
            font-size: 2.5em;
        }
        .status {
            display: inline-block;
            background: #27ae60;
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            margin-top: 10px;
        }
        .status.error { background: #e74c3c; }
        .content {
            padding: 30px;
        }
        .info-bar {
            display: flex;
            justify-content: space-between;
            margin-bottom: 20px;
            color: #666;
            font-size: 0.9em;
        }
        .afd-container {
            background: #f8f9fa;
            border-left: 4px solid #3498db;
            padding: 20px;
            margin-bottom: 20px;
            max-height: 500px;
            overflow-y: auto;
        }
        .afd-text {
            white-space: pre-wrap;
            font-family: 'Consolas', monospace;
            line-height: 1.6;
            color: #333;
        }
        .refresh-btn {
            background: #3498db;
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1em;
            transition: all 0.3s;
        }
        .refresh-btn:hover { background: #2980b9; transform: translateY(-2px); }
        .footer {
            text-align: center;
            padding: 20px;
            color: white;
            font-size: 0.8em;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>NWS Dashboard</h1>
            <div id="status-badge" class="status {% if status == 'error' %}error{% endif %}">
                Last updated: {{ last_update or "Never" }} | Status: {{ status }}
            </div>
        </div>
        
        <div class="content">
            <div class="info-bar">
                <span>Tucson Area Forecast Discussion</span>
                <button onclick="refreshData()" class="refresh-btn">Refresh Now</button>
            </div>
            
            <div class="afd-container">
                <pre class="afd-text">{{ afd_text or "No data available. Click refresh to load." }}</pre>
            </div>
        </div>
        
        <div class="footer">
            Data from National Weather Service | Auto-refreshes every 5 minutes
        </div>
    </div>
    
    <script>
        function refreshData() {
            document.querySelector('.refresh-btn').textContent = 'Refreshing...';
            setTimeout(() => location.reload(), 100);
        }
        
        setInterval(() => location.reload(), 300000);
    </script>
</body>
</html>
'''


@app.route('/')
def dashboard():
    """Main dashboard view."""
    return render_template_string(
        DASHBOARD_HTML,
        afd_text=_cache.get("tucson_afd"),
        last_update=_cache.get("last_update", "Never"),
        status=_cache.get("status")
    )


@app.route('/api/status')
def api_status():
    """API endpoint for status checks."""
    from flask import jsonify
    return jsonify({
        "status": _cache.get("status"),
        "last_update": _cache.get("last_update"),
        "has_data": bool(_cache.get("tucson_afd"))
    })


if __name__ == "__main__":
    # Initialize cache
    fetch_and_cache_afd()
    
    # Start background refresh thread
    refresh_thread = threading.Thread(
        target=background_refresh,
        args=(300,),  # Refresh every 5 minutes
        daemon=True
    )
    refresh_thread.start()
    
    print("Starting NWS Dashboard...")
    print("Access it at: http://localhost:5000")
    print("Press Ctrl+C to stop the server")
    
    app.run(host="0.0.0.0", port=5000, debug=False)
