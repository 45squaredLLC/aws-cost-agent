from flask import Flask, render_template, jsonify, request, session, send_from_directory
from flask_session import Session
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import os
import json
import secrets
import threading
import uuid

from aws.account_manager import AccountManager
from aws.cost_analyzer import CostAnalyzer
from aws.resource_scanner import ResourceScanner
from aws.cache_manager import CacheManager
from ai.bedrock_client import BedrockClient
import ai.cost_tools as cost_tools

# Load environment variables
load_dotenv()

app = Flask(__name__)

class ScriptNameMiddleware:
    """Middleware to handle base path prefix for reverse proxy setups."""
    def __init__(self, app, base_path=None):
        self.app = app
        self.base_path = base_path

    def __call__(self, environ, start_response):
        # Use X-Script-Name header if present, otherwise use configured base_path
        script_name = environ.get('HTTP_X_SCRIPT_NAME', '') or self.base_path or ''

        if script_name:
            environ['SCRIPT_NAME'] = script_name
            path_info = environ.get('PATH_INFO', '')
            if path_info.startswith(script_name):
                environ['PATH_INFO'] = path_info[len(script_name):]

        return self.app(environ, start_response)


# Get base path from environment
base_path = os.getenv('APPLICATION_ROOT', '')

# Handle reverse proxy headers
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.wsgi_app = ScriptNameMiddleware(app.wsgi_app, base_path=base_path)

# Set application root for URL generation
app.config['APPLICATION_ROOT'] = base_path or '/'

# Configure session
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_COOKIE_PATH'] = base_path or '/'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
Session(app)

# Initialize AWS clients
account_manager = AccountManager()
cost_analyzer = CostAnalyzer(account_manager=account_manager)
resource_scanner = ResourceScanner(account_manager=account_manager)
cache_manager = CacheManager()
bedrock_client = BedrockClient(account_manager=account_manager)

# Set global account manager reference for cost tools
cost_tools._account_manager = account_manager

# Background analysis jobs: {job_id: {status, result, error}}
_analysis_jobs = {}


def clean_response(response):
    """
    Clean up AI response to remove or format special tags
    """
    response = str(response).strip()
    
    # Extract and format thinking tags
    import re
    
    # Find all thinking blocks
    thinking_pattern = r'<thinking>(.*?)</thinking>'
    thinking_blocks = re.findall(thinking_pattern, response, re.DOTALL)
    
    # Remove thinking tags from response
    response = re.sub(thinking_pattern, '', response, flags=re.DOTALL)
    
    # Remove JSON code blocks
    if response.startswith('```json') or response.startswith('```'):
        response = response.replace('```json', '').replace('```', '').strip()
    
    # If it looks like pure JSON, try to extract a readable message
    if response.startswith('{') and response.endswith('}'):
        try:
            data = json.loads(response)
            # Try to extract recommendations in a readable format
            if 'recommendations' in data:
                readable = "Here are my recommendations:\n\n"
                for i, rec in enumerate(data['recommendations'], 1):
                    readable += f"{i}. **{rec.get('title', 'Recommendation')}**\n"
                    readable += f"   {rec.get('description', '')}\n"
                    if rec.get('estimated_savings'):
                        readable += f"   💰 Estimated savings: {rec['estimated_savings']}\n"
                    if rec.get('cli_command'):
                        readable += f"   ```\n   {rec['cli_command']}\n   ```\n"
                    if rec.get('risk'):
                        readable += f"   Risk level: {rec['risk']}\n"
                    readable += "\n"
                return readable
        except:
            pass
    
    # Clean up extra whitespace
    response = response.strip()
    
    # Add thinking blocks as formatted notes at the end (optional - comment out to hide)
    if thinking_blocks and response:
        # Uncomment the next two lines to show thinking process
        # response += "\n\n---\n💭 *Agent reasoning: " + " → ".join(t.strip() for t in thinking_blocks) + "*"
        pass  # Currently hidden - uncomment above to show
    
    return response


@app.route('/')
def index():
    """Serve the main page"""
    return render_template('index.html')


@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    """Return discovered accounts list."""
    return jsonify({
        "success": True,
        "is_org_mode": account_manager.is_org_mode,
        "accounts": account_manager.get_accounts()
    })


def _run_analysis(job_id, is_org, account_ids):
    """Background worker for analysis."""
    try:
        if is_org:
            app.logger.info(f"[{job_id}] Org mode: analyzing {len(account_ids)} accounts...")
            cost_data = cost_analyzer.get_org_cost_summary(account_ids=account_ids)
            resources = resource_scanner.scan_accounts(account_ids)

            cost_summary_for_ai = {
                'total_cost': cost_data.get('org_total_cost', 0),
                'period_days': cost_data.get('period_days', 30),
                'top_services': [],
                'start_date': cost_data.get('start_date', ''),
                'end_date': cost_data.get('end_date', ''),
                'account_costs': cost_data.get('account_costs', {})
            }
            all_services = {}
            for acct in cost_data.get('account_costs', {}).values():
                for svc in acct.get('top_services', []):
                    all_services[svc['service']] = all_services.get(svc['service'], 0) + svc['cost']
            sorted_svc = sorted(all_services.items(), key=lambda x: x[1], reverse=True)
            cost_summary_for_ai['top_services'] = [
                {'service': s[0], 'cost': round(s[1], 2)} for s in sorted_svc[:10]
            ]
        else:
            app.logger.info(f"[{job_id}] Single-account mode: analyzing...")
            cache_key = 'single'
            cached = cache_manager.get_cached_scan(cache_key)
            if cached:
                app.logger.info(f"[{job_id}] Using cached scan results")
                resources = cached
            else:
                resources = resource_scanner.scan_resources()
                cache_manager.save_scan(cache_key, resources)

            cost_data = cost_analyzer.get_cost_summary()
            cost_summary_for_ai = cost_data

        _analysis_jobs[job_id]['status'] = 'getting_recommendations'

        app.logger.info(f"[{job_id}] Getting AI recommendations...")
        analysis_input = {"cost_data": cost_summary_for_ai, "resources": resources}
        recommendations = bedrock_client.get_initial_analysis(analysis_input)

        cache_manager.save_analysis(cost_summary_for_ai, resources, recommendations)

        _analysis_jobs[job_id]['status'] = 'complete'
        _analysis_jobs[job_id]['result'] = {
            "success": True,
            "cost_summary": cost_summary_for_ai,
            "resources": resources,
            "recommendations": recommendations,
            "analysis_input": analysis_input
        }

    except Exception as e:
        app.logger.error(f"[{job_id}] Error: {str(e)}")
        _analysis_jobs[job_id]['status'] = 'error'
        _analysis_jobs[job_id]['error'] = str(e)


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """
    Start analysis in background. Returns job_id to poll with /api/analyze/status.
    """
    try:
        data = request.get_json(silent=True) or {}
        account_ids = data.get('account_ids', None)
        is_org = account_manager.is_org_mode and account_ids and len(account_ids) > 0

        job_id = str(uuid.uuid4())[:8]
        _analysis_jobs[job_id] = {'status': 'scanning', 'result': None, 'error': None}

        thread = threading.Thread(
            target=_run_analysis, args=(job_id, is_org, account_ids), daemon=True
        )
        thread.start()

        return jsonify({"success": True, "job_id": job_id, "status": "scanning"})

    except Exception as e:
        app.logger.error(f"Error starting analysis: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/analyze/status/<job_id>', methods=['GET'])
def analyze_status(job_id):
    """Poll for analysis job status."""
    job = _analysis_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404

    if job['status'] == 'complete':
        result = job['result']
        # Store in session for chat context
        session['analysis_data'] = result.get('analysis_input')
        session['chat_history'] = []
        # Clean up job
        del _analysis_jobs[job_id]
        return jsonify(result)

    if job['status'] == 'error':
        error = job['error']
        del _analysis_jobs[job_id]
        return jsonify({"success": False, "error": error}), 500

    return jsonify({"success": True, "status": job['status']})


@app.route('/api/chat', methods=['POST'])
def chat():
    """
    Interactive chat endpoint for follow-up questions
    """
    try:
        data = request.get_json()
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return jsonify({
                "success": False,
                "error": "Message cannot be empty"
            }), 400
        
        # Get analysis data from session
        analysis_data = session.get('analysis_data')
        if not analysis_data:
            return jsonify({
                "success": False,
                "error": "No analysis data found. Please run an analysis first."
            }), 400
        
        # Get chat history
        chat_history = session.get('chat_history', [])
        
        # Add user message to history
        chat_history.append({
            "role": "user",
            "content": user_message
        })
        
        # Get AI response
        app.logger.info(f"Processing chat message: {user_message}")
        ai_response = bedrock_client.chat(user_message, analysis_data, chat_history)
        
        # Clean up response (remove any JSON formatting if present)
        ai_response = clean_response(ai_response)
        
        # Add AI response to history
        chat_history.append({
            "role": "assistant",
            "content": ai_response
        })
        
        # Update session
        session['chat_history'] = chat_history
        
        return jsonify({
            "success": True,
            "message": ai_response
        })
        
    except Exception as e:
        app.logger.error(f"Error during chat: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route('/api/history', methods=['GET'])
def list_history():
    """List saved analysis history."""
    entries = cache_manager.list_history()
    return jsonify({"success": True, "entries": entries})


@app.route('/api/history/<filename>', methods=['GET'])
def get_history(filename):
    """Get a specific history entry as markdown."""
    content = cache_manager.get_history_entry(filename)
    if content is None:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "content": content, "filename": filename})


@app.route('/api/history/<filename>/download', methods=['GET'])
def download_history(filename):
    """Download a history entry as a file."""
    # Sanitize filename
    filename = os.path.basename(filename)
    history_dir = os.path.abspath(cache_manager.history_dir)
    return send_from_directory(history_dir, filename, as_attachment=True)


@app.route('/api/reset', methods=['POST'])
def reset():
    """
    Reset the analysis and chat history
    """
    session.clear()
    return jsonify({
        "success": True,
        "message": "Session reset successfully"
    })


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for k8s probes"""
    return jsonify({
        "status": "healthy",
        "aws_configured": bool(os.getenv('AWS_ACCESS_KEY_ID')),
        "bedrock_model": os.getenv('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0'),
        "has_analysis": 'analysis_data' in session
    })


@app.route('/api/health', methods=['GET'])
def api_health():
    """Legacy health check endpoint (redirect to /health)"""
    return health()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)