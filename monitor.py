import requests
import json
import os
import time
from datetime import datetime

# Configuration
API_URL = "https://iwm.diskin.net/api/state/snapshot"
SEEN_EVENTS_FILE = "seen_events.json"
CHECK_INTERVAL = 1  # Check every 1 second

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8731876743:AAEqaBaT-u9-QkGHMRoaflHm80QK_lJU-dI")
TELEGRAM_CHAT_IDS = [58224768, 5597700550]  # Ar + Harel
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def load_seen_events():
    if os.path.exists(SEEN_EVENTS_FILE):
        try:
            with open(SEEN_EVENTS_FILE, 'r') as f:
                return set(json.load(f))
        except:
            return set()
    return set()


def save_seen_events(seen_events):
    with open(SEEN_EVENTS_FILE, 'w') as f:
        json.dump(list(seen_events), f)


def send_telegram(message):
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            data = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            response = requests.post(TELEGRAM_API_URL, json=data, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"Telegram message SENT to chat_id {chat_id}")
            else:
                print(f"Telegram SEND FAILED to {chat_id}: {result.get('description', 'Unknown error')}")
        except Exception as e:
            print(f"Error sending Telegram to {chat_id}: {str(e)}")


def is_iran_launch(event):
    event_type = event.get('eventType')
    properties = event.get('properties', {})

    if event_type == 'missile_track':
        missile_track = properties.get('missileTrack', {})
        source = missile_track.get('sourceCountry', '') or missile_track.get('sourceRegion', '')
        if 'Iran' in source or 'iran' in source or '\u05d0\u05d9\u05e8\u05d0\u05df' in source:
            return True, f"Missile track from {source}"

    if event_type == 'alert':
        alert_type = properties.get('alertType', '')
        if any(kw in alert_type for kw in ['Rocket', 'Missile', '\u05d9\u05e8\u05d9 \u05e8\u05e7\u05d8\u05d5\u05ea \u05d5\u05d8\u05d9\u05dc\u05d9\u05dd', '\u05d8\u05d9\u05dc\u05d9\u05dd \u05d1\u05dc\u05d9\u05e1\u05d8\u05d9\u05d9\u05dd']):
            missile_track = properties.get('missileTrack', {})
            missile_path = properties.get('missilePath', {})
            source_country = missile_track.get('sourceCountry', '')
            source_region = missile_track.get('sourceRegion', '')
            origin_label = missile_path.get('origin', {}).get('label', '')
            combined_source = f"{source_country} {source_region} {origin_label}"
            if 'Iran' in combined_source or 'iran' in combined_source or '\u05d0\u05d9\u05e8\u05d0\u05df' in combined_source:
                return True, f"Alert: {alert_type} from {combined_source.strip()}"

    if event_type == 'social' and properties.get('channelUsername') == 'shigurimIL':
        is_missile = properties.get('isMissileRelated', False)
        source_country = properties.get('sourceCountry', '')
        text = properties.get('text', '')
        if is_missile and ('Iran' in str(source_country) or 'iran' in str(source_country) or '\u05d0\u05d9\u05e8\u05d0\u05df' in text):
            return True, f"Pre-warning: {text[:100]}"
        if ('\u05d0\u05d9\u05e8\u05d0\u05df' in text or 'Iran' in text) and ('\u05e9\u05d9\u05d2\u05d5\u05e8' in text or '\u05d8\u05d9\u05dc' in text or '\u05d1\u05dc\u05d9\u05e1\u05d8\u05d9' in text):
            return True, f"Pre-warning: {text[:100]}"

    if event_type in ['social', 'news']:
        text = properties.get('text', '')
        title = properties.get('title', '')
        combined = f"{text} {title}"
        iran_keywords = [
            '\u05e9\u05d9\u05d2\u05d5\u05e8 \u05de\u05d0\u05d9\u05e8\u05d0\u05df',
            '\u05e9\u05d5\u05d2\u05e8\u05d5 \u05de\u05d0\u05d9\u05e8\u05d0\u05df',
            '\u05d8\u05d9\u05dc\u05d9\u05dd \u05de\u05d0\u05d9\u05e8\u05d0\u05df',
            'missiles from Iran', 'launched from Iran', 'Iran launched', 'Iran fires',
            '\u05d8\u05d9\u05dc\u05d9\u05dd \u05d1\u05dc\u05d9\u05e1\u05d8\u05d9\u05d9\u05dd \u05de\u05d0\u05d9\u05e8\u05d0\u05df',
            '\u05e9\u05d9\u05d2\u05d5\u05e8 \u05d0\u05d9\u05e8\u05d0\u05e0\u05d9',
            'Iran missile launch'
        ]
        for keyword in iran_keywords:
            if keyword.lower() in combined.lower():
                return True, f"News/Social: {keyword} detected"

    return False, ""


def check_for_launches(seen_events):
    alerts_sent = 0

    try:
        response = requests.get(API_URL, timeout=15)
        if response.status_code != 200:
            print(f"Error: API returned status code {response.status_code}")
            return seen_events, 0
        data = response.json()
        events = data.get('events', [])

        for event in events:
            event_id = event.get('eventId')
            if event_id in seen_events:
                continue

            is_launch, reason = is_iran_launch(event)

            if is_launch:
                event_time = event.get('time', 'Unknown')
                properties = event.get('properties', {})
                title = properties.get('title', '\u05e9\u05d9\u05d2\u05d5\u05e8 \u05de\u05d6\u05d5\u05d4\u05d4')
                alert_msg = (
                    f"\U0001f6a8 <b>\u05d4\u05ea\u05e8\u05d0\u05ea \u05e9\u05d9\u05d2\u05d5\u05e8 \u05de\u05d0\u05d9\u05e8\u05d0\u05df!</b> \U0001f6a8\n\n"
                    f"\u23f0 <b>\u05d6\u05de\u05df:</b> {event_time}\n"
                    f"\U0001f4cb <b>\u05e4\u05e8\u05d8\u05d9\u05dd:</b> {title}\n"
                    f"\U0001f50d <b>\u05e1\u05d9\u05d1\u05d4:</b> {reason}"
                )
                print(f"IRAN LAUNCH DETECTED: {reason}")
                send_telegram(alert_msg)
                alerts_sent += 1

            seen_events.add(event_id)

        if len(seen_events) > 5000:
            seen_events = set(list(seen_events)[-5000:])

        save_seen_events(seen_events)

    except Exception as e:
        print(f"Exception during check: {str(e)}")

    return seen_events, alerts_sent


def main():
    print("=" * 50)
    print("Iran Missile Launch Monitor - Starting...")
    print(f"Check interval: {CHECK_INTERVAL} second(s)")
    print(f"Telegram recipients: {TELEGRAM_CHAT_IDS}")
    print("=" * 50)

    # Send startup notification
    send_telegram("\u2705 <b>\u05de\u05e2\u05e8\u05db\u05ea \u05e0\u05d9\u05d8\u05d5\u05e8 \u05e9\u05d9\u05d2\u05d5\u05e8\u05d9\u05dd \u05de\u05d0\u05d9\u05e8\u05d0\u05df \u05d4\u05d5\u05e4\u05e2\u05dc\u05d4!</b>\n\u05d1\u05d5\u05d3\u05e7\u05ea \u05db\u05dc \u05e9\u05e0\u05d9\u05d9\u05d4. \u05ea\u05e7\u05d1\u05dc\u05d5 \u05d4\u05ea\u05e8\u05d0\u05d4 \u05d1\u05e8\u05d2\u05e2 \u05e9\u05d9\u05d6\u05d5\u05d4\u05d4 \u05e9\u05d9\u05d2\u05d5\u05e8.")

    seen_events = load_seen_events()

    # Initial scan - mark all existing events as seen (no spam on startup)
    if len(seen_events) == 0:
        print("First run - marking existing events as seen...")
        try:
            response = requests.get(API_URL, timeout=15)
            if response.status_code == 200:
                data = response.json()
                events = data.get('events', [])
                for event in events:
                    seen_events.add(event.get('eventId'))
                save_seen_events(seen_events)
                print(f"Marked {len(seen_events)} existing events as seen.")
        except Exception as e:
            print(f"Error during initial scan: {str(e)}")

    check_count = 0
    while True:
        try:
            seen_events, alerts = check_for_launches(seen_events)
            check_count += 1
            if check_count % 60 == 0:  # Print status every 60 checks (~1 minute)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Still running... Check #{check_count}")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("Monitor stopped by user.")
            break
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            time.sleep(5)


if __name__ == "__main__":
    main()
