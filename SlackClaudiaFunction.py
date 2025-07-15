# Updated Code for: SlackClaudiaFunction (The Dispatcher)

import json
import logging
import boto3
import os
from slack_sdk import WebClient # <-- ADD THIS IMPORT

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- INITIALIZE CLIENTS ---
# This Lambda now needs both the Lambda client and the Slack client
lambda_client = boto3.client('lambda')
slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"]) # <-- ADD THIS
WORKER_LAMBDA_NAME = os.environ["WORKER_LAMBDA_NAME"]

def lambda_handler(event, context):
    logger.info(f"Dispatcher received event: {json.dumps(event)}")
    body = json.loads(event.get('body', '{}'))
    event_type = body.get('type')
    
    # --- ROUTE 1: Slack URL Verification ---
    if event_type == 'url_verification':
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/plain'},
            'body': body.get('challenge')
        }

    # --- ROUTE 2: Event Callback (Handles different event types) ---
    elif event_type == 'event_callback':
        slack_event = body.get('event', {})
        slack_event_type = slack_event.get('type')

        # --- Sub-Route: User @mentions the bot ---
        if slack_event_type == 'app_mention':
            try:
                # This part stays the same: invoke the worker asynchronously
                lambda_client.invoke(
                    FunctionName=WORKER_LAMBDA_NAME,
                    InvocationType='Event',
                    Payload=json.dumps(event)
                )
                logger.info(f"Successfully invoked worker for app_mention: {WORKER_LAMBDA_NAME}")
            except Exception as e:
                logger.error(f"Failed to invoke worker lambda: {e}")

        # --- Sub-Route: User opens the App Home tab ---
        # vvv ADD THIS ENTIRE BLOCK vvv
        elif slack_event_type == 'app_home_opened':
            try:
                user_id = slack_event.get('user')
                domain_name = event.get('requestContext', {}).get('domainName')
                
                # This is the URL for the button, pointing to your OAuthHandler Lambda
                connect_url = f"https://{domain_name}/connect_microsoft?user_id={user_id}"

                # Define the UI for the App Home using Slack's Block Kit
                app_home_view = {
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*Welcome to your AI Assistant, Claudia!* :wave:\n\nTo unlock personalised features like calendar management and email summarisation, you'll need to connect your Microsoft 365 account."
                            }
                        },
                        {
                            "type": "divider"
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Connect to Microsoft 365",
                                        "emoji": True
                                    },
                                    "style": "primary",
                                    "url": connect_url,
                                    "action_id": "connect_m365_button"
                                }
                            ]
                        }
                    ]
                }
                
                # Publish the view to the user's App Home tab
                slack_client.views_publish(user_id=user_id, view=app_home_view)
                logger.info(f"Published App Home view for user {user_id}")
            except Exception as e:
                logger.error(f"Error publishing App Home view: {e}")

    # Acknowledge all events to Slack immediately
    return {'statusCode': 200, 'body': 'Acknowledged'}

'''
Environment Variables
BEDROCK_AGENT_ALIAS_ID:QP8GWFDWQ4
BEDROCK_AGENT_ID:CTBWT3AQJ6
SLACK_BOT_TOKEN:xoxb-9111784689365-9127528456417-mD9y0SjMME6VZOdH1fIvvAhQ
WORKER_LAMBDA_NAME:SlackClaudiaWorkerFunction
'''