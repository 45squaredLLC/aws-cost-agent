#!/bin/bash

# AWS Cost Optimizer - IAM Setup Script
# This script creates an IAM user with read-only access across all AWS services

set -e  # Exit on error

echo "=================================="
echo "AWS Cost Optimizer - IAM Setup"
echo "=================================="
echo ""

# Configuration
IAM_USER_NAME="aws-cost-optimizer"
BEDROCK_POLICY_NAME="AWSCostOptimizerBedrockAccess"
REGION=${AWS_REGION:-us-east-1}
BEDROCK_MODEL="us.amazon.nova-lite-v1:0"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed${NC}"
    echo "Please install it from: https://aws.amazon.com/cli/"
    exit 1
fi

# Check if AWS CLI is configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured${NC}"
    echo "Please run: aws configure"
    exit 1
fi

echo -e "${GREEN}✓ AWS CLI is configured${NC}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account ID: $ACCOUNT_ID"
echo ""

# Confirm before proceeding
read -p "This will create IAM user '$IAM_USER_NAME' with full read-only access. Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""
echo "Step 1: Creating Bedrock Access Policy..."

# Create small custom policy for Bedrock (not included in ReadOnlyAccess)
BEDROCK_POLICY_JSON=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockAccess",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:List*",
        "bedrock:Get*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "BedrockAgentReadOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock-agent:List*",
        "bedrock-agent:Get*"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CallerAttribution",
      "Effect": "Allow",
      "Action": [
        "cloudtrail:LookupEvents",
        "iam:ListAttachedRolePolicies",
        "iam:ListRolePolicies",
        "iam:GetRolePolicy"
      ],
      "Resource": "*"
    }
  ]
}
EOF
)

# Check if Bedrock policy already exists and delete it
if aws iam get-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${BEDROCK_POLICY_NAME}" &> /dev/null; then
    echo -e "${YELLOW}Bedrock policy already exists. Deleting old version...${NC}"
    
    # Get all policy versions except default
    VERSIONS=$(aws iam list-policy-versions --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${BEDROCK_POLICY_NAME}" \
        --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text)
    
    # Delete non-default versions
    for VERSION in $VERSIONS; do
        aws iam delete-policy-version \
            --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${BEDROCK_POLICY_NAME}" \
            --version-id "$VERSION" 2>/dev/null || true
    done
    
    # Delete the policy
    aws iam delete-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/${BEDROCK_POLICY_NAME}" 2>/dev/null || true
    sleep 2
fi

# Create Bedrock policy
BEDROCK_POLICY_ARN=$(aws iam create-policy \
    --policy-name "$BEDROCK_POLICY_NAME" \
    --policy-document "$BEDROCK_POLICY_JSON" \
    --description "Bedrock InvokeModel permissions for AWS Cost Optimizer" \
    --query 'Policy.Arn' \
    --output text)

echo -e "${GREEN}✓ Bedrock policy created: $BEDROCK_POLICY_ARN${NC}"
echo ""

echo "Step 2: Creating IAM User..."

# Check if user already exists
if aws iam get-user --user-name "$IAM_USER_NAME" &> /dev/null; then
    echo -e "${YELLOW}User '$IAM_USER_NAME' already exists.${NC}"
    
    # Delete existing access keys
    EXISTING_KEYS=$(aws iam list-access-keys --user-name "$IAM_USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text)
    for KEY in $EXISTING_KEYS; do
        echo "Deleting existing access key: $KEY"
        aws iam delete-access-key --user-name "$IAM_USER_NAME" --access-key-id "$KEY"
    done
    
    # Detach existing policies
    ATTACHED_POLICIES=$(aws iam list-attached-user-policies --user-name "$IAM_USER_NAME" --query 'AttachedPolicies[].PolicyArn' --output text)
    for POLICY in $ATTACHED_POLICIES; do
        echo "Detaching policy: $POLICY"
        aws iam detach-user-policy --user-name "$IAM_USER_NAME" --policy-arn "$POLICY"
    done
else
    # Create the user
    aws iam create-user --user-name "$IAM_USER_NAME" --tags "Key=Purpose,Value=CostOptimizer"
    echo -e "${GREEN}✓ User created: $IAM_USER_NAME${NC}"
fi

echo ""
echo "Step 3: Attaching AWS Managed ReadOnlyAccess Policy..."

# Attach AWS managed ReadOnlyAccess policy (provides read-only to ALL AWS services)
aws iam attach-user-policy \
    --user-name "$IAM_USER_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"

echo -e "${GREEN}✓ ReadOnlyAccess policy attached${NC}"
echo ""

echo "Step 4: Attaching Bedrock Policy..."

# Attach custom Bedrock policy
aws iam attach-user-policy \
    --user-name "$IAM_USER_NAME" \
    --policy-arn "$BEDROCK_POLICY_ARN"

echo -e "${GREEN}✓ Bedrock policy attached${NC}"
echo ""

echo "Step 5: Creating Access Keys..."

# Create access key
ACCESS_KEY_JSON=$(aws iam create-access-key --user-name "$IAM_USER_NAME")
ACCESS_KEY_ID=$(echo "$ACCESS_KEY_JSON" | grep -o '"AccessKeyId": "[^"]*' | cut -d'"' -f4)
SECRET_ACCESS_KEY=$(echo "$ACCESS_KEY_JSON" | grep -o '"SecretAccessKey": "[^"]*' | cut -d'"' -f4)

echo -e "${GREEN}✓ Access keys created${NC}"
echo ""

echo "Step 6: Generating .env file..."

# Generate .env file
cat > .env <<EOF
# AWS Cost Optimizer Configuration
# Generated on $(date)

# AWS Credentials
AWS_ACCESS_KEY_ID=$ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$SECRET_ACCESS_KEY
AWS_REGION=$REGION

# Multi-Region Configuration
# Comma-separated list of regions to scan, or 'all' for all enabled regions
# Examples: us-east-1,us-west-2,eu-west-1
AWS_REGIONS=all

# Bedrock Configuration
BEDROCK_MODEL_ID=$BEDROCK_MODEL

# Optional: AWS Account Info
AWS_ACCOUNT_ID=$ACCOUNT_ID
IAM_USER_NAME=$IAM_USER_NAME
EOF

chmod 600 .env

echo -e "${GREEN}✓ .env file created${NC}"
echo ""

echo "=================================="
echo -e "${GREEN}Setup Complete!${NC}"
echo "=================================="
echo ""
echo "Created Resources:"
echo "  • IAM User: $IAM_USER_NAME"
echo "  • AWS Managed Policy: ReadOnlyAccess (full read-only access to ALL AWS services)"
echo "  • Custom Bedrock Policy: $BEDROCK_POLICY_NAME"
echo "  • Access Key: $ACCESS_KEY_ID"
echo "  • .env file: $(pwd)/.env"
echo ""
echo "Permissions Summary:"
echo "  ✓ Full read-only access across ALL AWS services (EC2, S3, RDS, Lambda, etc.)"
echo "  ✓ Bedrock InvokeModel/Converse for AI analysis"
echo "  ✓ Bedrock + Bedrock Agent read-only for resource scanning"
echo "  ✓ CloudTrail LookupEvents + IAM role inspection for Bedrock caller attribution"
echo "  ✓ No write, create, delete, or modify permissions"
echo ""
echo -e "${YELLOW}IMPORTANT SECURITY NOTES:${NC}"
echo "  • Your .env file contains sensitive credentials"
echo "  • Keep it secure and never commit it to git"
echo "  • The .env file has been set to read-only (600)"
echo ""
echo "Next Steps:"
echo "  1. Enable Bedrock model access in AWS Console:"
echo "     → Go to Amazon Bedrock → Model access"
echo "     → Enable 'Amazon Nova Lite' model"
echo "     → Wait 1-2 minutes for approval"
echo ""
echo "  2. Run the application:"
echo "     → docker compose up --build"
echo ""
echo "  3. Open browser:"
echo "     → http://localhost:5000"
echo ""
echo "To remove all created resources, run:"
echo "  ./destroy-aws.sh"
echo ""
echo "=================================="
echo "Multi-Account Setup (Optional)"
echo "=================================="
echo ""
echo "To analyze costs across all accounts in your AWS Organization:"
echo ""
echo "  Option A: Run the setup script in each member account:"
echo "    ./setup-org-role.sh $ACCOUNT_ID"
echo ""
echo "  Option B: Deploy via CloudFormation StackSet (recommended for many accounts):"
echo "    aws cloudformation create-stack-set \\"
echo "      --stack-set-name CostOptimizerRole \\"
echo "      --template-body file://cloudformation/cross-account-role.yaml \\"
echo "      --parameters ParameterKey=ManagementAccountId,ParameterValue=$ACCOUNT_ID \\"
echo "      --capabilities CAPABILITY_NAMED_IAM"
echo ""
echo "  Then set in .env (optional):"
echo "    CROSS_ACCOUNT_ROLE_NAME=CostOptimizerReadOnly"
echo ""