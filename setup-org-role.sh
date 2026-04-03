#!/bin/bash

# AWS Cost Optimizer - Cross-Account Role Setup Script
# Run this in each member account to create a read-only role
# that the management account can assume for cost analysis.
#
# Usage: ./setup-org-role.sh <management-account-id> [role-name]

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Parse flags
YES_FLAG=false
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES_FLAG=true; shift ;;
    esac
done

MANAGEMENT_ACCOUNT_ID="$1"
ROLE_NAME="${2:-CostOptimizerReadOnly}"

if [ -z "$MANAGEMENT_ACCOUNT_ID" ]; then
    echo -e "${RED}Usage: ./setup-org-role.sh [-y] <management-account-id> [role-name]${NC}"
    echo ""
    echo "  -y, --yes              Skip confirmation prompts"
    echo "  management-account-id  The AWS account ID that will assume this role"
    echo "  role-name              Role name (default: CostOptimizerReadOnly)"
    exit 1
fi

# Validate account ID format
if ! [[ "$MANAGEMENT_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
    echo -e "${RED}Error: Management account ID must be a 12-digit number${NC}"
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed${NC}"
    exit 1
fi

if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured${NC}"
    exit 1
fi

# Confirmation helper: skips prompt with -y or non-interactive stdin
confirm() {
    local prompt="$1"
    if [ "$YES_FLAG" = true ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        echo -e "${RED}Error: Running non-interactively without -y/--yes flag.${NC}"
        echo "Use -y to skip confirmation prompts:"
        echo "  ./setup-org-role.sh -y $MANAGEMENT_ACCOUNT_ID $ROLE_NAME"
        exit 1
    fi
    read -p "$prompt" -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
}

CURRENT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "=================================="
echo "AWS Cost Optimizer - Cross-Account Role Setup"
echo "=================================="
echo ""
echo "Current Account:    $CURRENT_ACCOUNT"
echo "Management Account: $MANAGEMENT_ACCOUNT_ID"
echo "Role Name:          $ROLE_NAME"
echo ""

if [ "$CURRENT_ACCOUNT" = "$MANAGEMENT_ACCOUNT_ID" ]; then
    echo -e "${YELLOW}Warning: You are running this in the management account.${NC}"
    echo "This script is meant to be run in member accounts."
    confirm "Continue anyway? (y/n) "
fi

confirm "Create role '$ROLE_NAME' trusting account $MANAGEMENT_ACCOUNT_ID? (y/n) "

echo ""
echo "Step 1: Creating trust policy..."

TRUST_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::${MANAGEMENT_ACCOUNT_ID}:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {}
    }
  ]
}
EOF
)

# Check if role exists
if aws iam get-role --role-name "$ROLE_NAME" &> /dev/null; then
    echo -e "${YELLOW}Role '$ROLE_NAME' already exists. Updating trust policy...${NC}"
    aws iam update-assume-role-policy \
        --role-name "$ROLE_NAME" \
        --policy-document "$TRUST_POLICY"
else
    echo "Creating role..."
    aws iam create-role \
        --role-name "$ROLE_NAME" \
        --assume-role-policy-document "$TRUST_POLICY" \
        --description "Read-only role for AWS Cost Optimizer cross-account analysis" \
        --tags "Key=Purpose,Value=CostOptimizer" "Key=ManagedBy,Value=aws-cost-optimizer"
fi

echo -e "${GREEN}✓ Role created/updated${NC}"
echo ""

echo "Step 2: Attaching ReadOnlyAccess policy..."

aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn "arn:aws:iam::aws:policy/ReadOnlyAccess"

echo -e "${GREEN}✓ ReadOnlyAccess policy attached${NC}"
echo ""

echo "Step 3: Creating Bedrock & attribution inline policy..."

BEDROCK_INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BedrockReadOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock:List*",
        "bedrock:Get*",
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

aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name "CostOptimizerBedrockReadOnly" \
    --policy-document "$BEDROCK_INLINE_POLICY"

echo -e "${GREEN}✓ Bedrock & attribution policy attached${NC}"
echo ""

echo "=================================="
echo -e "${GREEN}Setup Complete!${NC}"
echo "=================================="
echo ""
echo "Role ARN: arn:aws:iam::${CURRENT_ACCOUNT}:role/${ROLE_NAME}"
echo ""
echo "The management account ($MANAGEMENT_ACCOUNT_ID) can now assume this role"
echo "to perform read-only cost analysis on this account."
echo ""
echo "To remove this role, run:"
echo "  ./destroy-org-role.sh $ROLE_NAME"
echo ""
