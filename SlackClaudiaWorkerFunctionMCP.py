import json
import logging
import os
import boto3
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from dateutil import parser
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

class MCPClient:
    """Client for interacting with MS-365 MCP Server"""
    
    def __init__(self):
        # MCP server handles its own authentication, no token needed
        self.available_tools = [
            # Calendar tools
            'list-calendar-events', 'get-calendar-event', 'create-calendar-event',
            'update-calendar-event', 'delete-calendar-event', 'get-calendar-view',
            'list-calendars',
            
            # Mail tools  
            'list-mail-messages', 'send-mail', 'get-mail-message', 'delete-mail-message',
            'list-mail-folders', 'list-mail-folder-messages',
            
            # Tasks and To-Do
            'list-todo-tasks', 'create-todo-task', 'update-todo-task', 'delete-todo-task',
            'get-todo-task', 'list-todo-task-lists',
            
            # Contacts
            'list-outlook-contacts', 'get-outlook-contact', 'create-outlook-contact',
            'update-outlook-contact', 'delete-outlook-contact',
            
            # User profile
            'get-current-user',
            
            # OneDrive/Files
            'list-drives', 'get-drive-root-item', 'list-folder-files',
            
            # Teams (if work account)
            'list-chats', 'get-chat', 'list-chat-messages', 'send-chat-message'
        ]
        self.mcp_timeout = int(os.environ.get('MCP_TIMEOUT', '30'))
        self.max_retries = int(os.environ.get('MCP_MAX_RETRIES', '2'))
    
    def get_available_tools(self):
        """Return list of available MCP tools"""
        return self.available_tools.copy()
    
    def _create_mcp_request(self, tool_name, parameters=None):
        """Create properly formatted MCP JSON-RPC request"""
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": parameters or {}
            }
        }
    
    def _setup_mcp_environment(self):
        """Set up environment variables for MCP server execution"""
        env = os.environ.copy()
        env['NODE_PATH'] = '/opt/nodejs/node_modules'
        env['PATH'] = f"/opt/nodejs/node_modules/.bin:{env.get('PATH', '')}"
        
        # MCP server will handle its own authentication
        # No need to pass tokens - it uses device code flow
        
        return env
    
    def _execute_mcp_subprocess(self, mcp_request, retry_count=0):
        """Execute MCP server as subprocess with proper error handling"""
        env = self._setup_mcp_environment()
        
        # Use npx to run the MCP server from the layer
        cmd = ['npx', '@softeria/ms-365-mcp-server']
        
        try:
            logger.info(f"Executing MCP tool: {mcp_request['params']['name']}")
            
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                cwd='/tmp'
            )
            
            # Send request and get response with timeout
            stdout, stderr = process.communicate(
                input=json.dumps(mcp_request) + '\n',
                timeout=self.mcp_timeout
            )
            
            logger.info(f"MCP process return code: {process.returncode}")
            if stderr:
                logger.warning(f"MCP stderr: {stderr}")
            
            if process.returncode == 0 and stdout.strip():
                try:
                    response = json.loads(stdout.strip())
                    return self._process_mcp_response(response)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse MCP response: {e}")
                    logger.error(f"Raw stdout: {stdout}")
                    return None
            else:
                logger.error(f"MCP process failed with return code {process.returncode}")
                logger.error(f"stderr: {stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error(f"MCP process timeout after {self.mcp_timeout} seconds")
            try:
                process.kill()
                process.wait(timeout=5)
            except:
                pass
            return None
            
        except Exception as e:
            logger.error(f"MCP subprocess error: {e}")
            return None
    
    def _process_mcp_response(self, response):
        """Process and validate MCP server response"""
        if 'result' in response:
            logger.info("MCP tool executed successfully")
            return {
                'success': True,
                'data': response['result'],
                'error': None
            }
        elif 'error' in response:
            error_info = response['error']
            logger.error(f"MCP tool error: {error_info}")
            
            # Check if it's an authentication error
            if 'authentication' in str(error_info).lower() or 'login' in str(error_info).lower():
                return {
                    'success': False,
                    'data': None,
                    'error': 'authentication_required',
                    'error_message': 'Please authenticate with Microsoft 365 first'
                }
            else:
                return {
                    'success': False,
                    'data': None,
                    'error': 'tool_error',
                    'error_message': str(error_info)
                }
        else:
            logger.error("Invalid MCP response format")
            return None
    
    def call_mcp_tool(self, tool_name, parameters=None):
        """
        Call MCP server tool with retry logic and comprehensive error handling
        
        Returns:
            dict: {
                'success': bool,
                'data': dict|None,
                'error': str|None,
                'error_message': str|None
            }
        """
        if tool_name not in self.available_tools:
            logger.warning(f"Tool {tool_name} not in available tools list")
            return {
                'success': False,
                'data': None,
                'error': 'invalid_tool',
                'error_message': f"Tool '{tool_name}' is not available"
            }
        
        mcp_request = self._create_mcp_request(tool_name, parameters)
        
        # Retry logic for transient failures
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                logger.info(f"Retrying MCP tool call, attempt {attempt + 1}")
            
            result = self._execute_mcp_subprocess(mcp_request, attempt)
            
            if result is not None:
                return result
            
            # Don't retry on authentication errors
            if result and result.get('error') == 'authentication_required':
                return result
        
        # All retries failed
        logger.error(f"MCP tool {tool_name} failed after {self.max_retries + 1} attempts")
        return {
            'success': False,
            'data': None,
            'error': 'max_retries_exceeded',
            'error_message': f"Tool '{tool_name}' failed after multiple attempts"
        }
    
    def check_authentication(self):
        """Check if MCP server is authenticated"""
        result = self.call_mcp_tool('get-current-user')
        return result.get('success', False)

def create_enhanced_prompt(message_text, available_mcp_tools):
    """Create prompt that can choose between MCP tools and legacy functions"""
    current_date = datetime.utcnow().isoformat()
    
    return f"""
You are Claudia, an AI assistant for AWS consultants with access to Microsoft 365.

Available MCP tools: {', '.join(available_mcp_tools)}

Current date: {current_date}

Analyze this user request: "{message_text}"

Respond with a single JSON object using ONE of these formats:

1. For MCP tool usage (preferred for M365 operations):
{{"use_mcp": true, "tool": "tool_name", "parameters": {{"param1": "value1"}}}}

2. For legacy functions (fallback):
{{"use_mcp": false, "intent": "get_calendar", "parameters": {{"duration_days": 1}}}}

3. For general conversation:
{{"use_mcp": false, "intent": "general_conversation", "text": "user's message"}}

MCP Tool Examples:
- "show my calendar" → {{"use_mcp": true, "tool": "list-calendar-events", "parameters": {{}}}}
- "what's my next meeting" → {{"use_mcp": true, "tool": "get-calendar-view", "parameters": {{"startDateTime": "{current_date}", "endDateTime": "{(datetime.utcnow() + timedelta(days=1)).isoformat()}"}}}}
- "create meeting tomorrow 2pm" → {{"use_mcp": true, "tool": "create-calendar-event", "parameters": {{"subject": "Meeting", "start": {{"dateTime": "2025-01-16T14:00:00", "timeZone": "UTC"}}, "end": {{"dateTime": "2025-01-16T15:00:00", "timeZone": "UTC"}}}}}}

Choose MCP tools when possible as they provide richer functionality.

JSON:
"""

def lambda_handler(event, context):
    logger.info(f"MCP Worker received event: {json.dumps(event)}")
    body = json.loads(event.get('body', '{}'))
    slack_event = body.get('event', {})
    
    if slack_event.get('type') != 'app_mention':
        return {'status': 'Ignoring non-mention event'}
    
    user_id = slack_event.get('user')
    channel_id = slack_event.get('channel')
    message_text = slack_event.get('text', '').strip()
    
    # Initialize MCP client (handles its own authentication)
    mcp_client = MCPClient()
    
    # Check if MCP server is authenticated
    if not mcp_client.check_authentication():
        slack_client.chat_postMessage(
            channel=channel_id,
            text="🔐 I need to authenticate with Microsoft 365 first. The MCP server will handle authentication automatically on first use."
        )
        # Continue anyway - the MCP server will prompt for auth when needed
    
    try:
        # Get AI decision on how to handle the request
        prompt = create_enhanced_prompt(message_text, mcp_client.available_tools)
        
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
        
        # Parse the JSON response
        json_start = completion_text.find('{')
        json_end = completion_text.rfind('}') + 1
        action_json_str = completion_text[json_start:json_end]
        action = json.loads(action_json_str)
        
        # Route based on decision
        if action.get('use_mcp', False):
            # Use MCP tool
            tool_name = action.get('tool')
            parameters = action.get('parameters', {})
            
            slack_client.chat_postMessage(
                channel=channel_id, 
                text=f"Using enhanced M365 integration for {tool_name}..."
            )
            
            result = mcp_client.call_mcp_tool(tool_name, parameters)
            
            if result and result.get('success'):
                # Format the successful result for Slack
                data = result.get('data', {})
                
                if isinstance(data, dict) and 'value' in data:
                    # Handle Graph API response format
                    items = data['value']
                    if tool_name == 'list-calendar-events' and items:
                        event_list = [f"• *{e.get('subject', 'No title')}* - {e.get('start', {}).get('dateTime', 'No time')}" for e in items[:10]]
                        response_text = "Your upcoming events:\n" + "\n".join(event_list)
                    elif tool_name == 'get-calendar-view' and items:
                        event_list = [f"• *{e.get('subject', 'No title')}* - {e.get('start', {}).get('dateTime', 'No time')}" for e in items[:5]]
                        response_text = "Your next meetings:\n" + "\n".join(event_list)
                    elif tool_name == 'list-mail-messages' and items:
                        mail_list = [f"• *{e.get('subject', 'No subject')}* from {e.get('from', {}).get('emailAddress', {}).get('name', 'Unknown')}" for e in items[:5]]
                        response_text = "Your recent emails:\n" + "\n".join(mail_list)
                    elif tool_name == 'list-todo-tasks' and items:
                        task_list = [f"• {e.get('title', 'No title')} - {e.get('status', 'unknown')}" for e in items[:10]]
                        response_text = "Your tasks:\n" + "\n".join(task_list)
                    else:
                        response_text = f"✅ Operation completed successfully. Found {len(items)} items."
                elif tool_name == 'create-calendar-event':
                    response_text = "✅ Event created successfully!"
                elif tool_name == 'send-mail':
                    response_text = "✅ Email sent successfully!"
                elif tool_name == 'create-todo-task':
                    response_text = "✅ Task created successfully!"
                elif tool_name == 'get-current-user':
                    user_name = data.get('displayName', 'User')
                    response_text = f"✅ Connected as: {user_name}"
                else:
                    response_text = f"✅ {tool_name} completed successfully."
            elif result and result.get('error') == 'authentication_required':
                response_text = "🔐 Please authenticate with Microsoft 365 first. The MCP server needs to be logged in to access your data."
            else:
                error_msg = result.get('error_message', 'Unknown error') if result else 'No response from MCP server'
                response_text = f"❌ Sorry, I encountered an issue: {error_msg}"
                
        else:
            # Fall back to legacy functions
            intent = action.get('intent')
            parameters = action.get('parameters', {})
            
            if intent == 'get_calendar':
                # Try MCP first, then fall back to legacy if needed
                slack_client.chat_postMessage(channel=channel_id, text="Checking your calendar...")
                
                # Try using MCP calendar tools as fallback
                mcp_result = mcp_client.call_mcp_tool('list-calendar-events', {'top': 10})
                
                if mcp_result and mcp_result.get('success'):
                    data = mcp_result.get('data', {})
                    if isinstance(data, dict) and 'value' in data:
                        items = data['value']
                        if items:
                            event_list = [f"• *{e.get('subject', 'No title')}* - {e.get('start', {}).get('dateTime', 'No time')}" for e in items[:10]]
                            response_text = "Your upcoming events:\n" + "\n".join(event_list)
                        else:
                            response_text = "You have nothing on your calendar for that period."
                    else:
                        response_text = "✅ Calendar checked successfully."
                else:
                    # If MCP fails, inform user that legacy functions need separate auth
                    response_text = "❌ Unable to access calendar. The MCP server needs to be authenticated first, or you can use the legacy system by connecting via the App Home tab."
                    
            elif intent == 'general_conversation':
                # Handle general conversation with Bedrock
                response_stream = bedrock_agent_runtime.invoke_agent(
                    agentId=BEDROCK_AGENT_ID,
                    agentAliasId=BEDROCK_AGENT_ALIAS_ID,
                    sessionId=user_id,
                    inputText=message_text
                )
                response_text = ""
                for chunk in response_stream.get('completion'):
                    response_text += chunk['chunk']['bytes'].decode()
            else:
                response_text = "I'm not sure how to help with that request."
        
        slack_client.chat_postMessage(channel=channel_id, text=response_text)
        
    except Exception as e:
        logger.error(f"Error in MCP worker function: {e}")
        slack_client.chat_postMessage(
            channel=channel_id, 
            text=f"Sorry, I encountered an error: {str(e)}"
        )
    
    return {'status': 'complete'}