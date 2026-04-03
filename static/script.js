document.addEventListener('DOMContentLoaded', function() {
    const analyzeBtn = document.getElementById('analyzeBtn');
    const loadingSpinner = document.getElementById('loadingSpinner');
    const results = document.getElementById('results');
    const error = document.getElementById('error');
    const statusBar = document.getElementById('statusBar');
    const chatInput = document.getElementById('chatInput');
    const sendBtn = document.getElementById('sendBtn');
    const chatMessages = document.getElementById('chatMessages');

    // Base path for reverse proxy support
    const BASE_PATH = window.BASE_PATH || '';

    // State
    let isOrgMode = false;
    let currentAnalysisData = null;
    let demoMode = false;

    // Safe JSON fetch helper — handles HTML error pages from proxies
    async function fetchJSON(url, options = {}) {
        const response = await fetch(url, { credentials: 'include', ...options });
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            if (response.status === 504) {
                throw new Error('Request timed out. The scan is still running — try again in a minute, or select fewer accounts/regions.');
            }
            if (response.status === 502) {
                throw new Error('The application is restarting. Please wait a moment and try again.');
            }
            throw new Error(`Server returned an error (HTTP ${response.status}). Please try again.`);
        }
        return response.json();
    }

    // Demo mode obfuscation — stable mapping so same input = same masked output
    const _demoMap = {};
    let _demoCounter = 0;
    function _stableMask(value, prefix) {
        if (!_demoMap[value]) {
            _demoCounter++;
            _demoMap[value] = `${prefix}-${_demoCounter}`;
        }
        return _demoMap[value];
    }

    function obfuscate(text) {
        if (!demoMode || !text) return text;
        text = String(text);
        // AWS account IDs (12 digits)
        text = text.replace(/\b\d{12}\b/g, '************');
        // Instance/volume/snapshot/AMI/NAT/EIP/VPC IDs
        text = text.replace(/\b(i|vol|snap|ami|nat|eni|eipalloc|igw|vpc|subnet|sg|rtb|pcx|acl)-[0-9a-f]{8,17}\b/g, (m) => m.split('-')[0] + '-********');
        // ARNs (match both raw 12-digit account IDs and already-masked ones)
        text = text.replace(/arn:aws:[^:\s]+:[^:\s]*:(\d{12}|\*{12}):[^\s,")]+/g, 'arn:aws:***:***:************:***');
        // IP addresses
        text = text.replace(/\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b/g, '*.*.*.*');
        // S3 bucket names (full URL format)
        text = text.replace(/\b([a-z0-9][a-z0-9.-]{2,62})\.(s3|s3-[a-z0-9-]+)\.amazonaws\.com\b/g, '***-bucket.s3.amazonaws.com');
        // Email addresses
        text = text.replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, '***@***.***');
        // CloudWatch log group paths
        text = text.replace(/\/aws\/[a-zA-Z0-9/_-]+/g, '/aws/***/***');
        return text;
    }

    // Field-level obfuscation: masks known-sensitive field values with stable aliases
    const SENSITIVE_FIELDS = new Set([
        'id', 'name', 'arn', 'public_ip', 'allocation_id', 'vpc_id', 'subnet_id',
        'volume_id', 'source', 'email', 'domain', 'cluster', 'service',
        'account_name'
    ]);

    function maskField(key, value) {
        if (!demoMode || typeof value !== 'string') return value;
        // First apply regex patterns
        let masked = obfuscate(value);
        // If regex didn't change it and it's a sensitive field, use stable alias
        if (masked === value && SENSITIVE_FIELDS.has(key)) {
            // Don't mask generic AWS service names
            if (key === 'name' && /^(Amazon|AWS|Elastic)\s/.test(value)) return value;
            return _stableMask(value, key.replace(/_/g, '-'));
        }
        return masked;
    }

    // Check health and load accounts on page load
    checkHealth();
    loadAccounts();
    loadHistory();

    // Event listeners
    analyzeBtn.addEventListener('click', analyzeAccount);
    sendBtn.addEventListener('click', sendChatMessage);

    document.getElementById('selectAllBtn').addEventListener('click', function() {
        document.querySelectorAll('.account-checkbox').forEach(cb => cb.checked = true);
    });
    document.getElementById('selectNoneBtn').addEventListener('click', function() {
        document.querySelectorAll('.account-checkbox').forEach(cb => cb.checked = false);
    });
    document.getElementById('historyModalClose').addEventListener('click', function() {
        document.getElementById('historyModal').style.display = 'none';
    });
    document.getElementById('demoModeToggle').addEventListener('change', function() {
        demoMode = this.checked;
        // Re-render everything
        loadAccounts();
        if (currentAnalysisData) {
            displayResults(currentAnalysisData);
        }
        loadHistory();
    });

    // Handle Enter key (send) vs Shift+Enter (new line)
    chatInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!sendBtn.disabled) {
                sendChatMessage();
            }
        }
    });

    // Auto-resize textarea as user types
    chatInput.addEventListener('input', function() {
        autoResizeTextarea(this);
    });

    function autoResizeTextarea(textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
    }

    // Suggestion buttons
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('suggestion-btn')) {
            const suggestion = e.target.getAttribute('data-suggestion');
            chatInput.value = suggestion;
            sendChatMessage();
        }
    });

    async function checkHealth() {
        try {
            const data = await fetchJSON(BASE_PATH + '/api/health');
            if (data.status === 'healthy' && data.aws_configured) {
                updateStatus('✅ Connected to AWS', 'success');
            } else {
                updateStatus('⚠️ AWS credentials not configured', 'warning');
            }
        } catch (err) {
            updateStatus('❌ Service not available', 'error');
        }
    }

    async function loadAccounts() {
        try {
            const data = await fetchJSON(BASE_PATH + '/api/accounts');

            if (!data.success) return;

            isOrgMode = data.is_org_mode;

            if (isOrgMode && data.accounts.length > 1) {
                const selector = document.getElementById('accountSelector');
                const accountList = document.getElementById('accountList');

                let html = '';
                data.accounts.forEach(acct => {
                    html += `
                        <label class="account-item">
                            <input type="checkbox" class="account-checkbox" value="${acct.id}" checked>
                            <span class="account-name">${escapeHtml(maskField('account_name', acct.name))}</span>
                            <span class="account-id">${maskField('id', acct.id)}</span>
                        </label>
                    `;
                });
                accountList.innerHTML = html;
                selector.style.display = 'block';
                analyzeBtn.textContent = '🔍 Analyze Selected Accounts';
            }
        } catch (err) {
            console.error('Error loading accounts:', err);
        }
    }

    async function analyzeAccount() {
        // Hide previous results/errors
        results.style.display = 'none';
        error.style.display = 'none';

        // Clear chat
        chatMessages.innerHTML = '<div class="chat-empty"><div class="chat-empty-icon">💬</div><p>Analysis complete! Ask me anything about your AWS resources.</p></div>';

        // Show loading
        analyzeBtn.disabled = true;
        loadingSpinner.style.display = 'block';
        updateStatus('🔄 Analyzing...', 'info');

        // Build request body
        let body = {};
        if (isOrgMode) {
            const selectedAccounts = Array.from(document.querySelectorAll('.account-checkbox:checked'))
                .map(cb => cb.value);
            if (selectedAccounts.length === 0) {
                showError('Please select at least one account to analyze.');
                analyzeBtn.disabled = false;
                loadingSpinner.style.display = 'none';
                return;
            }
            body.account_ids = selectedAccounts;
        }

        try {
            // Start the analysis job
            const startData = await fetchJSON(BASE_PATH + '/api/analyze', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!startData.success) {
                showError(startData.error || 'Failed to start analysis');
                analyzeBtn.disabled = false;
                loadingSpinner.style.display = 'none';
                return;
            }

            // Poll for results
            const jobId = startData.job_id;
            const statusMessages = {
                scanning: '🔄 Scanning resources across all regions...',
                getting_recommendations: '🤖 Getting AI recommendations...'
            };

            while (true) {
                await new Promise(r => setTimeout(r, 3000));
                const pollData = await fetchJSON(BASE_PATH + '/api/analyze/status/' + jobId);

                if (pollData.status && !pollData.cost_summary) {
                    // Still in progress
                    updateStatus(statusMessages[pollData.status] || '🔄 Analyzing...', 'info');
                    continue;
                }

                // Complete or error
                if (pollData.success) {
                    currentAnalysisData = pollData;
                    displayResults(pollData);
                    updateStatus('✅ Analysis complete', 'success');
                    chatInput.disabled = false;
                    sendBtn.disabled = false;
                    chatInput.focus();
                    loadHistory();
                } else {
                    showError(pollData.error || 'Analysis failed');
                }
                break;
            }

        } catch (err) {
            showError(err.message);
        } finally {
            analyzeBtn.disabled = false;
            loadingSpinner.style.display = 'none';
        }
    }

    async function sendChatMessage() {
        const message = chatInput.value.trim();
        if (!message) return;

        // Disable input while processing
        chatInput.disabled = true;
        sendBtn.disabled = true;

        // Add user message to chat
        addChatMessage('user', message);

        // Clear input and reset height
        chatInput.value = '';
        chatInput.style.height = 'auto';

        // Show typing indicator
        const typingId = addTypingIndicator();

        try {
            const data = await fetchJSON(BASE_PATH + '/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: message })
            });

            // Remove typing indicator
            removeTypingIndicator(typingId);

            if (data.success) {
                addChatMessage('assistant', data.message);
            } else {
                addChatMessage('assistant', `Error: ${data.error}`);
            }

        } catch (err) {
            removeTypingIndicator(typingId);
            addChatMessage('assistant', `Error: ${err.message}`);
        } finally {
            // Re-enable input
            chatInput.disabled = false;
            sendBtn.disabled = false;
            chatInput.focus();
        }
    }

    function addChatMessage(role, content) {
        // Remove empty state
        const emptyState = chatMessages.querySelector('.chat-empty');
        if (emptyState) {
            emptyState.remove();
        }

        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${role}`;

        const avatar = document.createElement('div');
        avatar.className = `message-avatar ${role}`;
        avatar.textContent = role === 'user' ? '👤' : '🤖';

        const messageContent = document.createElement('div');
        messageContent.className = `message-content ${role}`;

        // Format content with basic markdown support + demo mode obfuscation
        const displayContent = role === 'assistant' ? obfuscate(content) : content;
        const formattedContent = formatMessage(displayContent);
        messageContent.innerHTML = formattedContent;

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(messageContent);

        chatMessages.appendChild(messageDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function formatMessage(text) {
        // Escape HTML first
        text = escapeHtml(text);

        // Remove any remaining XML-like thinking tags
        text = text.replace(/&lt;thinking&gt;.*?&lt;\/thinking&gt;/gi, '');
        text = text.replace(/<thinking>.*?<\/thinking>/gi, '');

        // Remove any JSON wrapper if present (fallback)
        text = text.replace(/^```json\s*/i, '').replace(/\s*```$/, '');

        // Format code blocks (triple backticks)
        text = text.replace(/```(\w+)?\n([\s\S]*?)```/g, function(match, lang, code) {
            return '<pre><code class="code-block">' + code.trim() + '</code></pre>';
        });

        // Format inline code (single backticks)
        text = text.replace(/`([^`]+)`/g, '<code>$1</code>');

        // Format bold text
        text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

        // Format bullet points
        text = text.replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>');
        text = text.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');

        // Format numbered lists
        text = text.replace(/^\s*(\d+)\.\s+(.+)$/gm, '<li>$2</li>');
        text = text.replace(/(<li>.*<\/li>)/s, function(match) {
            if (!match.includes('<ul>')) {
                return '<ol>' + match + '</ol>';
            }
            return match;
        });

        // Convert line breaks to paragraphs
        text = text.split('\n\n').map(para => {
            if (!para.trim()) return '';
            if (para.includes('<ul>') || para.includes('<ol>') || para.includes('<pre>')) {
                return para;
            }
            return '<p>' + para.replace(/\n/g, '<br>') + '</p>';
        }).join('');

        return text;
    }

    function addTypingIndicator() {
        const typingDiv = document.createElement('div');
        typingDiv.className = 'chat-message assistant';
        typingDiv.id = 'typing-indicator';

        const avatar = document.createElement('div');
        avatar.className = 'message-avatar assistant';
        avatar.textContent = '🤖';

        const typingContent = document.createElement('div');
        typingContent.className = 'message-content assistant';
        typingContent.innerHTML = '<div class="chat-typing"><span></span><span></span><span></span></div>';

        typingDiv.appendChild(avatar);
        typingDiv.appendChild(typingContent);

        chatMessages.appendChild(typingDiv);
        chatMessages.scrollTop = chatMessages.scrollHeight;

        return 'typing-indicator';
    }

    function removeTypingIndicator(id) {
        const indicator = document.getElementById(id);
        if (indicator) {
            indicator.remove();
        }
    }

    function displayResults(data) {
        const isOrg = data.resources.mode === 'organization';

        // Org summary
        if (isOrg) {
            displayOrgSummary(data);
            displayAccountTabs(data);
        } else {
            document.getElementById('orgSummarySection').style.display = 'none';
            document.getElementById('accountTabs').style.display = 'none';
        }

        // Display cost summary
        displayCostSummary(data, null);

        // Display resource summary
        displayResourceSummary(data, null);

        // Display orphaned resources
        displayOrphanedResources(data, null);

        // Display Bedrock usage (optional — only if data exists)
        displayBedrockUsage(data, null);

        // Display recommendations
        const recommendations = document.getElementById('recommendations');
        const recs = data.recommendations.recommendations || [];

        if (recs.length > 0) {
            recommendations.innerHTML = recs.map(rec => `
                <div class="recommendation">
                    <h3>${escapeHtml(rec.title)}</h3>
                    <p>${escapeHtml(obfuscate(rec.description))}</p>
                    ${rec.estimated_savings ? `<span class="savings">💰 ${escapeHtml(rec.estimated_savings)}</span>` : ''}
                    ${rec.risk ? `<span class="risk-badge risk-${rec.risk.toLowerCase()}">${escapeHtml(rec.risk)} Risk</span>` : ''}
                    ${rec.cli_command ? `<div class="cli-command">${escapeHtml(obfuscate(rec.cli_command))}</div>` : ''}
                </div>
            `).join('');
        } else {
            recommendations.innerHTML = '<p>No specific recommendations at this time. Your AWS setup looks optimized!</p>';
        }

        // Show results
        results.style.display = 'block';
    }

    function displayOrgSummary(data) {
        const section = document.getElementById('orgSummarySection');
        const summary = document.getElementById('orgSummary');
        section.style.display = 'block';

        const accounts = data.resources.accounts || {};
        const accountCount = Object.keys(accounts).length;
        const orgTotals = data.resources.org_totals || {};
        const totalCost = data.cost_summary.org_total_cost || data.cost_summary.total_cost || 0;

        // Calculate total waste across all accounts
        let totalWaste = 0;
        for (const acctData of Object.values(accounts)) {
            const orph = acctData.orphaned_resources || {};
            Object.values(orph).forEach(items => {
                (items || []).forEach(item => totalWaste += item.estimated_monthly_cost || 0);
            });
        }

        summary.innerHTML = `
            <div class="resource-grid">
                <div class="resource-box">
                    <div class="resource-count" style="color: #667eea;">$${totalCost}</div>
                    <div class="resource-label">Total Org Cost (${data.cost_summary.period_days || 30} days)</div>
                </div>
                <div class="resource-box">
                    <div class="resource-count">${accountCount}</div>
                    <div class="resource-label">Accounts Analyzed</div>
                </div>
                <div class="resource-box">
                    <div class="resource-count" style="color: #dc3545;">$${totalWaste.toFixed(2)}</div>
                    <div class="resource-label">Total Waste/month</div>
                </div>
            </div>
        `;
    }

    function displayAccountTabs(data) {
        const tabsContainer = document.getElementById('accountTabs');
        tabsContainer.style.display = 'flex';

        const accounts = data.resources.accounts || {};
        let html = '<button class="account-tab active" data-account="all">All Accounts</button>';
        for (const [acctId, acctData] of Object.entries(accounts)) {
            const name = acctData.account_name || acctId;
            html += `<button class="account-tab" data-account="${acctId}">${escapeHtml(maskField('account_name', name))}</button>`;
        }
        tabsContainer.innerHTML = html;

        tabsContainer.addEventListener('click', function(e) {
            if (!e.target.classList.contains('account-tab')) return;
            // Update active state
            tabsContainer.querySelectorAll('.account-tab').forEach(t => t.classList.remove('active'));
            e.target.classList.add('active');

            const accountId = e.target.getAttribute('data-account');
            if (accountId === 'all') {
                displayCostSummary(currentAnalysisData, null);
                displayResourceSummary(currentAnalysisData, null);
                displayOrphanedResources(currentAnalysisData, null);
                displayBedrockUsage(currentAnalysisData, null);
            } else {
                displayCostSummary(currentAnalysisData, accountId);
                displayResourceSummary(currentAnalysisData, accountId);
                displayOrphanedResources(currentAnalysisData, accountId);
                displayBedrockUsage(currentAnalysisData, accountId);
            }
        });
    }

    function displayCostSummary(data, accountId) {
        const costSummary = document.getElementById('costSummary');
        const isOrg = data.resources.mode === 'organization';

        let totalCost, topServices, periodDays;

        if (isOrg && accountId) {
            // Show specific account
            const acctCost = (data.cost_summary.account_costs || {})[accountId] || {};
            totalCost = acctCost.total_cost || 0;
            topServices = acctCost.top_services || [];
            periodDays = data.cost_summary.period_days || 30;
        } else {
            totalCost = data.cost_summary.org_total_cost || data.cost_summary.total_cost || 0;
            topServices = data.cost_summary.top_services || [];
            periodDays = data.cost_summary.period_days || 30;
        }

        costSummary.innerHTML = `
            <div class="cost-stat">
                <h3>$${totalCost}</h3>
                <p>Total cost (last ${periodDays} days)</p>
            </div>
            <h4>Top Services:</h4>
            <ul class="service-list">
                ${topServices.map(service => `
                    <li class="service-item">
                        <span class="service-name">${escapeHtml(service.service)}</span>
                        <span class="service-cost">$${service.cost}</span>
                    </li>
                `).join('')}
            </ul>
        `;
    }

    function displayResourceSummary(data, accountId) {
        const resourceSummary = document.getElementById('resourceSummary');
        const isOrg = data.resources.mode === 'organization';

        let totals, byRegion, regionsScanned;

        if (isOrg && accountId) {
            const acctData = (data.resources.accounts || {})[accountId] || {};
            totals = acctData.totals || {};
            byRegion = acctData.by_region || {};
            regionsScanned = acctData.regions_scanned || [];
        } else if (isOrg) {
            totals = data.resources.org_totals || {};
            byRegion = null;
            regionsScanned = [];
        } else {
            totals = data.resources.totals || {};
            byRegion = data.resources.by_region || {};
            regionsScanned = data.resources.regions_scanned || [];
        }

        let resourceHTML = `
            <div class="resource-grid">
                <div class="resource-box">
                    <div class="resource-count">${totals.ec2_instances || 0}</div>
                    <div class="resource-label">EC2 Instances</div>
                </div>
                <div class="resource-box">
                    <div class="resource-count">${totals.rds_instances || 0}</div>
                    <div class="resource-label">RDS Instances</div>
                </div>
                <div class="resource-box">
                    <div class="resource-count">${totals.s3_buckets || 0}</div>
                    <div class="resource-label">S3 Buckets</div>
                </div>
            </div>
        `;

        // Show per-region breakdown for single account view
        if (byRegion && regionsScanned && regionsScanned.length > 1) {
            const regionsWithResources = regionsScanned.filter(region => {
                const regionData = byRegion[region];
                if (!regionData || regionData.error) return false;
                const total = (regionData.ec2_instances || []).length +
                             (regionData.rds_instances || []).length +
                             (regionData.unattached_volumes || []).length +
                             (regionData.orphaned_snapshots || []).length +
                             (regionData.unassociated_eips || []).length +
                             (regionData.stale_amis || []).length;
                return total > 0;
            });

            if (regionsWithResources.length > 0) {
                resourceHTML += '<h4 style="margin-top: 20px;">By Region:</h4>';
                for (const region of regionsWithResources) {
                    const regionData = byRegion[region];
                    const ec2Count = (regionData.ec2_instances || []).length;
                    const rdsCount = (regionData.rds_instances || []).length;
                    resourceHTML += `
                        <div style="background: white; padding: 12px; margin: 8px 0; border-radius: 6px;">
                            <strong>${region}</strong>: ${ec2Count} EC2, ${rdsCount} RDS
                        </div>
                    `;
                }
            }
        }

        resourceSummary.innerHTML = resourceHTML;
    }

    function displayOrphanedResources(data, accountId) {
        const orphanedSection = document.getElementById('orphanedSection');
        const orphanedSummary = document.getElementById('orphanedSummary');
        const isOrg = data.resources.mode === 'organization';

        // Resource type display config: key → {icon, label, nameKey, detailFn, style}
        const RESOURCE_DISPLAY = {
            unattached_volumes:    { icon: '💾', label: 'Unattached EBS Volumes', nameKey: 'id', detail: v => `${v.size_gb}GB ${v.volume_type} (${v.region})` },
            orphaned_snapshots:    { icon: '📸', label: 'Orphaned Snapshots', nameKey: 'id', detail: s => `${s.size_gb}GB (${s.region})` },
            unassociated_eips:     { icon: '🌐', label: 'Unassociated Elastic IPs', nameKey: 'public_ip', detail: e => `${e.region}` },
            stale_amis:            { icon: '🖼️', label: 'Stale AMIs (>90 days)', nameKey: 'id', detail: a => `${(a.name||'').substring(0,25)} (${a.age_days} days old)` },
            idle_load_balancers:   { icon: '⚖️', label: 'Idle Load Balancers', nameKey: 'name', detail: l => `${l.type} - ${l.reason}` },
            nat_gateways:          { icon: '🌉', label: 'NAT Gateways', nameKey: 'id', detail: n => `VPC ${n.vpc_id} (${n.region})`, costSuffix: ' base' },
            gp2_volumes:           { icon: '💽', label: 'gp2 → gp3 Upgrade', nameKey: 'id', detail: v => `${v.size_gb}GB (${v.region})`, style: 'optimization' },
            cloudwatch_no_retention: { icon: '📋', label: 'Log Groups (no retention)', nameKey: 'name', detail: g => `${g.stored_gb}GB (${g.region})`, style: 'optimization', costLabel: 'storage' },
            idle_rds:              { icon: '🗄️', label: 'Idle RDS Instances', nameKey: 'id', detail: d => `${d.class} ${d.engine} - ${d.reason}` },
            lambda_functions:      { icon: '⚡', label: 'Unused/Over-Prov Lambda', nameKey: 'name', detail: f => `${f.memory_mb}MB, ${f.invocations_30d} inv/30d`, style: 'optimization' },
            elasticache_clusters:  { icon: '🧊', label: 'Idle ElastiCache', nameKey: 'id', detail: c => `${c.node_type} ${c.engine} - ${c.reason}` },
            opensearch_domains:    { icon: '🔍', label: 'Idle OpenSearch', nameKey: 'name', detail: d => `${d.instance_count}x ${d.instance_type}` },
            dynamodb_tables:       { icon: '📊', label: 'Unused DynamoDB Tables', nameKey: 'name', detail: t => `${t.billing_mode}, ${t.size_gb}GB` },
            rds_read_replicas:     { icon: '📖', label: 'Unused RDS Replicas', nameKey: 'id', detail: r => `${r.class} ${r.engine} → ${r.source}` },
            redshift_clusters:     { icon: '🏗️', label: 'Idle Redshift Clusters', nameKey: 'id', detail: c => `${c.num_nodes}x ${c.node_type}` },
            ecs_services:          { icon: '📦', label: 'ECS Service Issues', nameKey: 'service', detail: s => `cluster: ${s.cluster}, ${s.launch_type}` },
            kinesis_streams:       { icon: '🌊', label: 'Idle Kinesis Streams', nameKey: 'name', detail: s => `${s.shard_count} shards, ${s.incoming_records_7d} records/7d` },
            glue_jobs:             { icon: '🔧', label: 'Glue Job Issues', nameKey: 'name', detail: j => `${j.worker_type}, ${j.num_workers} workers` },
            sagemaker_resources:   { icon: '🤖', label: 'SageMaker Issues', nameKey: 'name', detail: s => `${s.type} ${s.instance_type || ''}` },
            bedrock_resources:     { icon: '🧠', label: 'Bedrock Resources', nameKey: 'name', detail: b => `${b.type} (${b.status})${b.model ? ' — ' + b.model : ''}` },
            cloudfront_distributions: { icon: '🌍', label: 'Unused CloudFront', nameKey: 'id', detail: d => `${d.domain}` },
            api_gateways:          { icon: '🔗', label: 'Unused API Gateways', nameKey: 'name', detail: a => `${a.type}` },
            route53_hosted_zones:  { icon: '📍', label: 'Orphaned Route53 Zones', nameKey: 'name', detail: z => `${z.record_count} records` },
            secrets_manager:       { icon: '🔑', label: 'Unused Secrets', nameKey: 'name', detail: s => `${(s.issues||[])[0] || ''}` },
        };
        const allOrphanedKeys = Object.keys(RESOURCE_DISPLAY);

        let totals, orphaned;

        if (isOrg && accountId) {
            const acctData = (data.resources.accounts || {})[accountId] || {};
            totals = acctData.totals || {};
            orphaned = acctData.orphaned_resources || {};
        } else if (isOrg) {
            // Aggregate across all accounts
            totals = data.resources.org_totals || {};
            orphaned = {};
            allOrphanedKeys.forEach(k => orphaned[k] = []);
            for (const acctData of Object.values(data.resources.accounts || {})) {
                const ao = acctData.orphaned_resources || {};
                allOrphanedKeys.forEach(k => orphaned[k].push(...(ao[k] || [])));
            }
        } else {
            totals = data.resources.totals || {};
            orphaned = data.resources.orphaned_resources || {};
        }

        const totalOrphaned = allOrphanedKeys.reduce((sum, k) => sum + (orphaned[k] || []).length, 0);

        if (totalOrphaned === 0) {
            orphanedSection.style.display = 'none';
            return;
        }

        orphanedSection.style.display = 'block';

        let totalWaste = 0;
        allOrphanedKeys.forEach(k => {
            (orphaned[k] || []).forEach(item => totalWaste += item.estimated_monthly_cost || 0);
        });

        let html = `
            <div class="orphaned-total-waste">
                <span class="waste-amount">$${totalWaste.toFixed(2)}/month</span>
                <span class="waste-label">Estimated waste from idle & orphaned resources</span>
            </div>
            <div class="orphaned-grid">
        `;

        // Data-driven rendering for all resource types
        for (const [key, config] of Object.entries(RESOURCE_DISPLAY)) {
            const items = orphaned[key] || [];
            if (items.length === 0) continue;

            const cost = items.reduce((sum, item) => sum + (item.estimated_monthly_cost || 0), 0);
            const isOptimization = config.style === 'optimization';
            const borderStyle = isOptimization ? ' style="border-color: #ffc107;"' : '';
            const costStyle = isOptimization ? ' style="color: #856404;"' : '';
            const costSuffix = config.costSuffix || '';
            const costLabel = config.costLabel || '';

            let costDisplay;
            if (cost > 0) {
                costDisplay = `~$${cost.toFixed(2)}/mo${costSuffix}${costLabel ? ' ' + costLabel : ''}`;
            } else {
                costDisplay = isOptimization ? 'Optimization available' : 'Review recommended';
            }

            html += `
                <div class="orphaned-box"${borderStyle}>
                    <div class="orphaned-icon">${config.icon}</div>
                    <div class="orphaned-count">${items.length}</div>
                    <div class="orphaned-label">${config.label}</div>
                    <div class="orphaned-cost"${costStyle}>${costDisplay}</div>
                    <details class="orphaned-details">
                        <summary>View details</summary>
                        <ul>
                            ${items.slice(0, 5).map(item => {
                                const name = maskField(config.nameKey, String(item[config.nameKey] || 'unknown'));
                                let detail = '';
                                try { detail = obfuscate(config.detail(item)); } catch(e) {}
                                return `<li><code>${name}</code>${detail ? ' - ' + detail : ''}</li>`;
                            }).join('')}
                            ${items.length > 5 ? `<li>...and ${items.length - 5} more</li>` : ''}
                        </ul>
                    </details>
                </div>
            `;
        }

        html += '</div>';
        orphanedSummary.innerHTML = html;
    }

    // --- Bedrock Usage (optional) ---
    function displayBedrockUsage(data, accountId) {
        const section = document.getElementById('bedrockSection');
        const body = document.getElementById('bedrockSummary');
        if (!section || !body) return;

        const isOrg = data.resources.mode === 'organization';
        let bedrockUsage = null;

        if (isOrg && accountId) {
            const acctData = (data.resources.accounts || {})[accountId] || {};
            bedrockUsage = acctData.bedrock_usage;
        } else if (isOrg) {
            // Aggregate across accounts
            const accounts = data.resources.accounts || {};
            const merged = { model_usage: [], top_callers: [], regions_with_activity: [] };
            for (const acctData of Object.values(accounts)) {
                const bu = acctData.bedrock_usage;
                if (!bu) continue;
                merged.model_usage.push(...(bu.model_usage || []));
                merged.top_callers.push(...(bu.top_callers || []));
                merged.regions_with_activity.push(...(bu.regions_with_activity || []));
            }
            if (merged.model_usage.length || merged.top_callers.length) {
                bedrockUsage = merged;
            }
        } else {
            bedrockUsage = data.resources.bedrock_usage;
        }

        if (!bedrockUsage || (!bedrockUsage.model_usage.length && !bedrockUsage.top_callers.length)) {
            section.style.display = 'none';
            return;
        }

        section.style.display = 'block';
        let html = '';

        // Model usage table
        if (bedrockUsage.model_usage && bedrockUsage.model_usage.length > 0) {
            const totalInvocations = bedrockUsage.model_usage.reduce((s, m) => s + m.invocations_30d, 0);
            const totalInput = bedrockUsage.model_usage.reduce((s, m) => s + m.input_tokens_30d, 0);
            const totalOutput = bedrockUsage.model_usage.reduce((s, m) => s + m.output_tokens_30d, 0);

            html += `
                <div class="bedrock-overview">
                    <div class="resource-grid">
                        <div class="resource-box">
                            <div class="resource-count">${bedrockUsage.model_usage.length}</div>
                            <div class="resource-label">Models Used</div>
                        </div>
                        <div class="resource-box">
                            <div class="resource-count">${formatNumber(totalInvocations)}</div>
                            <div class="resource-label">Invocations (30d)</div>
                        </div>
                        <div class="resource-box">
                            <div class="resource-count">${formatNumber(totalInput)}</div>
                            <div class="resource-label">Input Tokens (30d)</div>
                        </div>
                        <div class="resource-box">
                            <div class="resource-count">${formatNumber(totalOutput)}</div>
                            <div class="resource-label">Output Tokens (30d)</div>
                        </div>
                    </div>
                </div>
                <h4 style="margin-top: 16px;">Model Breakdown</h4>
                <table class="bedrock-table">
                    <thead>
                        <tr>
                            <th>Model</th>
                            <th>Region</th>
                            <th>Invocations</th>
                            <th>Input Tokens</th>
                            <th>Output Tokens</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${bedrockUsage.model_usage.map(m => `
                            <tr>
                                <td><code>${escapeHtml(m.model)}</code></td>
                                <td>${escapeHtml(m.region)}</td>
                                <td>${formatNumber(m.invocations_30d)}</td>
                                <td>${formatNumber(m.input_tokens_30d)}</td>
                                <td>${formatNumber(m.output_tokens_30d)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        // Top callers — resources with Bedrock access
        if (bedrockUsage.top_callers && bedrockUsage.top_callers.length > 0) {
            html += `
                <h4 style="margin-top: 24px;">Resources Using Bedrock</h4>
                <p style="color: #6c757d; font-size: 0.9em; margin-bottom: 8px;">Lambda functions, ECS services, and IAM principals with Bedrock permissions — identified via IAM role analysis and CloudTrail.</p>
                <div class="bedrock-callers-grid">
                    ${bedrockUsage.top_callers.map(c => {
                        const sourceClass = c.source_type.toLowerCase().replace(/[\s\/]/g, '-');
                        let detail = '';
                        if (c.source_type === 'Lambda') {
                            detail = `<div class="bedrock-caller-detail">Runtime: ${escapeHtml(c.runtime || 'N/A')} &bull; Memory: ${c.memory_mb || '?'}MB &bull; Invocations (30d): <strong>${formatNumber(c.call_count)}</strong></div>`;
                        } else if (c.source_type === 'ECS') {
                            detail = `<div class="bedrock-caller-detail">Cluster: ${escapeHtml(maskField('name', c.cluster || 'N/A'))} &bull; Running tasks: <strong>${c.running_tasks || 0}</strong></div>`;
                        } else {
                            detail = `<div class="bedrock-caller-detail">API calls: <strong>${formatNumber(c.call_count)}</strong></div>`;
                        }
                        const roleArn = c.role_arn ? `<div class="bedrock-caller-role">Role: <code>${escapeHtml(maskField('arn', c.role_arn))}</code></div>` : '';
                        return `
                            <div class="bedrock-caller-card">
                                <div class="bedrock-caller-header">
                                    <span class="bedrock-source-badge bedrock-source-${sourceClass}">${escapeHtml(c.source_type)}</span>
                                    <code class="bedrock-caller-name">${escapeHtml(maskField('name', c.principal))}</code>
                                    <span class="bedrock-caller-region">${escapeHtml(c.region)}</span>
                                </div>
                                ${detail}
                                ${roleArn}
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        }

        // Regions with activity
        if (bedrockUsage.regions_with_activity && bedrockUsage.regions_with_activity.length > 0) {
            const unique = [...new Set(bedrockUsage.regions_with_activity)];
            html += `<p style="margin-top: 12px; color: #6c757d; font-size: 0.9em;">Active regions: ${unique.join(', ')}</p>`;
        }

        body.innerHTML = html;
    }

    function formatNumber(n) {
        if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
        if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
        return String(n);
    }

    // --- History ---
    async function loadHistory() {
        try {
            const data = await fetchJSON(BASE_PATH + '/api/history');

            if (!data.success || !data.entries || data.entries.length === 0) {
                document.getElementById('historySection').style.display = 'none';
                return;
            }

            const section = document.getElementById('historySection');
            const list = document.getElementById('historyList');
            section.style.display = 'block';

            list.innerHTML = data.entries.map(entry => `
                <div class="history-entry" data-filename="${entry.filename}">
                    <div class="history-entry-info">
                        <span class="history-date">${escapeHtml(entry.date)}</span>
                        ${entry.total_cost ? `<span class="history-cost">${escapeHtml(entry.total_cost)}</span>` : ''}
                        ${entry.accounts ? `<span class="history-accounts">${escapeHtml(entry.accounts)}</span>` : ''}
                    </div>
                    <div class="history-entry-actions">
                        <button class="btn-small history-view-btn" data-filename="${entry.filename}">View</button>
                        <a class="btn-small" href="${BASE_PATH}/api/history/${entry.filename}/download" download>Download</a>
                    </div>
                </div>
            `).join('');

            // Add click handlers
            list.querySelectorAll('.history-view-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    viewHistoryEntry(this.getAttribute('data-filename'));
                });
            });
        } catch (err) {
            console.error('Error loading history:', err);
        }
    }

    async function viewHistoryEntry(filename) {
        try {
            const data = await fetchJSON(BASE_PATH + '/api/history/' + encodeURIComponent(filename));

            if (!data.success) return;

            const modal = document.getElementById('historyModal');
            const title = document.getElementById('historyModalTitle');
            const body = document.getElementById('historyModalBody');
            const downloadLink = document.getElementById('historyDownloadLink');

            title.textContent = filename;
            downloadLink.href = BASE_PATH + '/api/history/' + encodeURIComponent(filename) + '/download';

            // Render markdown as HTML (simple conversion) + demo mode
            body.innerHTML = renderMarkdown(obfuscate(data.content));
            modal.style.display = 'flex';
        } catch (err) {
            console.error('Error viewing history:', err);
        }
    }

    function renderMarkdown(md) {
        let html = escapeHtml(md);
        // Headers
        html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
        html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
        html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
        // Bold
        html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // Code blocks
        html = html.replace(/```\n?([\s\S]*?)```/g, '<pre><code>$1</code></pre>');
        // Inline code
        html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
        // Tables
        html = html.replace(/\|(.+)\|\n\|[-| ]+\|\n((?:\|.+\|\n?)*)/g, function(match, header, rows) {
            const headers = header.split('|').map(h => `<th>${h.trim()}</th>`).join('');
            const bodyRows = rows.trim().split('\n').map(row => {
                const cells = row.split('|').filter(c => c).map(c => `<td>${c.trim()}</td>`).join('');
                return `<tr>${cells}</tr>`;
            }).join('');
            return `<table><thead><tr>${headers}</tr></thead><tbody>${bodyRows}</tbody></table>`;
        });
        // List items
        html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
        html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
        // Paragraphs
        html = html.split('\n\n').map(p => {
            if (!p.trim() || p.includes('<h') || p.includes('<ul') || p.includes('<pre') || p.includes('<table')) return p;
            return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
        }).join('');
        return html;
    }

    function showError(message) {
        const errorMessage = document.getElementById('errorMessage');
        errorMessage.textContent = message;
        error.style.display = 'block';
        updateStatus('❌ Analysis failed', 'error');
    }

    function updateStatus(text, type) {
        const statusText = document.getElementById('statusText');
        statusText.textContent = text;
        statusBar.className = 'status-bar status-' + type;
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
});
