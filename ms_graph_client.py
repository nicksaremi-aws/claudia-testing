# ms_graph_client.py
import logging
import requests
import boto3
import os
import json
from dateutil import parser
from datetime import datetime, timedelta, timezone
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# --- AWS & App Configuration ---
# These must be set as environment variables in the Lambda function
DYNAMODB_TABLE_NAME = os.environ['TOKENS_TABLE']
SECRETS_ARN = os.environ['SECRETS_ARN']
# --- Initialize AWS Clients ---
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMODB_TABLE_NAME)
secrets_client = boto3.client('secretsmanager')
# --- Fetch Secrets (executed at cold start) ---
# This block runs once per Lambda container instance, making it efficient.
try:
    secret_response = secrets_client.get_secret_value(SecretId=SECRETS_ARN)
    secrets = json.loads(secret_response['SecretString'])
    MS_CLIENT_ID = secrets['microsoft_client_id']
    MS_CLIENT_SECRET = secrets['microsoft_client_secret']
    MS_TENANT_ID = secrets['microsoft_tenant_id']
except Exception as e:
    logger.critical(f"FATAL: Could not retrieve secrets for MS Graph Client: {e}")
    # This will cause the Lambda to fail if it can't get credentials, which is a good safety measure.
    raise e
# --- Core Token Management Logic ---
def get_tokens_for_user(user_id):
    """Retrieves access and refresh tokens for a user from DynamoDB."""
    try:
        response = table.get_item(Key={'user_id': user_id})
        return response.get('Item')
    except Exception as e:
        logger.error(f"Could not get tokens for user {user_id} from DynamoDB: {e}")
        return None
def refresh_and_save_tokens(user_id, refresh_token):
    """Uses a refresh token to get a new access token and updates DynamoDB."""
    logger.info(f"Refreshing token for user {user_id}")
    token_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/token' ####### changed {MS_TENANT_ID} to common
    token_data = {
        'client_id': MS_CLIENT_ID,
        'client_secret': MS_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'scope': 'openid profile User.Read Mail.Read Mail.Send Calendars.ReadWrite offline_access'
    }
    response = requests.post(token_url, data=token_data)
    token_response = response.json()
    if 'error' in token_response:
        logger.error(f"Error refreshing token: {token_response.get('error_description')}")
        # If the refresh token is expired or invalid, remove the bad entry from the DB.
        # The user will simply have to reconnect.
        if "invalid_grant" in token_response.get('error_description', ''):
            table.delete_item(Key={'user_id': user_id})
        return None
    # Update the database with the new tokens and expiration time
    new_access_token = token_response['access_token']
    new_refresh_token = token_response.get('refresh_token', refresh_token)
    expires_in = token_response.get('expires_in', 3600)
    # Set expiration to 5 minutes before it actually expires for a safety buffer
    expires_at = (datetime.utcnow() + timedelta(seconds=expires_in - 300)).isoformat()
    table.update_item(
        Key={'user_id': user_id},
        UpdateExpression="SET ms_access_token = :a, ms_refresh_token = :r, ms_token_expires_at = :e",
        ExpressionAttributeValues={
            ':a': new_access_token,
            ':r': new_refresh_token,
            ':e': expires_at
        }
    )
    logger.info(f"Successfully refreshed and saved tokens for user {user_id}")
    return new_access_token
def get_valid_access_token(user_id):
    """
    This is the main function you'll call from your Lambda.
    It gets a user's token and automatically refreshes it if it's about to expire.
    """
    user_data = get_tokens_for_user(user_id)
    if not user_data:
        return None # User has not connected their account
    # Check for expiration
    expires_at_str = user_data.get('ms_token_expires_at')
    # If the expiration time is not set or is in the past, refresh
    if not expires_at_str or datetime.fromisoformat(expires_at_str) < datetime.utcnow():
        logger.info("Token expired or nearing expiration, refreshing...")
        return refresh_and_save_tokens(user_id, user_data['ms_refresh_token'])
    else:
        logger.info("Token is still valid.")
        return user_data['ms_access_token']
# --- Microsoft Graph API Functions ---
GRAPH_API_ENDPOINT = 'https://graph.microsoft.com/v1.0'
def get_calendar_view(access_token, start_datetime, end_datetime):
    """Gets calendar events between two datetimes."""
    headers = {'Authorization': f'Bearer {access_token}', 'Prefer': 'outlook.timezone="UTC"'}
    params = {
        'startDateTime': start_datetime,
        'endDateTime': end_datetime,
        '$select': 'subject,start,end',
        '$orderby': 'start/dateTime'
    }
    response = requests.get(f"{GRAPH_API_ENDPOINT}/me/calendarview", headers=headers, params=params)
    response.raise_for_status() # Raise an exception for HTTP errors (like 401/403)
    return response.json().get('value', [])
def create_calendar_event(access_token, subject, start_time, end_time, content):
    """Creates a new event in the user's calendar."""
    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json'
    }
    event_data = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": content
        },
        "start": {
            "dateTime": start_time,
            "timeZone": "UTC"
        },
        "end": {
            "dateTime": end_time,
            "timeZone": "UTC"
        }
    }
    response = requests.post(f"{GRAPH_API_ENDPOINT}/me/events", headers=headers, json=event_data)
    response.raise_for_status()
    return response.json()
# You can add more functions here for reading/sending email as you build them.
def get_calendar_events_for_day(access_token, date):
    """Get all events for a specific day (UTC)."""
    start = f"{date}T00:00:00Z"
    end = f"{date}T23:59:59Z"
    return get_calendar_view(access_token, start, end)

def find_event(events, time=None, attendee=None):
    """Find an event by time or attendee name."""
    for event in events:
        event_time = parser.parse(event['start']['dateTime']).strftime('%H:%M')
        if time and time in event_time:
            return event
        if attendee and attendee.lower() in event.get('subject', '').lower():
            return event
    return None

def find_free_time_slot(access_token, date, duration_minutes, preferred_start_time=None):
    """Find a free slot of given duration on a given date. Returns dict with 'start' and 'end' ISO strings."""
    events = get_calendar_events_for_day(access_token, date)
    busy_times = []
    
    for event in events:
        start_dt = parser.parse(event['start']['dateTime'])
        end_dt = parser.parse(event['end']['dateTime'])
        # Ensure timezone-aware
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        busy_times.append((start_dt, end_dt))
    
    busy_times.sort()
    
    # Create timezone-aware datetime objects
    day_start = datetime.fromisoformat(f"{date}T08:00:00").replace(tzinfo=timezone.utc)
    day_end = datetime.fromisoformat(f"{date}T18:00:00").replace(tzinfo=timezone.utc)
    
    if preferred_start_time:
        day_start = datetime.fromisoformat(f"{date}T{preferred_start_time}:00").replace(tzinfo=timezone.utc)
    
    slot_start = day_start
    while slot_start + timedelta(minutes=duration_minutes) <= day_end:
        slot_end = slot_start + timedelta(minutes=duration_minutes)
        
        # Check for overlaps
        overlap = any(bs < slot_end and be > slot_start for bs, be in busy_times)
        
        if not overlap:
            return {
                'start': slot_start.isoformat(),
                'end': slot_end.isoformat()
            }
        
        slot_start += timedelta(minutes=15)
    
    return None

''' 
Environment Variables
BEDROCK_AGENT_ALIAS_ID:QP8GWFDWQ4
BEDROCK_AGENT_ID:CTBWT3AQJ6
SECRETS_ARN:arn:aws:secretsmanager:eu-west-2:780593604437:secret:claudia/microsoft-credentials-e703CI
SLACK_BOT_TOKEN:xoxb-9111784689365-9127528456417-mD9y0SjMME6VZOdH1fIvvAhQ
TOKENS_TABLE:ClaudiaUserTable
'''