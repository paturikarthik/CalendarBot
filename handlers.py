import json
import os
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
import requests
from datetime import datetime, timedelta
import calendar
import re

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Store pending events per user (in-memory, will reset on redeploy)
pending_events = {}

# Color mapping for Google Calendar
CALENDAR_COLORS = {
    'red': '11',
    'orange': '6',
    'yellow': '5',
    'green': '10',
    'blue': '9',
    'purple': '3',
    'pink': '4',
    'gray': '8',
}

# Singapore Public Holidays (update annually from https://www.mom.gov.sg/employment-practices/public-holidays)
SINGAPORE_PUBLIC_HOLIDAYS = {
    2025: [
        "2025-01-01",  # New Year's Day
        "2025-01-29",  # Chinese New Year
        "2025-01-30",  # Chinese New Year
        "2025-04-18",  # Good Friday
        "2025-05-01",  # Labour Day
        "2025-05-12",  # Vesak Day
        "2025-06-02",  # Hari Raya Puasa
        "2025-08-09",  # National Day
        "2025-08-09",  # Hari Raya Haji (falls on National Day)
        "2025-10-20",  # Deepavali
        "2025-12-25",  # Christmas Day
    ],
    2026: [
        "2026-01-01",  # New Year's Day
        "2026-02-17",  # Chinese New Year
        "2026-02-18",  # Chinese New Year
        "2026-04-03",  # Good Friday
        "2026-05-01",  # Labour Day
        "2026-05-31",  # Vesak Day
        "2026-06-03",  # Hari Raya Puasa (estimated)
        "2026-08-10",  # National Day (observed)
        "2026-08-20",  # Hari Raya Haji (estimated)
        "2026-11-08",  # Deepavali (estimated)
        "2026-12-25",  # Christmas Day
    ]
}

def get_calendar_service():
    """Get Google Calendar service using service account"""
    service_account_info = json.loads(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"))
    
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=SCOPES)
    
    return build('calendar', 'v3', credentials=credentials)

def get_nth_working_day_of_month(year, month, n):
    """
    Calculate the nth working day of a given month.
    Working day = weekday (Mon-Fri) that's not a public holiday.
    
    Args:
        year: Year (e.g., 2025)
        month: Month (1-12)
        n: Which working day (e.g., 7 for 7th working day)
    
    Returns:
        Day of month for the nth working day, or None if not found
    """
    # Get public holidays for the year
    holidays = set(SINGAPORE_PUBLIC_HOLIDAYS.get(year, []))
    
    # Start from the first day of the month
    current_date = datetime(year, month, 1)
    working_days_count = 0
    
    # Count working days
    while current_date.month == month:
        date_str = current_date.strftime("%Y-%m-%d")
        
        # Check if it's a weekday (Mon=0 to Fri=4) and not a public holiday
        if current_date.weekday() < 5 and date_str not in holidays:
            working_days_count += 1
            
            if working_days_count == n:
                return current_date.day
        
        current_date += timedelta(days=1)
    
    # If we didn't find enough working days, return None
    return None

def calculate_working_day_dates(n, start_date_str, num_occurrences=12):
    """
    Calculate the nth working day for multiple months starting from start_date.
    
    Args:
        n: Which working day (e.g., 7)
        start_date_str: Starting date in ISO format (e.g., "2025-11-01T14:00:00")
        num_occurrences: Number of occurrences to calculate (default: 12 for 1 year)
    
    Returns:
        List of dates in YYYYMMDD format
    """
    start_date = datetime.fromisoformat(start_date_str.split('T')[0])
    dates = []
    
    # Calculate for next num_occurrences months
    for month_offset in range(num_occurrences):
        # Calculate target month
        year = start_date.year
        month = start_date.month + month_offset
        
        # Handle year rollover
        while month > 12:
            month -= 12
            year += 1
        
        day = get_nth_working_day_of_month(year, month, n)
        if day:
            date_obj = datetime(year, month, day)
            # Only add if date is in the future or today
            if date_obj.date() >= start_date.date():
                dates.append(date_obj.strftime("%Y%m%d"))
    
    return dates

def get_ordinal_suffix(n):
    """Get ordinal suffix for a number (1st, 2nd, 3rd, etc.)"""
    if 10 <= n % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return suffix

def parse_recurrence_natural(text, event_start):
    """Convert natural language recurrence to RRULE using AI and working day calculation"""
    
    # Check if it's a working day pattern
    working_day_match = re.search(r'(\d+)(?:st|nd|rd|th)?\s+working\s+day', text.lower())
    
    if working_day_match:
        n = int(working_day_match.group(1))
        
        # Check if duration is specified (e.g., "for 6 months")
        duration_match = re.search(r'for\s+(\d+)\s+month', text.lower())
        num_occurrences = int(duration_match.group(1)) if duration_match else 12
        
        # Calculate actual dates for the nth working day
        dates = calculate_working_day_dates(n, event_start, num_occurrences)
        
        if dates:
            explanation = f"Every {n}{get_ordinal_suffix(n)} working day of the month (excluding weekends & Singapore public holidays) - {len(dates)} individual events"
            
            return {
                'type': 'working_day',
                'explanation': explanation,
                'dates': dates,
                'n': n
            }
    
    # For non-working-day patterns, use AI to generate RRULE
    try:
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert at converting natural language recurrence patterns to Google Calendar RRULE format. "
                        "Reply ONLY with a JSON object containing 'rrule' and 'explanation'. "
                        "The RRULE must be valid for Google Calendar API.\n\n"
                        
                        "Common patterns:\n"
                        "- 'every day' -> RRULE:FREQ=DAILY\n"
                        "- 'every week' -> RRULE:FREQ=WEEKLY\n"
                        "- 'every 2 weeks' -> RRULE:FREQ=WEEKLY;INTERVAL=2\n"
                        "- 'weekdays only' -> RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\n"
                        "- 'every monday and wednesday' -> RRULE:FREQ=WEEKLY;BYDAY=MO,WE\n"
                        "- 'daily for 10 times' -> RRULE:FREQ=DAILY;COUNT=10\n"
                        "- 'weekly until December 31' -> RRULE:FREQ=WEEKLY;UNTIL=20251231T235959Z\n"
                        "- 'every month' -> RRULE:FREQ=MONTHLY\n"
                        "- 'every month on the 15th' -> RRULE:FREQ=MONTHLY;BYMONTHDAY=15\n"
                        "- 'first monday of every month' -> RRULE:FREQ=MONTHLY;BYDAY=1MO\n"
                        "- 'last friday of every month' -> RRULE:FREQ=MONTHLY;BYDAY=-1FR\n"
                        "- 'second and fourth tuesday' -> RRULE:FREQ=MONTHLY;BYDAY=2TU,4TU\n"
                        "- 'every 3 months' -> RRULE:FREQ=MONTHLY;INTERVAL=3\n"
                        "- 'yearly on march 15' -> RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=15\n"
                        "- 'bi-weekly' -> RRULE:FREQ=WEEKLY;INTERVAL=2\n"
                        "- 'quarterly' -> RRULE:FREQ=MONTHLY;INTERVAL=3\n\n"
                        
                        "Day abbreviations: MO, TU, WE, TH, FR, SA, SU\n"
                        "Month numbers: 1=Jan, 2=Feb, 3=Mar, 4=Apr, 5=May, 6=Jun, 7=Jul, 8=Aug, 9=Sep, 10=Oct, 11=Nov, 12=Dec\n\n"
                        
                        "Output format:\n"
                        "{\n"
                        "  \"rrule\": \"RRULE:FREQ=...\",\n"
                        "  \"explanation\": \"Human-readable explanation\"\n"
                        "}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Convert this to RRULE: {text}"
                },
            ],
        )
        
        result = json.loads(response.choices[0].message.content.strip())
        rrule = result.get('rrule')
        explanation = result.get('explanation', text)
        
        if rrule and rrule.startswith('RRULE:'):
            return {
                'type': 'standard',
                'rrule': rrule,
                'explanation': explanation
            }
        else:
            return None
    
    except Exception as e:
        print(f"Error parsing recurrence: {e}")
        return None

def parse_reminder_natural(text):
    """Convert natural language reminders to minutes"""
    reminders = []
    text_lower = text.lower()
    
    # Common patterns
    patterns = {
        '10 min': 10,
        '15 min': 15,
        '30 min': 30,
        '1 hour': 60,
        '2 hour': 120,
        '1 day': 1440,
        '2 day': 2880,
        '1 week': 10080,
    }
    
    for pattern, minutes in patterns.items():
        if pattern in text_lower or pattern.replace(' ', '') in text_lower:
            reminders.append({
                'method': 'popup',
                'minutes': minutes
            })
    
    # If no patterns found, try to extract numbers
    if not reminders:
        # Look for patterns like "10 minutes", "1 hour", "2 days"
        time_matches = re.findall(r'(\d+)\s*(min|minute|hour|day|week)', text_lower)
        for num, unit in time_matches:
            num = int(num)
            if 'min' in unit:
                minutes = num
            elif 'hour' in unit:
                minutes = num * 60
            elif 'day' in unit:
                minutes = num * 1440
            elif 'week' in unit:
                minutes = num * 10080
            
            reminders.append({
                'method': 'popup',
                'minutes': minutes
            })
    
    return reminders if reminders else None

def extract_time_with_ai(text, current_event):
    """Extract time updates using AI"""
    try:
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract start and end datetime from the text. "
                        "Reply ONLY in JSON format with 'start' and 'end' in ISO 8601 format. "
                        f"Current event time: {current_event['start']} to {current_event['end']}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Extract time from: {text}"
                },
            ],
        )
        
        time_data = json.loads(response.choices[0].message.content.strip())
        return time_data.get('start'), time_data.get('end')
    
    except Exception:
        return None, None

def parse_edit_field(text):
    """Parse field-specific edits like 'title: New Title'"""
    text = text.strip()
    
    # Check for field: value pattern
    if ':' in text:
        field, value = text.split(':', 1)
        field = field.strip().lower()
        value = value.strip()
        
        if field in ['title', 'location', 'description']:
            return field, value
        elif field == 'time':
            return 'time', value
    
    return None, None

def create_calendar_event(event_data, email):
    """Create event in Google Calendar"""
    try:
        service = get_calendar_service()
        
        # Check if this is a working day recurrence that needs multiple events
        if event_data.get('recurrence_data') and event_data['recurrence_data'].get('type') == 'working_day':
            rec_data = event_data['recurrence_data']
            dates = rec_data.get('dates', [])
            
            if not dates:
                raise Exception("No working day dates calculated")
            
            # Create individual events for each calculated date
            created_links = []
            
            # Parse original start/end to get time components
            start_dt = datetime.fromisoformat(event_data['start'])
            end_dt = datetime.fromisoformat(event_data['end'])
            start_time = start_dt.time()
            end_time = end_dt.time()
            
            for date_str in dates:
                # Parse the date (format: YYYYMMDD)
                event_date = datetime.strptime(date_str, "%Y%m%d")
                
                # Combine with original times
                event_start = datetime.combine(event_date, start_time)
                event_end = datetime.combine(event_date, end_time)
                
                event = {
                    'summary': event_data['title'],
                    'description': event_data.get('description'),
                    'location': event_data.get('location'),
                    'start': {
                        'dateTime': event_start.isoformat(),
                        'timeZone': 'Asia/Singapore',
                    },
                    'end': {
                        'dateTime': event_end.isoformat(),
                        'timeZone': 'Asia/Singapore',
                    },
                }
                
                # Add color if specified
                if event_data.get('color'):
                    event['colorId'] = event_data['color']
                
                # Add reminders if specified
                if event_data.get('reminders'):
                    event['reminders'] = {
                        'useDefault': False,
                        'overrides': event_data['reminders']
                    }
                
                # Create the event
                created_event = service.events().insert(calendarId=email, body=event).execute()
                created_links.append(created_event.get('htmlLink'))
            
            # Return first event link
            return created_links[0] if created_links else None
        
        else:
            # Standard single event or RRULE-based recurrence
            event = {
                'summary': event_data['title'],
                'description': event_data.get('description'),
                'location': event_data.get('location'),
                'start': {
                    'dateTime': event_data['start'],
                    'timeZone': 'Asia/Singapore',
                },
                'end': {
                    'dateTime': event_data['end'],
                    'timeZone': 'Asia/Singapore',
                },
            }
            
            # Handle standard recurrence (non-working-day) using RRULE
            if event_data.get('recurrence_data') and event_data['recurrence_data'].get('type') == 'standard':
                rec_data = event_data['recurrence_data']
                event['recurrence'] = [rec_data['rrule']]
            elif event_data.get('recurrence'):
                # Legacy support
                event['recurrence'] = [event_data['recurrence']]
            
            # Add color if specified
            if event_data.get('color'):
                event['colorId'] = event_data['color']
            
            # Add reminders if specified
            if event_data.get('reminders'):
                event['reminders'] = {
                    'useDefault': False,
                    'overrides': event_data['reminders']
                }
            
            created_event = service.events().insert(calendarId=email, body=event).execute()
            return created_event.get('htmlLink')
    
    except Exception as e:
        raise Exception(f"Calendar API error: {str(e)}")

def send_message(chat_id, text, parse_mode="Markdown"):
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    })

def retrieve_email(user_id):
    if str(user_id) == "477194086":
        return "paturi.karthik1@gmail.com"
    elif str(user_id) == "545873418":
        return "shaneeqa24@gmail.com"
    elif str(user_id) == "312007192":
        return "brigitte.tan@gmail.com"
    return None

import urllib.parse

def generate_invite_link(event_data):
    """
    Generate a shareable Google Calendar 'Add to Calendar' template link.
    Anyone who clicks this link can add the pre-filled event to their own calendar.
    """
    base_url = "https://calendar.google.com/calendar/render?action=TEMPLATE"
    
    # Remove hyphens and colons from ISO format for Google Calendar URL requirements
    # e.g., '2025-10-23T18:00:00' -> '20251023T180000'
    start = event_data.get('start', '').replace('-', '').replace(':', '')
    end = event_data.get('end', '').replace('-', '').replace(':', '')
    
    params = {
        'text': event_data.get('title', ''),
        'dates': f"{start}/{end}",
        'ctz': 'Asia/Singapore'  # Matching the timezone used in your calendar creation
    }
    
    if event_data.get('description'):
        params['details'] = event_data.get('description')
        
    if event_data.get('location'):
        params['location'] = event_data.get('location')
        
    # Build the final URL
    query_string = urllib.parse.urlencode(params, safe='/')
    
    return f"{base_url}&{query_string}"

def format_event_preview(event_data):
    """Format event data for preview"""
    msg = "📋 *Event Preview:*\n\n"
    msg += f"📅 *{event_data['title']}*\n"
    msg += f"🕐 {event_data['start']} - {event_data['end']}\n"
    
    if event_data.get('description'):
        msg += f"📝 {event_data['description']}\n"
    
    if event_data.get('location'):
        msg += f"📍 {event_data['location']}\n"
    
    if event_data.get('recurrence_data'):
        explanation = event_data['recurrence_data'].get('explanation', 'Recurring event')
        msg += f"🔄 {explanation}\n"
    
    if event_data.get('color'):
        color_name = [k for k, v in CALENDAR_COLORS.items() if v == event_data['color']]
        if color_name:
            msg += f"🎨 Color: {color_name[0]}\n"
    
    if event_data.get('reminders'):
        reminder_text = ", ".join([f"{r['minutes']}min" if r['minutes'] < 60 
                                else f"{r['minutes']//60}hr" 
                                for r in event_data['reminders']])
        msg += f"⏰ Reminders: {reminder_text}\n"
    
    msg += "\n*Options:*\n"
    msg += "✅ /yes - Create event\n"
    msg += "❌ /no - Cancel\n"
    msg += "✏️ /edit - Modify specific fields\n"
    msg += "🔄 /recurring - Make it recurring\n"
    msg += "⏰ /reminder - Add reminders\n"
    msg += "🎨 /colour - Set color\n"
    
    return msg

def handle_yes(chat_id, user_id, email):
    """Handle /yes command - create the event"""
    if user_id not in pending_events:
        send_message(chat_id, "❌ No pending event found. Send me event details first!")
        return
    
    event_data = pending_events[user_id]
    
    try:
        # Check if working day recurrence
        is_working_day = (event_data.get('recurrence_data') and 
                        event_data['recurrence_data'].get('type') == 'working_day')
        
        if is_working_day:
            num_dates = len(event_data['recurrence_data'].get('dates', []))
            send_message(chat_id, f"🔄 Creating {num_dates} individual events for working day pattern...")
        
        event_link = create_calendar_event(event_data, email)
        
        success_msg = f"✅ Event created successfully!\n\n"
        success_msg += f"📅 *{event_data['title']}*\n"
        
        if is_working_day:
            num_dates = len(event_data['recurrence_data'].get('dates', []))
            success_msg += f"🔄 Created {num_dates} individual events\n"
            success_msg += f"Pattern: Every {event_data['recurrence_data']['n']}{get_ordinal_suffix(event_data['recurrence_data']['n'])} working day\n"
        else:
            success_msg += f"🕐 {event_data['start']} - {event_data['end']}\n"
            if event_data.get('recurrence_data'):
                success_msg += f"🔄 {event_data['recurrence_data'].get('explanation', 'Recurring')}\n"
        
        if event_data.get('location'):
            success_msg += f"📍 {event_data['location']}\n"
        
        success_msg += f"\n[View in Calendar]({event_link})"
        
        send_message(chat_id, success_msg)

        invite_link = generate_invite_link(event_data)
        
        # Construct and send the second message: Bold Title + Invite Link
        invite_msg = f"*{event_data['title']}*\n{invite_link}"
        send_message(chat_id, invite_msg)
        
        # Clear pending event
        del pending_events[user_id]
    
    except Exception as e:
        send_message(chat_id, f"❌ Error creating event: {str(e)}")

def handle_no(chat_id, user_id):
    """Handle /no command - cancel the event"""
    if user_id in pending_events:
        del pending_events[user_id]
        send_message(chat_id, "❌ Event cancelled.")
    else:
        send_message(chat_id, "No pending event to cancel.")

def handle_edit(chat_id, user_id):
    """Handle /edit command - guide user through editing"""
    if user_id not in pending_events:
        send_message(chat_id, "❌ No pending event found. Send me event details first!")
        return
    
    event = pending_events[user_id]
    
    msg = "✏️ *What would you like to edit?*\n\n"
    msg += f"1️⃣ Title: `{event['title']}`\n"
    msg += f"2️⃣ Time: `{event['start']} to {event['end']}`\n"
    msg += f"3️⃣ Location: `{event.get('location', 'Not set')}`\n"
    msg += f"4️⃣ Description: `{event.get('description', 'Not set')}`\n\n"
    msg += "*Reply with:*\n"
    msg += "• `title: New Title`\n"
    msg += "• `time: tomorrow 3pm to 5pm`\n"
    msg += "• `location: New Location`\n"
    msg += "• `description: New Description`\n\n"
    msg += "Or just type the new event details naturally!"
    
    send_message(chat_id, msg)
    pending_events[user_id]['awaiting_edit'] = True

def handle_recurring(chat_id, user_id):
    """Handle /recurring command"""
    if user_id not in pending_events:
        send_message(chat_id, "❌ No pending event found. Send me event details first!")
        return
    
    msg = "🔄 *How often should this repeat?*\n\n"
    msg += "*Simple patterns (uses RRULE):*\n"
    msg += "• every day\n"
    msg += "• every week\n"
    msg += "• every 2 weeks\n"
    msg += "• every month\n"
    msg += "• weekdays only\n"
    msg += "• every monday and wednesday\n"
    msg += "• first monday of every month\n"
    msg += "• last friday of every month\n\n"
    
    msg += "*Working day patterns (creates individual events):*\n"
    msg += "• every 7th working day of the month\n"
    msg += "• every 15th working day for 6 months\n"
    msg += "• every 10th working day\n\n"
    
    msg += "*With limits:*\n"
    msg += "• daily for 10 times\n"
    msg += "• weekly until December 31\n"
    msg += "• monthly for 6 months\n\n"
    
    msg += "Just describe it naturally! 💬"
    
    send_message(chat_id, msg)
    pending_events[user_id]['awaiting_recurrence'] = True

def handle_reminder(chat_id, user_id):
    """Handle /reminder command"""
    if user_id not in pending_events:
        send_message(chat_id, "❌ No pending event found. Send me event details first!")
        return
    
    msg = "⏰ *When should I remind you?*\n\n"
    msg += "Reply naturally with one or more:\n\n"
    msg += "• `10 minutes before`\n"
    msg += "• `30 minutes before`\n"
    msg += "• `1 hour before`\n"
    msg += "• `1 day before`\n"
    msg += "• `1 week before`\n\n"
    msg += "Or combine: `10 min and 1 hour before`"
    
    send_message(chat_id, msg)
    pending_events[user_id]['awaiting_reminder'] = True

def handle_colour(chat_id, user_id):
    """Handle /colour command"""
    if user_id not in pending_events:
        send_message(chat_id, "❌ No pending event found. Send me event details first!")
        return
    
    msg = "🎨 *Choose a color:*\n\n"
    msg += "🔴 red\n"
    msg += "🟠 orange\n"
    msg += "🟡 yellow\n"
    msg += "🟢 green\n"
    msg += "🔵 blue\n"
    msg += "🟣 purple\n"
    msg += "🩷 pink\n"
    msg += "⚪ gray\n\n"
    msg += "Just type the color name!"
    
    send_message(chat_id, msg)
    pending_events[user_id]['awaiting_color'] = True

def handle_update(data):
    if "message" not in data:
        return
    
    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    email = retrieve_email(user_id)
    
    if not email:
        return
    
    text = message.get("text", "").strip()
    if not text:
        return
    
    # Handle commands
    if text == "/start":
        send_message(chat_id, "Hi! Send me event details and I'll help you create a calendar event! 📅")
        return
    
    if text == "/yes":
        handle_yes(chat_id, user_id, email)
        return
    
    if text == "/no":
        handle_no(chat_id, user_id)
        return
    
    if text == "/edit":
        handle_edit(chat_id, user_id)
        return
    
    if text == "/recurring":
        handle_recurring(chat_id, user_id)
        return
    
    if text == "/reminder":
        handle_reminder(chat_id, user_id)
        return
    
    if text in ["/colour", "/color"]:
        handle_colour(chat_id, user_id)
        return
    
    # Check if user is in a specific editing state
    if user_id in pending_events:
        event = pending_events[user_id]
        
        # Handle edit mode
        if event.get('awaiting_edit'):
            field, value = parse_edit_field(text)
            
            if field == 'title':
                event['title'] = value
                event['awaiting_edit'] = False
                send_message(chat_id, f"✅ Title updated!\n\n" + format_event_preview(event))
                return
            
            elif field == 'location':
                event['location'] = value
                event['awaiting_edit'] = False
                send_message(chat_id, f"✅ Location updated!\n\n" + format_event_preview(event))
                return
            
            elif field == 'description':
                event['description'] = value
                event['awaiting_edit'] = False
                send_message(chat_id, f"✅ Description updated!\n\n" + format_event_preview(event))
                return
            
            elif field == 'time':
                start, end = extract_time_with_ai(value, event)
                if start and end:
                    event['start'] = start
                    event['end'] = end
                    event['awaiting_edit'] = False
                    send_message(chat_id, f"✅ Time updated!\n\n" + format_event_preview(event))
                else:
                    send_message(chat_id, "❌ Couldn't parse time. Try again like: `time: tomorrow 3pm to 5pm`")
                return
            
            else:
                # Try to extract entire new event
                send_message(chat_id, "🔄 Re-extracting event details...")
                event['awaiting_edit'] = False
        
        # Handle recurrence input
        elif event.get('awaiting_recurrence'):
            rec_data = parse_recurrence_natural(text, event['start'])
            if rec_data:
                event['recurrence_data'] = rec_data
                event['awaiting_recurrence'] = False
                
                # Show detailed info for working day patterns
                if rec_data.get('type') == 'working_day':
                    msg = f"✅ Recurrence set!\n\n"
                    msg += f"📅 Pattern: {rec_data['explanation']}\n\n"
                    msg += f"Calculated dates (showing first 4):\n"
                    for date_str in rec_data.get('dates', [])[:4]:
                        formatted = datetime.strptime(date_str, "%Y%m%d").strftime("%B %d, %Y")
                        msg += f"• {formatted}\n"
                    if len(rec_data.get('dates', [])) > 4:
                        msg += f"• ... and {len(rec_data['dates']) - 4} more\n"
                    msg += "\n" + format_event_preview(event)
                else:
                    msg = f"✅ Recurrence set: {rec_data['explanation']}\n\n" + format_event_preview(event)
                
                send_message(chat_id, msg)
            else:
                send_message(chat_id, "❌ Couldn't understand the recurrence pattern. Try again!")
            return
        
        # Handle reminder input
        elif event.get('awaiting_reminder'):
            reminders = parse_reminder_natural(text)
            if reminders:
                event['reminders'] = reminders
                event['awaiting_reminder'] = False
                send_message(chat_id, f"✅ Reminders set!\n\n" + format_event_preview(event))
            else:
                send_message(chat_id, "❌ Couldn't understand the reminder. Try: `10 minutes before` or `1 hour before`")
            return
        
        # Handle color input
        elif event.get('awaiting_color'):
            color = text.lower()
            if color in CALENDAR_COLORS:
                event['color'] = CALENDAR_COLORS[color]
                event['awaiting_color'] = False
                send_message(chat_id, f"✅ Color set: {color}\n\n" + format_event_preview(event))
            else:
                send_message(chat_id, f"❌ Invalid color. Choose from: {', '.join(CALENDAR_COLORS.keys())}")
            return
    
    # Extract event from text
    send_message(chat_id, "🔄 Processing your event...")
    
    try:
        # Get AI response
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a calendar event extractor. "
                        "You must reply ONLY in JSON — no explanations, no text outside the JSON. "
                        "Ensure date-times follow ISO 8601 format (YYYY-MM-DDTHH:MM:SS). "
                        "Try to extract as much information as possible. If no end time is given, either choose 1 hour or till end of day. "
                        "If morning, choose 9am, if afternoon, choose 12pm. If evening, choose 6pm. night is 9pm. "
                        "If information is missing, use null. Output must be valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": f"""
Extract a single calendar event from the following text:
{text}

Reply in **this exact JSON format** only:
{{
  "title": "string — concise event title",
  "description": "string — brief event description or null",
  "start": "string — ISO 8601 datetime (e.g., 2025-10-23T18:00:00)",
  "end": "string — ISO 8601 datetime (e.g., 2025-10-23T19:00:00)",
  "location": "string — location or null"
}}
"""
                },
            ],
        )
        
        # Parse JSON response
        ai_response = response.choices[0].message.content.strip()
        event_data = json.loads(ai_response)
        
        # Validate required fields
        if not event_data.get('title') or not event_data.get('start') or not event_data.get('end'):
            send_message(chat_id, "❌ Couldn't extract event details. Please provide title, start time, and end time.")
            return
        
        # Store pending event
        pending_events[user_id] = event_data
        
        # Show preview with options
        send_message(chat_id, format_event_preview(event_data))
    
    except json.JSONDecodeError:
        send_message(chat_id, "❌ Error: AI didn't return valid JSON. Try rephrasing your message.")
    except Exception as e:
        send_message(chat_id, f"❌ Error: {str(e)}")