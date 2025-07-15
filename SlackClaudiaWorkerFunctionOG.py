import json
import logging
import os
import boto3
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# Initialize clients from environment variables
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
BEDROCK_AGENT_ID = os.environ["BEDROCK_AGENT_ID"]
BEDROCK_AGENT_ALIAS_ID = os.environ["BEDROCK_AGENT_ALIAS_ID"]
slack_client = WebClient(token=SLACK_BOT_TOKEN)
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    logger.info(f"Worker received event: {json.dumps(event)}")

    body = json.loads(event.get('body', '{}'))
    slack_event = body.get('event', {})

    if slack_event.get('type') != 'app_mention':
        return {'status': 'Ignoring non-mention event'}

    user_id = slack_event.get('user')
    channel_id = slack_event.get('channel')
    message_text = slack_event.get('text', '')

    try:
        # --- Command Router in the Worker ---
        
        # ACTION: Summarize the channel
        if 'summarize' in message_text.lower():
            slack_client.chat_postMessage(channel=channel_id, text="On it! Reading the channel history to create a summary...")
            
            history = slack_client.conversations_history(channel=channel_id, limit=100)
            messages = history.get('messages', [])
            
            conversation_text = "\n".join([f"{msg.get('user')}: {msg.get('text')}" for msg in reversed(messages)])
            
            prompt = f"Please provide a concise summary of the key points, decisions, and action items from the following Slack conversation:\n\n{conversation_text}"
            
            response_stream = bedrock_agent_runtime.invoke_agent(
                agentId=BEDROCK_AGENT_ID,
                agentAliasId=BEDROCK_AGENT_ALIAS_ID,
                sessionId=user_id,
                inputText=prompt
            )
            
            completion_text = ""
            for chunk in response_stream.get('completion'):
                completion_text += chunk['chunk']['bytes'].decode()
            
            slack_client.chat_postMessage(channel=channel_id, text=f"Here's the summary:\n\n{completion_text}")

        # DEFAULT: General conversation
        else:
            response_stream = bedrock_agent_runtime.invoke_agent(
                agentId=BEDROCK_AGENT_ID,
                agentAliasId=BEDROCK_AGENT_ALIAS_ID,
                sessionId=user_id,
                inputText=message_text
            )
            
            completion_text = ""
            for chunk in response_stream.get('completion'):
                completion_text += chunk['chunk']['bytes'].decode()
            
            slack_client.chat_postMessage(channel=channel_id, text=completion_text)

    except Exception as e:
        logger.error(f"Error in worker function: {e}")
        slack_client.chat_postMessage(channel=channel_id, text=f"Sorry, an error occurred while I was thinking: {e}")

    return {'status': 'complete'}