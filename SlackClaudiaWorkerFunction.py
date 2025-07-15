import json
import logging
import os
import boto3
from datetime import datetime, timedelta, timezone
from dateutil import parser # This library is great for parsing dates
from slack_sdk import WebClient
import ms_graph_client
# --- AWS Client Initialization ---
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
BEDROCK_AGENT_ID = os.environ["BEDROCK_AGENT_ID"]
BEDROCK_AGENT_ALIAS_ID = os.environ["BEDROCK_AGENT_ALIAS_ID"]
slack_client = WebClient(token=SLACK_BOT_TOKEN)
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# --- Helper Function for Formatting Dates ---
def format_event_time(iso_string):
    """Parses an ISO 8601 string and formats it in BST timezone."""
    try:
        from zoneinfo import ZoneInfo
        dt_object = parser.parse(iso_string)
        # Convert to BST timezone
        bst_dt = dt_object.astimezone(ZoneInfo('Europe/London'))
        return bst_dt.strftime('%A, %B %d at %I:%M %p %Z')
    except ImportError:
        # Fallback for older Python versions
        dt_object = parser.parse(iso_string)
        return dt_object.strftime('%A, %B %d at %I:%M %p')
# --- Main Lambda Handler ---
def lambda_handler(event, context):
    logger.info(f"Worker received event: {json.dumps(event)}")
    body = json.loads(event.get('body', '{}'))
    slack_event = body.get('event', {})
    if slack_event.get('type') != 'app_mention':
        return {'status': 'Ignoring non-mention event'}
    user_id = slack_event.get('user')
    channel_id = slack_event.get('channel')
    message_text = slack_event.get('text', '').strip()
    # --- 1. Get a valid Microsoft access token for the user ---
    access_token = ms_graph_client.get_valid_access_token(user_id)
    if not access_token:
        slack_client.chat_postMessage(
            channel=channel_id,
            text="Before I can help, please connect your Microsoft 365 account via my App Home tab."
        )
        return {'status': 'complete'}
    try:
        # --- 2. Use a more advanced "Few-Shot" prompt for Bedrock ---
        # This prompt gives examples to the model, making it MUCH better at extracting details.
        prompt = f"""
        You are a helpful AI assistant for AWS Professional Services Consultants named Claudia. Your task is to analyze the user's request and respond with a single, valid JSON object that identifies the user's intent and extracts key parameters. Today's date is {datetime.utcnow().isoformat()}.
        Here are the available intents and their parameters:
        "get_calendar": See the user's upcoming events.
            "duration_days" (integer, default 1)
            "start_date" (string, ISO 8601 YYYY-MM-DD format, optional)
        "create_event": Create a new calendar event.
            "subject" (string)
            "start_time" (string, ISO 8601 format)
            "end_time" (string, ISO 8601 format, optional)
            "content" (string, optional)
        "find_and_create_event": Find free time and create an event.
            "subject" (string)
            "duration_minutes" (integer)
            "date" (string, ISO 8601 YYYY-MM-DD, optional)
            "preferred_start_time" (string, optional)
        "get_event_details": Ask about a specific event.
            "date" (string, ISO 8601 YYYY-MM-DD, optional)
            "time" (string, e.g. "12:00", optional)
            "attendee" (string, optional)
        "general_conversation": For any request that does not match the other intents.
        "text" (string, the user's original message)
        --- EXAMPLES ---
        User Request: "who is my 12 o'clock meeting with today?"
        JSON: {{"intent": "get_event_details", "parameters": {{"date": "{datetime.utcnow().strftime('%Y-%m-%d')}", "time": "12:00"}}}}
        User Request: "what's the title of my meeting with John on Tuesday?"
        JSON: {{"intent": "get_event_details", "parameters": {{"attendee": "John", "date": "<next-tuesday-date>"}}}}
        User Request: "I need half an hour today to do task X, can you find time in my calendar and schedule it?"
        JSON: {{"intent": "find_and_create_event", "parameters": {{"subject": "task X", "duration_minutes": 30, "date": "{datetime.utcnow().strftime('%Y-%m-%d')}"}}}}
        User Request: "what's on my calendar today?"
        JSON: {{"intent": "get_calendar", "parameters": {{"duration_days": 1}}}}
        User Request: "show me my schedule for the next 3 days"
        JSON: {{"intent": "get_calendar", "parameters": {{"duration_days": 3}}}}
        User Request: "what did I have yesterday?"
        JSON: {{"intent": "get_calendar", "parameters": {{"start_date": "{(datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')}", "duration_days": 1}}}}
        User Request: "create an event for tomorrow at 2pm to 3pm called Project Sync"
        JSON: {{"intent": "create_event", "parameters": {{"subject": "Project Sync", "start_time": "{(datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')}T14:00:00", "end_time": "{(datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')}T15:00:00"}}}}
        User Request: "add claudia testing to my calendar for 11am today until 11.30am"
        JSON: {{"intent": "create_event", "parameters": {{"subject": "claudia testing", "start_time": "{datetime.utcnow().strftime('%Y-%m-%d')}T11:00:00", "end_time": "{datetime.utcnow().strftime('%Y-%m-%d')}T11:30:00"}}}}
        User Request: "can you help me write an email?"
        JSON: {{"intent": "general_conversation", "parameters": {{"text": "can you help me write an email?"}}}}
        ---
        User Request: "{message_text}"
        JSON:
        """
        response_stream = bedrock_agent_runtime.invoke_agent(
            agentId=BEDROCK_AGENT_ID,
            agentAliasId=BEDROCK_AGENT_ALIAS_ID,
            sessionId=user_id,
            inputText=prompt
        )
        completion_text = ""
        for chunk in response_stream.get('completion'):
            completion_text += chunk['chunk']['bytes'].decode()
        logger.info(f"Bedrock response: {completion_text}")
        action_json_str = completion_text[completion_text.find('{'):completion_text.rfind('}')+1]
        action = json.loads(action_json_str)
        intent = action.get('intent')
        parameters = action.get('parameters', {})
        # --- 3. Execute Action Based on Intent ---
        if intent == 'get_calendar':
            slack_client.chat_postMessage(channel=channel_id, text="Let me check your calendar...")
            duration = parameters.get('duration_days', 1)
            start_date_str = parameters.get('start_date')
            if start_date_str:
                start_dt_obj = parser.parse(start_date_str)
                if start_dt_obj.tzinfo is None:
                    start_dt_obj = start_dt_obj.replace(tzinfo=timezone.utc)
            else:
                start_dt_obj = datetime.now(timezone.utc)

            end_dt_obj = start_dt_obj + timedelta(days=duration)

            start_dt_iso = start_dt_obj.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            end_dt_iso = end_dt_obj.strftime('%Y-%m-%dT%H:%M:%S.000Z')

            events = ms_graph_client.get_calendar_view(access_token, start_dt_iso, end_dt_iso)
            if not events:
                response_text = f"You have nothing on your calendar for that period."
            else:
                # Use the helper function for nice formatting
                event_list = [f"• *{e['subject']}* on {format_event_time(e['start']['dateTime'])}" for e in events]
                response_text = f"Here are your upcoming events:\n" + "\n".join(event_list)
            slack_client.chat_postMessage(channel=channel_id, text=response_text)
            
        elif intent == 'create_event':
            subject = parameters.get('subject')
            start_time = parameters.get('start_time')
            end_time = parameters.get('end_time')
            if not subject or not start_time:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    text="I can do that! Please tell me the full event details in one message, including the name, date, and time."
                )
            else:
                # If no end time is provided, default to 30 minutes after the start time
                if not end_time:
                    end_time = (parser.parse(start_time) + timedelta(minutes=30)).isoformat()
                ms_graph_client.create_calendar_event(
                    access_token,
                    subject=subject,
                    start_time=start_time,
                    end_time=end_time,
                    content=parameters.get('content', '')
                )
                slack_client.chat_postMessage(channel=channel_id, text=f":white_check_mark: Done! I've created the event: *{subject}*.")
        elif intent == 'get_event_details':
            date = parameters.get('date')
            time = parameters.get('time')
            attendee = parameters.get('attendee')
            events = ms_graph_client.get_calendar_events_for_day(access_token, date or datetime.utcnow().strftime('%Y-%m-%d'))
            match = ms_graph_client.find_event(events, time=time, attendee=attendee)
            if match:
                response_text = f"Event: *{match['subject']}* at {format_event_time(match['start']['dateTime'])}"
            else:
                response_text = "Sorry, I couldn't find a matching event."
            slack_client.chat_postMessage(channel=channel_id, text=response_text)
        elif intent == 'find_and_create_event':
            subject = parameters.get('subject')
            duration = parameters.get('duration_minutes', 30)
            date = parameters.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
            preferred_start_time = parameters.get('preferred_start_time')
            slot = ms_graph_client.find_free_time_slot(access_token, date, duration, preferred_start_time)
            if slot:
                ms_graph_client.create_calendar_event(
                    access_token,
                    subject=subject,
                    start_time=slot['start'],
                    end_time=slot['end'],
                    content=f"Auto-scheduled by Claudia"
                )
                response_text = f":white_check_mark: Scheduled '{subject}' from {format_event_time(slot['start'])} to {format_event_time(slot['end'])}."
            else:
                response_text = "Sorry, I couldn't find a free slot for that duration."
            slack_client.chat_postMessage(channel=channel_id, text=response_text)
        else: # Default to general conversation
            # For simple chat, just pass the original text back to the agent
            response_stream = bedrock_agent_runtime.invoke_agent(
                agentId=BEDROCK_AGENT_ID,
                agentAliasId=BEDROCK_AGENT_ALIAS_ID,
                sessionId=user_id,
                inputText=message_text
            )
            final_response = ""
            for chunk in response_stream.get('completion'):
                final_response += chunk['chunk']['bytes'].decode()
            logger.info(f"Bedrock response (general conversation): {final_response}") # Log the final response
            slack_client.chat_postMessage(channel=channel_id, text=final_response)
    except Exception as e:
        logger.error(f"Error in worker function: {e}")
        slack_client.chat_postMessage(channel=channel_id, text=f"Sorry, an error occurred while I was thinking :thinking_face:: {e}")
    return {'status': 'complete'}

''' 
Environment Variables
BEDROCK_AGENT_ALIAS_ID:QP8GWFDWQ4
BEDROCK_AGENT_ID:CTBWT3AQJ6
SECRETS_ARN:arn:aws:secretsmanager:eu-west-2:780593604437:secret:claudia/microsoft-credentials-e703CI
SLACK_BOT_TOKEN:xoxb-9111784689365-9127528456417-mD9y0SjMME6VZOdH1fIvvAhQ
TOKENS_TABLE:ClaudiaUserTable
'''