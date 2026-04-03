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

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose (included with Docker Desktop)
- An AWS account with the [AWS CLI](https://aws.amazon.com/cli/) installed and configured (`aws configure`)
- Amazon Bedrock model access enabled (see Step 2 below)

## Getting Started

### Step 1: Clone the repository

```bash
git clone https://github.com/avansledright/aws-cost-optimizer.git
cd aws-cost-optimizer
```

### Step 2: Enable Bedrock model access

Before running the app, you must enable the AI model in the AWS Console:

1. Open the [Amazon Bedrock Console](https://console.aws.amazon.com/bedrock/)
2. In the left sidebar, click **Model access**
3. Click **Modify model access**
4. Enable **Amazon Nova Lite** (or whichever model you want to use)
5. Click **Save changes** and wait for the status to show "Access granted" (usually under a minute)

> Without this step the app will fail when generating AI recommendations.

### Step 3: Configure AWS credentials

You have two options:

**Option A: Automated setup (recommended)**

The setup script creates a dedicated IAM user with the correct permissions and generates a `.env` file:

```bash
chmod +x setup-aws.sh
./setup-aws.sh
```

This requires your AWS CLI to be configured with an IAM user/role that has permission to create IAM users and policies.

**Option B: Manual setup**

If you already have AWS credentials with the [required permissions](#required-iam-permissions), copy the example file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:
```
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_REGION=us-east-1
AWS_REGIONS=all
BEDROCK_MODEL_ID=us.amazon.nova-lite-v1:0
```

### Step 4: Build and run

```bash
docker compose up --build
```

Wait for the build to finish — you'll see output like:
```
app-1  |  * Running on http://0.0.0.0:5000
```

### Step 5: Open the app

Navigate to **http://localhost:5000** in your browser.

Click **Analyze My AWS Account** to start your first scan. The initial scan takes 1-2 minutes depending on how many regions and resources you have.

## Required IAM Permissions

The setup script (`setup-aws.sh`) creates an IAM user with:

- **AWS Managed Policy: `ReadOnlyAccess`** — read-only access to all AWS services (EC2, S3, RDS, Lambda, CloudWatch, Cost Explorer, etc.)
- **Custom Policy: `AWSCostOptimizerBedrockAccess`** — adds:
  - `bedrock:InvokeModel*`, `bedrock:Converse*` — AI analysis via Bedrock Nova
  - `bedrock:List*`, `bedrock:Get*` — scan provisioned throughput, custom models, guardrails
  - `bedrock-agent:List*`, `bedrock-agent:Get*` — scan knowledge bases and agents
  - `cloudtrail:LookupEvents` — Bedrock management event attribution
  - `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` — identify Lambda/ECS roles with Bedrock permissions

If you're setting up permissions manually, attach the `ReadOnlyAccess` managed policy and create a custom policy with the Bedrock, CloudTrail, and IAM actions listed above. See [`setup-aws.sh`](setup-aws.sh) for the exact policy document.

## Configuration

All configuration is done via environment variables in the `.env` file.

| Variable | Default | Description |
|----------|---------|-------------|
| `AWS_ACCESS_KEY_ID` | (required) | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | (required) | Your AWS secret key |
| `AWS_REGION` | `us-east-1` | Primary region (used for Cost Explorer and Bedrock) |
| `AWS_REGIONS` | `all` | Regions to scan: `all`, or comma-separated list (e.g. `us-east-1,us-west-2`) |
| `BEDROCK_MODEL_ID` | `us.amazon.nova-lite-v1:0` | Bedrock model to use for AI analysis |
| `CROSS_ACCOUNT_ROLE_NAME` | `CostOptimizerReadOnly` | IAM role name to assume in member accounts (multi-account only) |

### Multi-Region Scanning

Resources are scanned in parallel across regions. The UI shows both total counts and per-region breakdowns.

```bash
# Scan all enabled regions (default)
AWS_REGIONS=all

# Scan specific regions only
AWS_REGIONS=us-east-1,us-west-2,eu-west-1

# Single region
AWS_REGIONS=us-east-1
```

### Multi-Account Setup (AWS Organizations)

If your AWS credentials have Organizations access, the tool automatically discovers all accounts and shows an account selector in the UI.

**Setup cross-account roles in member accounts:**

Option A — Run the setup script in each member account:
```bash
./setup-org-role.sh <management-account-id>
```

Option B — Deploy via CloudFormation StackSet (recommended for many accounts):
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

This section only appears when Bedrock usage exists in the account.

### Analysis History

Analysis results are automatically saved as markdown files in `./data/history/`. History persists across container restarts via the Docker volume mount. You can view and download past analyses from the web UI.

## Development

To run without Docker for local development:

```bash
# Python 3.10+ required
python3 -m venv venv
source venv/bin/activate    # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Set environment variables (or create .env file)
cp .env.example .env
# Edit .env with your credentials

python app.py
```

The app starts on `http://localhost:5000` by default.

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
├── cloudformation/           # CloudFormation templates
│   └── cross-account-role.yaml
├── static/                   # Frontend assets (JS, CSS)
├── templates/                # HTML templates
├── data/                     # Persistent data (gitignored)
│   ├── cache/                # Scan result cache
│   └── history/              # Analysis history (markdown)
├── .env.example              # Template for environment variables
├── setup-aws.sh              # Automated IAM setup
├── setup-org-role.sh         # Cross-account role setup
├── destroy-aws.sh            # Cleanup single-account IAM resources
└── destroy-org-role.sh       # Cleanup cross-account role
```

## Troubleshooting

**"AWS credentials not configured" in the UI**
- Verify your `.env` file exists and contains valid `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
- If you changed `.env`, restart the container: `docker compose down && docker compose up --build`

**"Error calling Bedrock" or empty recommendations**
- Make sure you enabled the Bedrock model in the AWS Console (see [Step 2](#step-2-enable-bedrock-model-access))
- Verify the `BEDROCK_MODEL_ID` in `.env` matches the model you enabled
- Bedrock is only available in certain regions — `us-east-1` and `us-west-2` have the widest model selection

**Scan takes a long time**
- Scanning all regions checks 25+ resource types across every enabled region. Set `AWS_REGIONS` to specific regions to speed it up
- The first scan is always slowest. Results are cached for subsequent views

**"AccessDenied" errors in logs**
- The app gracefully skips services it can't access, but if you see many of these, your IAM user may be missing the `ReadOnlyAccess` policy
- Some services (Bedrock, SageMaker) may not be available in all regions — these errors are safe to ignore

**Multi-account: "Error scanning account"**
- Verify the cross-account role exists in the target account (`./setup-org-role.sh`)
- The role name must match `CROSS_ACCOUNT_ROLE_NAME` in `.env` (default: `CostOptimizerReadOnly`)

## Cleanup

To remove all AWS resources created by the setup scripts:

```bash
# Remove the IAM user and policies
./destroy-aws.sh

# Remove cross-account roles (run in each member account)
./destroy-org-role.sh
```

## Security Notes

- The `.env` file contains sensitive credentials — never commit it to git
- The setup script creates a user with **read-only** access plus Bedrock invoke — no write, delete, or modify permissions
- Consider running in an isolated network environment
- Use the **Demo Mode** toggle in the UI to obfuscate sensitive values before screen sharing

## License

MIT
