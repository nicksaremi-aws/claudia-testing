# Code for: OAuthHandler Lambda Function

import json
import logging
import os
import boto3
import requests # NOTE: You must add a Lambda Layer for the 'requests' library

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
# Fetch from environment variables for flexibility
SECRETS_ARN = os.environ['SECRETS_ARN']
DYNAMODB_TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']

# Initialize AWS clients
secrets_client = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(DYNAMODB_TABLE_NAME)

# --- Fetch Secrets at Cold Start ---
# This code runs once when the Lambda starts up.
try:
    secret_response = secrets_client.get_secret_value(SecretId=SECRETS_ARN)
    secrets = json.loads(secret_response['SecretString'])
    MS_CLIENT_ID = secrets['microsoft_client_id']
    MS_CLIENT_SECRET = secrets['microsoft_client_secret']
    MS_TENANT_ID = secrets['microsoft_tenant_id']
except Exception as e:
    logger.critical(f"FATAL: Could not retrieve secrets from Secrets Manager: {e}")
    raise e

def lambda_handler(event, context):
    logger.info(f"OAuthHandler received event for path: {event.get('rawPath')}")
    
    path = event.get('rawPath')
    domain_name = event.get('requestContext', {}).get('domainName')
    if not domain_name:
        return {'statusCode': 500, 'body': 'Could not determine domain name.'}
    
    # --- ROUTE 1: Start the Login Flow ---
    if path == '/connect_microsoft':
        # This part redirects the user to Microsoft's login page.
        # It's triggered by a button in Slack, passing the user's Slack ID.
        slack_user_id = event.get('queryStringParameters', {}).get('user_id')
        if not slack_user_id:
            return {'statusCode': 400, 'body': 'user_id query parameter is required.'}

        # Construct the authorization URL
        auth_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
        params = {
            'client_id': MS_CLIENT_ID,
            'response_type': 'code',
            'redirect_uri': f"https://{domain_name}/oauth_microsoft_redirect",
            'scope': 'openid profile User.Read Mail.Read Mail.Send Calendars.ReadWrite offline_access',
            'state': slack_user_id # Pass the Slack User ID for tracking
        }
        redirect_url = requests.Request('GET', auth_url, params=params).prepare().url

        # Return an HTTP 302 Redirect response to the user's browser
        return {'statusCode': 302, 'headers': {'Location': redirect_url}}

    # --- ROUTE 2: Handle the Redirect Back From Microsoft ---
    elif path == '/oauth_microsoft_redirect':
        params = event.get('queryStringParameters', {})
        auth_code = params.get('code')
        slack_user_id = params.get('state') # Retrieve the Slack User ID we sent
        
        if not auth_code or not slack_user_id:
            return {'statusCode': 400, 'body': 'Error: authorization code or state is missing.'}

        # Exchange the authorization code for an access token
        token_url = f'https://login.microsoftonline.com/common/oauth2/v2.0/token'
        token_data = {
            'client_id': MS_CLIENT_ID,
            'client_secret': MS_CLIENT_SECRET,
            'code': auth_code,
            'grant_type': 'authorization_code',
            'redirect_uri': f"https://{domain_name}/oauth_microsoft_redirect"
        }
        
        response = requests.post(token_url, data=token_data)
        token_response = response.json()
        
        if 'error' in token_response:
            logger.error(f"Error from Microsoft token endpoint: {token_response}")
            return {'statusCode': 500, 'body': f"Error getting token from Microsoft: {token_response.get('error_description')}"}

        # Store the tokens securely in our DynamoDB table
        table.put_item(
            Item={
                'user_id': slack_user_id,
                'ms_access_token': token_response['access_token'],
                'ms_refresh_token': token_response['refresh_token']
            }
        )
        
        # Return a success message to the user's browser
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'text/html'},
            'body': '<html><body><h1>Success!</h1><p>Your Microsoft account is now connected to the Claudia assistant. You can close this window and return to Slack.</p></body></html>'
        }

    return {'statusCode': 404, 'body': 'Not Found'}


''' 
ENVIRONMENT Variables:
DYNAMODB_TABLE_NAME: ClaudiaUserTable
SECRETS_ARN: arn:aws:secretsmanager:eu-west-2:780593604437:secret:claudia/microsoft-credentials-e703CI
'''