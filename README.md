# AWS Cost Optimizer

An AI-powered AWS cost analysis tool that uses Amazon Bedrock Nova to provide intelligent cost optimization recommendations.

## Features

- Scans 25+ AWS resource types across all enabled regions
- AI-powered analysis using AWS Bedrock Nova with actionable CLI commands
- Bedrock usage analysis: per-model token breakdown and caller attribution via IAM role tracing
- Multi-account support via AWS Organizations with per-account drill-down
- Cost Explorer integration with service-level spend breakdown
- Demo mode to obfuscate sensitive values for screen recordings
- Analysis history saved as downloadable markdown files
- Interactive follow-up chat for deeper investigation
- Docker-based deployment with Kubernetes/k3s support

## Resource Types Scanned

| Category | Resources |
|----------|-----------|
| Compute | EC2 instances, Lambda functions, ECS services |
| Database & Cache | RDS instances, RDS read replicas, idle RDS, ElastiCache, OpenSearch, DynamoDB, Redshift |
| Storage | S3 buckets, unattached EBS volumes, orphaned snapshots, stale AMIs, gp2 volumes |
| Networking | Elastic IPs, load balancers, NAT gateways, CloudFront, API Gateway, Route53 |
| ML & AI | SageMaker (notebooks + endpoints), Bedrock (provisioned throughput, custom models, knowledge bases, agents, guardrails) |
| Streaming & Analytics | Kinesis streams, Glue jobs |
| Security & Config | Secrets Manager, CloudWatch log groups (no retention) |
| Bedrock Usage | Per-model invocations/tokens (CloudWatch), caller attribution via IAM role analysis (Lambda, ECS) |

## Prerequisites

- Docker and Docker Compose
- AWS Account with appropriate IAM permissions
- AWS Bedrock access with Nova model enabled

## Quick Start

1. **Clone and setup:**
   ```bash
   chmod +x setup-aws.sh && ./setup-aws.sh
   ```

2. **Run the application:**
   ```bash
   docker-compose up --build
   ```

3. **Open browser:**
   ```
   http://localhost:5000
   ```

## Required IAM Permissions

The setup script (`setup-aws.sh`) creates an IAM user with:

- **AWS Managed Policy: `ReadOnlyAccess`** — read-only access to all AWS services (EC2, S3, RDS, Lambda, CloudWatch, Cost Explorer, etc.)
- **Custom Policy: `AWSCostOptimizerBedrockAccess`** — adds:
  - `bedrock:InvokeModel*`, `bedrock:Converse*` — AI analysis via Bedrock Nova
  - `bedrock:List*`, `bedrock:Get*` — scan provisioned throughput, custom models, guardrails
  - `bedrock-agent:List*`, `bedrock-agent:Get*` — scan knowledge bases and agents
  - `cloudtrail:LookupEvents` — Bedrock management event attribution
  - `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` — identify Lambda/ECS roles with Bedrock permissions

## Configuration

Edit `.env` file:
```
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
AWS_REGIONS=all  # or us-east-1,us-west-2,eu-west-1
BEDROCK_MODEL_ID=amazon.nova-lite-v1:0
CROSS_ACCOUNT_ROLE_NAME=CostOptimizerReadOnly  # for multi-account mode
```

### Multi-Region Scanning

The tool supports scanning multiple AWS regions:

**Scan all enabled regions:**
```
AWS_REGIONS=all
```

**Scan specific regions:**
```
AWS_REGIONS=us-east-1,us-west-2,eu-west-1
```

**Scan single region:**
```
AWS_REGIONS=us-east-1
```

Resources are scanned in parallel across regions for faster analysis. The UI will show both total counts and per-region breakdowns.

### Multi-Account Setup (AWS Organizations)

If your AWS credentials have Organizations access, the tool automatically discovers all accounts and shows an account selector in the UI.

**Setup cross-account roles in member accounts:**

Option A - Run the setup script in each member account:
```bash
./setup-org-role.sh <management-account-id>
```

Option B - Deploy via CloudFormation StackSet (recommended):
```bash
aws cloudformation create-stack-set \
  --stack-set-name CostOptimizerRole \
  --template-body file://cloudformation/cross-account-role.yaml \
  --parameters ParameterKey=ManagementAccountId,ParameterValue=<your-account-id> \
  --capabilities CAPABILITY_NAMED_IAM
```

To remove cross-account roles:
```bash
./destroy-org-role.sh
```

### Bedrock Usage Analysis

When Bedrock activity is detected, the tool provides a dedicated analysis section:

- **Model breakdown**: per-model invocation counts, input/output token volumes (from CloudWatch `AWS/Bedrock` metrics)
- **Caller attribution**: identifies Lambda functions and ECS services whose IAM roles have Bedrock permissions, shows their invocation volumes, runtime, and memory configuration
- **Provisioned resources**: flags provisioned throughput (billed continuously), custom models, knowledge bases, agents, and guardrails

This section is optional and only appears when Bedrock usage exists.

### Analysis History

Analysis results are automatically saved as markdown files in `./data/history/`. History persists across container restarts via a volume mount. You can view and download past analyses from the web UI.

## Project Structure

```
aws-cost-optimizer/
├── app.py                    # Flask application
├── aws/                      # AWS integration
│   ├── account_manager.py    # Organization & cross-account sessions
│   ├── cache_manager.py      # Scan caching & history
│   ├── cost_analyzer.py      # Cost Explorer
│   └── resource_scanner.py   # Resource inventory (25+ scanners)
├── ai/                       # AI integration
│   ├── bedrock_client.py     # Bedrock client (Strands SDK)
│   └── cost_tools.py         # Strands tools for AI agent
├── cloudformation/            # CloudFormation templates
│   └── cross-account-role.yaml
├── static/                   # Frontend assets
├── templates/                # HTML templates
├── data/                     # Persistent data (gitignored)
│   ├── cache/                # Scan result cache
│   └── history/              # Analysis history (markdown)
├── setup-aws.sh              # Single-account IAM setup
├── setup-org-role.sh         # Cross-account role setup
├── destroy-aws.sh            # Cleanup single-account
└── destroy-org-role.sh       # Cleanup cross-account role
```

## Development

Run without Docker:
```bash
pip install -r requirements.txt
python app.py
```

## Security Notes

- Never commit `.env` file
- Use IAM roles when possible
- Consider read-only permissions
- Run in isolated network environment

## License

MIT
