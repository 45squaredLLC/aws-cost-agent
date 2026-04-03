import os
import json
import boto3
from strands import Agent
from strands.models import BedrockModel
from strands_tools import use_aws
from ai.cost_tools import analyze_nat_gateways, analyze_data_transfer, get_cost_breakdown


class BedrockClient:
    """
    Client for AWS Bedrock Nova model using Strands SDK with AWS tools
    """

    def __init__(self, account_manager=None):
        self.model_id = os.getenv('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0')
        self.region = os.getenv('AWS_REGION', 'us-east-1')
        self.account_manager = account_manager
        
        # Create boto3 session for Bedrock
        boto_session = boto3.Session(region_name=self.region)
        
        # Create Bedrock model with increased token limit
        bedrock_model = BedrockModel(
            model_id=self.model_id,
            boto_session=boto_session,
            max_tokens=4096  # Increase token limit for complex queries
        )
        
        # Initialize Strands Agent with Bedrock model and cost optimization tools
        self.agent = Agent(
            model=bedrock_model,
            tools=[
                use_aws,
                analyze_nat_gateways,
                analyze_data_transfer,
                get_cost_breakdown
            ]
        )
    
    def get_initial_analysis(self, data):
        """
        Get initial analysis and recommendations
        
        Args:
            data: Dictionary containing cost_data and resources
            
        Returns:
            dict: Recommendations and CLI commands
        """
        prompt = self._build_analysis_prompt(data)
        
        try:
            response = self.agent(prompt)
            recommendations = self._parse_recommendations(response)
            return recommendations
            
        except Exception as e:
            print(f"Error calling Bedrock: {str(e)}")
            return {
                "error": str(e),
                "recommendations": [],
                "cli_commands": []
            }
    
    def chat(self, user_message, analysis_data, chat_history):
        """
        Interactive chat with context about AWS resources
        
        Args:
            user_message: User's question
            analysis_data: AWS cost and resource data
            chat_history: Previous conversation history
            
        Returns:
            str: AI response
        """
        try:
            # Build context-aware prompt
            prompt = self._build_chat_prompt(user_message, analysis_data, chat_history)
            
            # Get response from agent
            response = self.agent(prompt)
            
            return str(response)
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error in chat: {error_msg}")
            
            # Handle specific errors with helpful messages
            if "max_tokens" in error_msg.lower():
                return """I ran into a token limit while processing your request. This usually happens when checking many resources at once.

**To help me answer efficiently:**
- Ask about specific resources (e.g., "top 5 largest buckets")
- Limit scope (e.g., "S3 buckets in us-east-1")
- Break complex questions into smaller ones

**Try asking:**
- "What are my 5 largest S3 buckets?"
- "Show me S3 buckets over 100GB"
- "Check the 10 most recently created buckets"

This helps me provide answers without overwhelming the system."""
            
            elif "credentials" in error_msg.lower() or "access" in error_msg.lower():
                return f"I encountered an AWS access error: {error_msg}\n\nPlease verify your AWS credentials are configured correctly."
            
            else:
                return f"I encountered an error: {error_msg}\n\nTry rephrasing your question or asking about specific resources."
    
    def _build_analysis_prompt(self, data):
        """Build a detailed prompt for Nova"""
        cost_data = data.get('cost_data', {})
        resources = data.get('resources', {})
        mode = resources.get('mode', 'single')

        if mode == 'organization':
            return self._build_org_analysis_prompt(cost_data, resources)

        orphaned = resources.get('orphaned_resources', {})

        prompt = f"""You are an AWS cost optimization expert analyzing an AWS account.

IMPORTANT: This is an initial analysis. DO NOT use the use_aws tool here. Just analyze the provided data and give recommendations with example AWS CLI commands.

COST SUMMARY:
Total Cost (last {cost_data.get('period_days', 30)} days): ${cost_data.get('total_cost', 0)}

Top Services by Cost:
{json.dumps(cost_data.get('top_services', []), indent=2)}

ACTIVE RESOURCES:
EC2 Instances: {len(resources.get('ec2_instances', []))} instances
{self._format_ec2_details(resources.get('ec2_instances', []))}

RDS Instances: {len(resources.get('rds_instances', []))} instances
{self._format_rds_details(resources.get('rds_instances', []))}

S3 Buckets: {len(resources.get('s3_buckets', []))} buckets
{self._format_s3_details(resources.get('s3_buckets', []))}

ORPHANED/UNUSED RESOURCES (These are costing money but not being used!):

Unattached EBS Volumes: {len(orphaned.get('unattached_volumes', []))} volumes
{self._format_orphaned_volumes(orphaned.get('unattached_volumes', []))}

Orphaned EBS Snapshots: {len(orphaned.get('orphaned_snapshots', []))} snapshots
{self._format_orphaned_snapshots(orphaned.get('orphaned_snapshots', []))}

Unassociated Elastic IPs: {len(orphaned.get('unassociated_eips', []))} addresses
{self._format_unassociated_eips(orphaned.get('unassociated_eips', []))}

Stale AMIs (>90 days old): {len(orphaned.get('stale_amis', []))} images
{self._format_stale_amis(orphaned.get('stale_amis', []))}

NETWORKING & INFRASTRUCTURE COSTS:

Idle Load Balancers: {len(orphaned.get('idle_load_balancers', []))} idle
{self._format_idle_lbs(orphaned.get('idle_load_balancers', []))}

NAT Gateways: {len(orphaned.get('nat_gateways', []))} gateways ($32+/mo each)
{self._format_nat_gateways(orphaned.get('nat_gateways', []))}

OPTIMIZATION OPPORTUNITIES:

gp2 Volumes (upgradeable to gp3 for 20% savings): {len(orphaned.get('gp2_volumes', []))} volumes
{self._format_gp2_volumes(orphaned.get('gp2_volumes', []))}

CloudWatch Log Groups with No Retention: {len(orphaned.get('cloudwatch_no_retention', []))} groups
{self._format_cw_no_retention(orphaned.get('cloudwatch_no_retention', []))}

Idle RDS Instances (near-zero usage): {len(orphaned.get('idle_rds', []))} instances
{self._format_idle_rds(orphaned.get('idle_rds', []))}

Unused/Over-Provisioned Lambda Functions: {len(orphaned.get('lambda_functions', []))} functions
{self._format_lambda_functions(orphaned.get('lambda_functions', []))}

DATABASE & CACHE:

Idle ElastiCache Clusters: {len(orphaned.get('elasticache_clusters', []))}
{self._format_generic_resources(orphaned.get('elasticache_clusters', []), 'id', ['node_type', 'engine', 'reason'])}

Idle OpenSearch Domains: {len(orphaned.get('opensearch_domains', []))}
{self._format_generic_resources(orphaned.get('opensearch_domains', []), 'name', ['instance_type', 'instance_count', 'reason'])}

Unused DynamoDB Tables: {len(orphaned.get('dynamodb_tables', []))}
{self._format_generic_resources(orphaned.get('dynamodb_tables', []), 'name', ['billing_mode', 'size_gb', 'issues'])}

Unused RDS Read Replicas: {len(orphaned.get('rds_read_replicas', []))}
{self._format_generic_resources(orphaned.get('rds_read_replicas', []), 'id', ['class', 'engine', 'source', 'reason'])}

Idle Redshift Clusters: {len(orphaned.get('redshift_clusters', []))}
{self._format_generic_resources(orphaned.get('redshift_clusters', []), 'id', ['node_type', 'num_nodes', 'reason'])}

CONTAINERS:

ECS Issues: {len(orphaned.get('ecs_services', []))}
{self._format_generic_resources(orphaned.get('ecs_services', []), 'service', ['cluster', 'launch_type', 'issues'])}

STREAMING & ANALYTICS:

Idle Kinesis Streams: {len(orphaned.get('kinesis_streams', []))}
{self._format_generic_resources(orphaned.get('kinesis_streams', []), 'name', ['shard_count', 'reason'])}

Glue Job Issues: {len(orphaned.get('glue_jobs', []))}
{self._format_generic_resources(orphaned.get('glue_jobs', []), 'name', ['worker_type', 'num_workers', 'issues'])}

ML RESOURCES:

SageMaker Issues: {len(orphaned.get('sagemaker_resources', []))}
{self._format_generic_resources(orphaned.get('sagemaker_resources', []), 'name', ['type', 'instance_type', 'issues'])}

Bedrock Resources: {len(orphaned.get('bedrock_resources', []))}
{self._format_generic_resources(orphaned.get('bedrock_resources', []), 'name', ['type', 'status', 'model', 'commitment', 'issues'])}

{self._format_bedrock_usage(resources.get('bedrock_usage'))}

NETWORKING & API:

Unused CloudFront Distributions: {len(orphaned.get('cloudfront_distributions', []))}
{self._format_generic_resources(orphaned.get('cloudfront_distributions', []), 'id', ['domain', 'issues'])}

Unused API Gateways: {len(orphaned.get('api_gateways', []))}
{self._format_generic_resources(orphaned.get('api_gateways', []), 'name', ['type', 'issues'])}

Orphaned Route53 Zones: {len(orphaned.get('route53_hosted_zones', []))}
{self._format_generic_resources(orphaned.get('route53_hosted_zones', []), 'name', ['record_count', 'issues'])}

Unused Secrets: {len(orphaned.get('secrets_manager', []))}
{self._format_generic_resources(orphaned.get('secrets_manager', []), 'name', ['issues'])}

TASK:
Analyze the data above and identify the top 7-10 cost optimization opportunities.

PRIORITY: Orphaned resources and idle infrastructure are immediate savings. gp2→gp3 migration and CloudWatch retention are quick wins. Bedrock provisioned throughput and high-volume model usage are key AI/ML cost drivers. Prioritize by dollar impact.

IMPORTANT: Use ONLY real AWS CLI commands (e.g., "aws bedrock ...", "aws ec2 ...", etc.). Never invent or abbreviate service names. If you don't know the exact CLI command for a service, omit the cli_command field rather than guessing.

For each opportunity, provide:
1. A brief title
2. A detailed description of the issue
3. Estimated monthly savings (use the estimated_monthly_cost values provided)
4. A SPECIFIC AWS CLI command that would help implement this recommendation
5. Risk level (Low/Medium/High)

IMPORTANT FOR CLI COMMANDS:
- Use the ACTUAL resource IDs from the data above
- Provide real, working AWS CLI commands
- Include proper regions where applicable
- Make commands copy-pasteable

Example CLI commands:
- Delete unattached volume: aws ec2 delete-volume --volume-id vol-xxx --region us-east-1
- Delete orphaned snapshot: aws ec2 delete-snapshot --snapshot-id snap-xxx --region us-east-1
- Release Elastic IP: aws ec2 release-address --allocation-id eipalloc-xxx --region us-east-1
- Deregister stale AMI: aws ec2 deregister-image --image-id ami-xxx --region us-east-1
- Upgrade gp2 to gp3: aws ec2 modify-volume --volume-id vol-xxx --volume-type gp3 --region us-east-1
- Set log retention: aws logs put-retention-policy --log-group-name /aws/xxx --retention-in-days 30 --region us-east-1
- Delete idle LB: aws elbv2 delete-load-balancer --load-balancer-arn arn:xxx --region us-east-1
- Delete NAT Gateway: aws ec2 delete-nat-gateway --nat-gateway-id nat-xxx --region us-east-1
- Delete unused Lambda: aws lambda delete-function --function-name xxx --region us-east-1
- Delete provisioned throughput: aws bedrock delete-provisioned-model-throughput --provisioned-model-id xxx --region us-east-1
- List Bedrock model invocation logging: aws bedrock get-model-invocation-logging-configuration --region us-east-1

Respond ONLY with valid JSON in this exact format:
{{
  "recommendations": [
    {{
      "title": "Brief title",
      "description": "Detailed description with specific resource IDs",
      "estimated_savings": "$X/month",
      "cli_command": "aws ec2 delete-volume --volume-id vol-abc123 --region us-east-1",
      "risk": "Low"
    }}
  ]
}}

DO NOT use any tools. DO NOT call use_aws. Just provide the JSON response with actionable AWS CLI commands.
"""
        return prompt

    def _build_org_analysis_prompt(self, cost_data, resources):
        """Build analysis prompt for organization mode with per-account data."""
        accounts = resources.get('accounts', {})
        account_costs = cost_data.get('account_costs', {})

        account_sections = []
        for acct_id, acct_data in accounts.items():
            if 'error' in acct_data and 'totals' not in acct_data:
                account_sections.append(f"\n### Account: {acct_data.get('account_name', acct_id)} ({acct_id})\nError: {acct_data['error']}")
                continue

            acct_cost = account_costs.get(acct_id, {})
            orphaned = acct_data.get('orphaned_resources', {})

            section = f"""
### Account: {acct_data.get('account_name', acct_id)} ({acct_id})
Cost: ${acct_cost.get('total_cost', 0)} (last {cost_data.get('period_days', 30)} days)
Top Services: {json.dumps(acct_cost.get('top_services', [])[:5], indent=2)}

Resources:
  EC2: {acct_data.get('totals', {}).get('ec2_instances', 0)} | RDS: {acct_data.get('totals', {}).get('rds_instances', 0)} | S3: {acct_data.get('totals', {}).get('s3_buckets', 0)}
{self._format_ec2_details(acct_data.get('ec2_instances', []))}
{self._format_rds_details(acct_data.get('rds_instances', []))}

Orphaned Resources:
  Unattached Volumes: {len(orphaned.get('unattached_volumes', []))}
{self._format_orphaned_volumes(orphaned.get('unattached_volumes', []))}
  Orphaned Snapshots: {len(orphaned.get('orphaned_snapshots', []))}
{self._format_orphaned_snapshots(orphaned.get('orphaned_snapshots', []))}
  Unassociated EIPs: {len(orphaned.get('unassociated_eips', []))}
{self._format_unassociated_eips(orphaned.get('unassociated_eips', []))}
  Stale AMIs: {len(orphaned.get('stale_amis', []))}
{self._format_stale_amis(orphaned.get('stale_amis', []))}

Bedrock Resources: {len(orphaned.get('bedrock_resources', []))}
{self._format_generic_resources(orphaned.get('bedrock_resources', []), 'name', ['type', 'status', 'model', 'commitment', 'issues'])}

{self._format_bedrock_usage(acct_data.get('bedrock_usage'))}"""
            account_sections.append(section)

        prompt = f"""You are an AWS cost optimization expert analyzing an AWS Organization with multiple accounts.

IMPORTANT: This is an initial analysis. DO NOT use the use_aws tool here. Just analyze the provided data.

ORGANIZATION SUMMARY:
Total Cost: ${cost_data.get('org_total_cost', cost_data.get('total_cost', 0))}
Accounts Analyzed: {len(accounts)}

PER-ACCOUNT BREAKDOWN:
{''.join(account_sections)}

TASK:
Analyze the data above and identify the top 5-7 cost optimization opportunities ACROSS ALL ACCOUNTS.
Look for:
- Cross-account optimization opportunities (e.g., consolidation, shared resources)
- Per-account orphaned resources causing waste
- Accounts with disproportionately high costs
- Bedrock usage optimization (provisioned throughput, high-cost models, excessive token usage)

IMPORTANT: Use ONLY real AWS CLI commands (e.g., "aws bedrock ...", "aws ec2 ...", etc.). Never invent or abbreviate service names. If you don't know the exact CLI command for a service, omit the cli_command field rather than guessing.

For each, provide title, description (include account name/ID), estimated savings, CLI command, and risk level.

Include account ID in CLI commands where relevant (e.g., use --profile or note which account).

Respond ONLY with valid JSON:
{{
  "recommendations": [
    {{
      "title": "Brief title",
      "description": "Description including account name/ID",
      "estimated_savings": "$X/month",
      "cli_command": "aws ec2 delete-volume --volume-id vol-abc123 --region us-east-1",
      "risk": "Low"
    }}
  ]
}}

DO NOT use any tools. DO NOT call use_aws. Just provide the JSON response.
"""
        return prompt
    
    def _build_chat_prompt(self, user_message, analysis_data, chat_history):
        cost_data = analysis_data.get('cost_data', {})
        resources = analysis_data.get('resources', {})
        mode = resources.get('mode', 'single')

        # Build conversation history
        history_text = ""
        if len(chat_history) > 1:
            history_text = "\n\nPREVIOUS CONVERSATION:\n"
            for msg in chat_history[:-1]:
                role = "User" if msg['role'] == 'user' else "Assistant"
                history_text += f"{role}: {msg['content']}\n"

        # Build account context for org mode
        account_context = ""
        if mode == 'organization':
            accounts = resources.get('accounts', {})
            account_lines = []
            for acct_id, acct_data in accounts.items():
                acct_name = acct_data.get('account_name', acct_id)
                acct_cost = cost_data.get('account_costs', {}).get(acct_id, {})
                account_lines.append(f"  - {acct_name} ({acct_id}): ${acct_cost.get('total_cost', 0)}")
            account_context = f"""
ORGANIZATION MODE - Multiple accounts analyzed:
{chr(10).join(account_lines)}

All tools accept an optional account_id parameter to target a specific account.
If the user asks about a specific account, pass its account_id to the tool.
If the user's question is ambiguous about which account, ask them to clarify.
"""
            totals = resources.get('org_totals', {})
            total_cost = cost_data.get('org_total_cost', cost_data.get('total_cost', 0))
        else:
            totals = resources.get('totals', {})
            total_cost = cost_data.get('total_cost', 0)

        prompt = f"""You are an AWS cost optimization expert assistant with real-time access to AWS APIs.

AWS CREDENTIALS: Already configured via environment variables. DO NOT ask for AWS profile or credentials - they are automatically available to all tools.
{account_context}
STATIC CONTEXT - AWS ACCOUNT DATA FROM INITIAL SCAN:

Cost Summary (last {cost_data.get('period_days', 30)} days):
- Total Cost: ${total_cost}
- Top Services: {json.dumps(cost_data.get('top_services', [])[:5], indent=2)}

Resources Inventory from Scan:
- EC2 Instances: {totals.get('ec2_instances', 0)} total
- RDS Instances: {totals.get('rds_instances', 0)} total
- S3 Buckets: {totals.get('s3_buckets', 0)} total

Orphaned/Unused Resources Detected:
- Unattached EBS Volumes: {totals.get('unattached_volumes', 0)} total
- Orphaned EBS Snapshots: {totals.get('orphaned_snapshots', 0)} total
- Unassociated Elastic IPs: {totals.get('unassociated_eips', 0)} total
- Stale AMIs (>90 days): {totals.get('stale_amis', 0)} total
- Idle Load Balancers: {totals.get('idle_load_balancers', 0)} total
- NAT Gateways: {totals.get('nat_gateways', 0)} ($32+/mo each)
- gp2 Volumes (upgrade to gp3): {totals.get('gp2_volumes', 0)} total
- CloudWatch Logs (no retention): {totals.get('cloudwatch_no_retention', 0)} groups
- Idle RDS Instances: {totals.get('idle_rds', 0)} total
- Unused Lambda Functions: {totals.get('lambda_functions', 0)} total
- Idle ElastiCache: {totals.get('elasticache_clusters', 0)} | Idle OpenSearch: {totals.get('opensearch_domains', 0)}
- Unused DynamoDB: {totals.get('dynamodb_tables', 0)} | Unused RDS Replicas: {totals.get('rds_read_replicas', 0)}
- Idle Redshift: {totals.get('redshift_clusters', 0)} | ECS Issues: {totals.get('ecs_services', 0)}
- Idle Kinesis: {totals.get('kinesis_streams', 0)} | Glue Issues: {totals.get('glue_jobs', 0)}
- SageMaker: {totals.get('sagemaker_resources', 0)} | Unused APIs: {totals.get('api_gateways', 0)}
- Unused CloudFront: {totals.get('cloudfront_distributions', 0)} | Orphaned Route53: {totals.get('route53_hosted_zones', 0)}
- Unused Secrets: {totals.get('secrets_manager', 0)}
- Bedrock Resources: {totals.get('bedrock_resources', 0)}

{self._format_bedrock_usage_for_chat(resources)}

AVAILABLE TOOLS:

AWS credentials are already configured. Call tools directly without asking for credentials.

1. **analyze_nat_gateways(region, account_id)** - PREFERRED for NAT Gateway analysis
   - Analyzes NAT Gateways and identifies missing VPC Gateway Endpoints
   - Shows potential savings from free S3/DynamoDB endpoints

2. **analyze_data_transfer(region, account_id)** - PREFERRED for data transfer analysis
   - Finds instances using NAT Gateways in different AZs (causes extra charges)
   - Identifies cross-region VPC peering costs

3. **get_cost_breakdown(days, group_by, account_id)** - PREFERRED for cost analysis
   - Gets cost breakdown from Cost Explorer
   - group_by options: SERVICE, REGION, USAGE_TYPE, LINKED_ACCOUNT

4. **use_aws(service_name, operation_name, parameters, region)** - General AWS API
   - For any AWS API not covered by specialized tools

TOOL SELECTION RULES:
- For NAT Gateway questions → use analyze_nat_gateways
- For data transfer questions → use analyze_data_transfer
- For cost breakdown questions → use get_cost_breakdown
- For everything else → use use_aws

RESPONSE RULES:
1. CALL THE TOOL IMMEDIATELY - don't explain what you will do
2. Use specialized tools when available (faster and more efficient)
3. DO NOT ask for AWS credentials - they are already configured
4. After getting results, provide a clear conversational answer
5. Be efficient - don't make unnecessary API calls

{history_text}

USER QUESTION: {user_message}

Remember:
- AWS credentials are ALREADY CONFIGURED
- USE THE TOOL immediately to get data
- DON'T ask for profiles or credentials
- DON'T describe - just DO IT
"""
        return prompt
    
    def _format_ec2_details(self, instances):
        if not instances:
            return "No EC2 instances found"
        
        details = []
        for inst in instances[:5]:
            cpu = inst.get('cpu_utilization', 'N/A')
            details.append(
                f"  - {inst['id']} ({inst['type']}): State={inst['state']}, CPU={cpu}%"
            )
        
        if len(instances) > 5:
            details.append(f"  ... and {len(instances) - 5} more")
        
        return "\n".join(details)
    
    def _format_rds_details(self, instances):
        """Format RDS instance details for prompt"""
        if not instances:
            return "No RDS instances found"

        details = []
        for db in instances[:5]:
            cpu = db.get('cpu_utilization')
            cpu_str = f", CPU={cpu}%" if cpu is not None else ""
            details.append(
                f"  - {db['id']} ({db['class']}): Engine={db['engine']}, Storage={db['storage_gb']}GB{cpu_str}"
            )

        if len(instances) > 5:
            details.append(f"  ... and {len(instances) - 5} more")

        return "\n".join(details)

    def _format_orphaned_volumes(self, volumes):
        """Format unattached EBS volumes for prompt"""
        if not volumes:
            return "  No unattached volumes found"

        details = []
        total_cost = 0
        for vol in volumes[:10]:
            cost = vol.get('estimated_monthly_cost', 0)
            total_cost += cost
            details.append(
                f"  - {vol['id']}: {vol['size_gb']}GB {vol['volume_type']}, "
                f"Region={vol['region']}, Cost=${cost}/month"
            )

        if len(volumes) > 10:
            details.append(f"  ... and {len(volumes) - 10} more")

        details.append(f"  TOTAL ESTIMATED WASTE: ${round(total_cost, 2)}/month")
        return "\n".join(details)

    def _format_orphaned_snapshots(self, snapshots):
        """Format orphaned EBS snapshots for prompt"""
        if not snapshots:
            return "  No orphaned snapshots found"

        details = []
        total_cost = 0
        for snap in snapshots[:10]:
            cost = snap.get('estimated_monthly_cost', 0)
            total_cost += cost
            details.append(
                f"  - {snap['id']}: {snap['size_gb']}GB, Volume={snap['volume_id']}, "
                f"Region={snap['region']}, Cost=${cost}/month"
            )

        if len(snapshots) > 10:
            details.append(f"  ... and {len(snapshots) - 10} more")

        details.append(f"  TOTAL ESTIMATED WASTE: ${round(total_cost, 2)}/month")
        return "\n".join(details)

    def _format_unassociated_eips(self, eips):
        """Format unassociated Elastic IPs for prompt"""
        if not eips:
            return "  No unassociated Elastic IPs found"

        details = []
        total_cost = 0
        for eip in eips[:10]:
            cost = eip.get('estimated_monthly_cost', 3.60)
            total_cost += cost
            details.append(
                f"  - {eip['public_ip']} (AllocationId={eip['allocation_id']}): "
                f"Region={eip['region']}, Cost=${cost}/month"
            )

        if len(eips) > 10:
            details.append(f"  ... and {len(eips) - 10} more")

        details.append(f"  TOTAL ESTIMATED WASTE: ${round(total_cost, 2)}/month")
        return "\n".join(details)

    def _format_stale_amis(self, amis):
        """Format stale AMIs for prompt"""
        if not amis:
            return "  No stale AMIs found"

        details = []
        total_cost = 0
        for ami in amis[:10]:
            cost = ami.get('estimated_monthly_cost', 0)
            total_cost += cost
            details.append(
                f"  - {ami['id']} ({ami['name'][:30]}): {ami['age_days']} days old, "
                f"Snapshots={ami['snapshot_size_gb']}GB, Region={ami['region']}, Cost=${cost}/month"
            )

        if len(amis) > 10:
            details.append(f"  ... and {len(amis) - 10} more")

        details.append(f"  TOTAL ESTIMATED WASTE: ${round(total_cost, 2)}/month")
        return "\n".join(details)
    
    def _format_s3_details(self, buckets):
        """Format S3 bucket details for prompt"""
        if not buckets:
            return "  No S3 buckets found"

        no_lifecycle = [b for b in buckets if not b.get('has_lifecycle')]
        details = []
        if no_lifecycle:
            details.append(f"  WARNING: {len(no_lifecycle)}/{len(buckets)} buckets have NO lifecycle policy")
            for b in no_lifecycle[:5]:
                details.append(f"  - {b['name']} ({b['region']}) - no lifecycle rules")
            if len(no_lifecycle) > 5:
                details.append(f"  ... and {len(no_lifecycle) - 5} more without lifecycle")

        no_tiering = [b for b in buckets if not b.get('has_intelligent_tiering')]
        if no_tiering:
            details.append(f"  {len(no_tiering)}/{len(buckets)} buckets without Intelligent-Tiering")

        return "\n".join(details) if details else "  All buckets have lifecycle policies"

    def _format_idle_lbs(self, lbs):
        """Format idle load balancers for prompt"""
        if not lbs:
            return "  No idle load balancers found"

        details = []
        total_cost = 0
        for lb in lbs[:10]:
            cost = lb.get('estimated_monthly_cost', 0)
            total_cost += cost
            details.append(
                f"  - {lb['name']} ({lb['type']}): {lb['reason']}, "
                f"Region={lb['region']}, Cost=${cost}/month"
            )

        if len(lbs) > 10:
            details.append(f"  ... and {len(lbs) - 10} more")
        details.append(f"  TOTAL ESTIMATED WASTE: ${round(total_cost, 2)}/month")
        return "\n".join(details)

    def _format_nat_gateways(self, nats):
        """Format NAT Gateways for prompt"""
        if not nats:
            return "  No NAT Gateways found"

        details = []
        total_cost = 0
        for nat in nats[:10]:
            cost = nat.get('estimated_monthly_cost', 32.40)
            total_cost += cost
            details.append(
                f"  - {nat['id']}: VPC={nat['vpc_id']}, Region={nat['region']}, "
                f"Base Cost=${cost}/month + $0.045/GB processed"
            )

        if len(nats) > 10:
            details.append(f"  ... and {len(nats) - 10} more")
        details.append(f"  TOTAL BASE COST: ${round(total_cost, 2)}/month (excluding data processing)")
        return "\n".join(details)

    def _format_gp2_volumes(self, volumes):
        """Format gp2 volumes upgradeable to gp3"""
        if not volumes:
            return "  No gp2 volumes found"

        details = []
        total_savings = 0
        for vol in volumes[:10]:
            savings = vol.get('estimated_monthly_cost', 0)
            total_savings += savings
            attached = "attached" if vol.get('attached') else "unattached"
            details.append(
                f"  - {vol['id']}: {vol['size_gb']}GB gp2 ({attached}), "
                f"Region={vol['region']}, Savings=${savings}/month if upgraded to gp3"
            )

        if len(volumes) > 10:
            details.append(f"  ... and {len(volumes) - 10} more")
        details.append(f"  TOTAL POTENTIAL SAVINGS: ${round(total_savings, 2)}/month")
        return "\n".join(details)

    def _format_cw_no_retention(self, groups):
        """Format CloudWatch Log Groups with no retention"""
        if not groups:
            return "  All log groups have retention policies"

        details = []
        total_cost = 0
        for lg in groups[:10]:
            cost = lg.get('estimated_monthly_cost', 0)
            total_cost += cost
            details.append(
                f"  - {lg['name']}: {lg['stored_gb']}GB stored, "
                f"Region={lg['region']}, Storage Cost=${cost}/month (growing!)"
            )

        if len(groups) > 10:
            details.append(f"  ... and {len(groups) - 10} more")
        details.append(f"  TOTAL CURRENT STORAGE COST: ${round(total_cost, 2)}/month (will keep growing without retention)")
        return "\n".join(details)

    def _format_idle_rds(self, instances):
        """Format idle RDS instances"""
        if not instances:
            return "  No idle RDS instances detected"

        details = []
        for db in instances[:10]:
            cpu = db.get('avg_cpu_7d')
            conns = db.get('total_connections_7d')
            details.append(
                f"  - {db['id']} ({db['class']}): {db['reason']}, "
                f"Engine={db['engine']}, Storage={db['storage_gb']}GB, Region={db['region']}"
            )

        if len(instances) > 10:
            details.append(f"  ... and {len(instances) - 10} more")
        return "\n".join(details)

    def _format_lambda_functions(self, functions):
        """Format unused/over-provisioned Lambda functions"""
        if not functions:
            return "  No unused or over-provisioned Lambda functions found"

        details = []
        for fn in functions[:10]:
            issues_str = "; ".join(fn.get('issues', []))
            details.append(
                f"  - {fn['name']}: {fn['memory_mb']}MB, Runtime={fn['runtime']}, "
                f"Invocations(30d)={fn['invocations_30d']}, Issues: {issues_str}"
            )

        if len(functions) > 10:
            details.append(f"  ... and {len(functions) - 10} more")
        return "\n".join(details)

    def _format_bedrock_usage_for_chat(self, resources):
        """Extract and format Bedrock usage from resources for the chat prompt."""
        mode = resources.get('mode', 'single')
        if mode == 'organization':
            # Merge across accounts
            all_model_usage = []
            all_callers = []
            for acct_data in resources.get('accounts', {}).values():
                bu = acct_data.get('bedrock_usage')
                if bu:
                    all_model_usage.extend(bu.get('model_usage', []))
                    all_callers.extend(bu.get('top_callers', []))
            if all_model_usage or all_callers:
                return self._format_bedrock_usage({'model_usage': all_model_usage, 'top_callers': all_callers, 'regions_with_activity': []})
            return ""
        else:
            return self._format_bedrock_usage(resources.get('bedrock_usage'))

    def _format_bedrock_usage(self, bedrock_usage):
        """Format Bedrock usage data (CloudWatch + CloudTrail) for the AI prompt."""
        if not bedrock_usage:
            return ""

        lines = ["BEDROCK USAGE ANALYSIS (last 30 days):"]

        model_usage = bedrock_usage.get('model_usage', [])
        if model_usage:
            lines.append("\n  Model Usage:")
            for m in model_usage[:10]:
                lines.append(
                    f"  - {m['model']}: {m['invocations_30d']} invocations, "
                    f"{m['input_tokens_30d']} input tokens, "
                    f"{m['output_tokens_30d']} output tokens (region: {m['region']})"
                )

        top_callers = bedrock_usage.get('top_callers', [])
        if top_callers:
            lines.append("\n  Top Callers (CloudTrail attribution):")
            for c in top_callers[:10]:
                actions = ', '.join(f"{a['action']}({a['count']})" for a in c.get('actions', []))
                lines.append(
                    f"  - {c['principal']} (ARN: {c['arn']}): "
                    f"{c['call_count']} calls via {c['source_type']} "
                    f"[{actions}] (region: {c['region']})"
                )

        regions = bedrock_usage.get('regions_with_activity', [])
        if regions:
            lines.append(f"\n  Active regions: {', '.join(set(regions))}")

        return "\n".join(lines)

    def _format_generic_resources(self, items, name_key, detail_keys):
        """Generic formatter for any resource list."""
        if not items:
            return "  None found"

        details = []
        total_cost = 0
        for item in items[:10]:
            name = item.get(name_key, 'unknown')
            parts = [f"{k}={item[k]}" for k in detail_keys if k in item]
            region = item.get('region', '')
            cost = item.get('estimated_monthly_cost', 0)
            total_cost += cost
            line = f"  - {name}: {', '.join(parts)}"
            if region:
                line += f", Region={region}"
            if cost > 0:
                line += f", Cost=${cost}/month"
            details.append(line)

        if len(items) > 10:
            details.append(f"  ... and {len(items) - 10} more")
        if total_cost > 0:
            details.append(f"  TOTAL ESTIMATED COST: ${round(total_cost, 2)}/month")
        return "\n".join(details)

    def _parse_recommendations(self, response):
        """Parse Nova's response into structured recommendations"""
        try:
            text = str(response)
            
            # Try to find JSON in the response
            start = text.find('{')
            end = text.rfind('}') + 1
            
            if start >= 0 and end > start:
                json_str = text[start:end]
                parsed = json.loads(json_str)
                return parsed
            
            return {
                "recommendations": [],
                "raw_response": text
            }
            
        except Exception as e:
            print(f"Error parsing recommendations: {str(e)}")
            return {
                "recommendations": [],
                "error": str(e),
                "raw_response": str(response)
            }