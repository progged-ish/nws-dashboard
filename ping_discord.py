import subprocess
import time
import requests
from datetime import datetime

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1486127286557085806/KbosGzeAomOfTHGK-G_5KHKNhSnzdInBqYLr0UtI1gKK2r8YaXjBWYl1LTPYiUnokzKR"
STATUS_INTERVAL = 30


def check_processes():
    """Check if any relevant Python/METAR processes are running."""
    try:
        result = subprocess.run(
            "ps aux | grep -E 'python|metar|parse' | grep -v grep",
            shell=True, capture_output=True, text=True
        )
        return len(result.stdout.strip().split('\n')) > 0
    except Exception:
        return False


def send_ping(status_msg):
    """Send status update to Discord webhook."""
    try:
        payload = {
            "content": f"☤ Hermes System Monitor\n\n{status_msg}\n",
            "username": "Progged Bot"
        }
        resp = requests.post(DISCORD_WEBHOOK, json=payload)
        return resp.status_code in [200, 201]
    except Exception as e:
        print(f"ERROR sending ping: {e}")
        return False


def main():
    """Main loop for status pinging."""
    print("Starting 30-second Discord status pinger...")
    while True:
        if check_processes():
            status_msg = f"[{datetime.now().strftime('%H:%M:%S')}] Active: Weather/Parser scripts are running."
        else:
            status_msg = f"[{datetime.now().strftime('%H:%M:%S')}] Idle: No background python scripts detected."

        send_ping(status_msg)
        print(status_msg)
        time.sleep(STATUS_INTERVAL)


if __name__ == "__main__":
    main()