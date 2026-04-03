#!/bin/bash

# AWS Cost Optimizer - Cleanup Script
# This script removes all IAM resources created by setup-aws.sh

set -e  # Exit on error

echo "=================================="
echo "AWS Cost Optimizer - Cleanup"
echo "=================================="
echo ""

# Configuration
IAM_USER_NAME="aws-cost-optimizer"
BEDROCK_POLICY_NAME="AWSCostOptimizerBedrockAccess"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed${NC}"
    exit 1
fi

# Check if AWS CLI is configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured${NC}"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "AWS Account ID: $ACCOUNT_ID"
echo ""

# Warning prompt
echo -e "${RED}WARNING: This will permanently delete:${NC}"
echo "  • IAM User: $IAM_USER_NAME"
echo "  • Custom Bedrock Policy: $BEDROCK_POLICY_NAME"
echo "  • AWS Managed Policy attachment: ReadOnlyAccess"
echo "  • All associated access keys"
echo "  • .env file (optional)"
echo ""
read -p "Are you sure you want to continue? (yes/no) " -r
echo

if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo "Aborted."
    exit 1
fi

echo ""

# Step 1: Delete Access Keys
echo "Step 1: Deleting Access Keys..."

if aws iam get-user --user-name "$IAM_USER_NAME" &> /dev/null; then
    ACCESS_KEYS=$(aws iam list-access-keys \
        --user-name "$IAM_USER_NAME" \
        --query 'AccessKeyMetadata[].AccessKeyId' \
        --output text)
    
    if [ -n "$ACCESS_KEYS" ]; then
        for KEY in $ACCESS_KEYS; do
            echo "  Deleting access key: $KEY"
            aws iam delete-access-key \
                --user-name "$IAM_USER_NAME" \
                --access-key-id "$KEY"
        done
        echo -e "${GREEN}✓ Access keys deleted${NC}"
    else
        echo "  No access keys found"
    fi
else
    echo -e "${YELLOW}User '$IAM_USER_NAME' does not exist${NC}"
fi

echo ""

# Step 2: Detach Policies
echo "Step 2: Detaching Policies from User..."

if aws iam get-user --user-name "$IAM_USER_NAME" &> /dev/null; then
    ATTACHED_POLICIES=$(aws iam list-attached-user-policies \
        --user-name "$IAM_USER_NAME" \
        --query 'AttachedPolicies[].PolicyArn' \
        --output text)
    
    if [ -n "$ATTACHED_POLICIES" ]; then
        for POLICY_ARN in $ATTACHED_POLICIES; do
            POLICY_DISPLAY_NAME=$(echo "$POLICY_ARN" | rev | cut -d'/' -f1 | rev)
            echo "  Detaching policy: $POLICY_DISPLAY_NAME"
            aws iam detach-user-policy \
                --user-name "$IAM_USER_NAME" \
                --policy-arn "$POLICY_ARN"
        done
        echo -e "${GREEN}✓ Policies detached${NC}"
    else
        echo "  No policies attached"
    fi
fi

echo ""

# Step 3: Delete User
echo "Step 3: Deleting IAM User..."

if aws iam get-user --user-name "$IAM_USER_NAME" &> /dev/null; then
    aws iam delete-user --user-name "$IAM_USER_NAME"
    echo -e "${GREEN}✓ User deleted: $IAM_USER_NAME${NC}"
else
    echo -e "${YELLOW}User '$IAM_USER_NAME' does not exist${NC}"
fi

echo ""

# Step 4: Delete Policy
echo "Step 4: Deleting Custom Bedrock Policy..."

BEDROCK_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${BEDROCK_POLICY_NAME}"

if aws iam get-policy --policy-arn "$BEDROCK_POLICY_ARN" &> /dev/null; then
    
    # Delete all non-default versions first
    echo "  Deleting policy versions..."
    VERSIONS=$(aws iam list-policy-versions \
        --policy-arn "$BEDROCK_POLICY_ARN" \
        --query 'Versions[?IsDefaultVersion==`false`].VersionId' \
        --output text)
    
    for VERSION in $VERSIONS; do
        echo "    Deleting version: $VERSION"
        aws iam delete-policy-version \
            --policy-arn "$BEDROCK_POLICY_ARN" \
            --version-id "$VERSION"
    done
    
    # Delete the policy
    aws iam delete-policy --policy-arn "$BEDROCK_POLICY_ARN"
    echo -e "${GREEN}✓ Bedrock policy deleted: $BEDROCK_POLICY_NAME${NC}"
else
    echo -e "${YELLOW}Bedrock policy '$BEDROCK_POLICY_NAME' does not exist${NC}"
fi

# Note: ReadOnlyAccess is an AWS managed policy and doesn't need to be deleted
echo "  Note: ReadOnlyAccess is an AWS managed policy (not deleted)"

echo ""

# Step 5: Delete .env file (optional)
echo "Step 5: Cleaning up .env file..."
echo ""

if [ -f ".env" ]; then
    read -p "Delete .env file? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Backup first
        if [ ! -d ".backups" ]; then
            mkdir -p .backups
        fi
        BACKUP_FILE=".backups/.env.backup.$(date +%Y%m%d_%H%M%S)"
        cp .env "$BACKUP_FILE"
        echo "  Backup created: $BACKUP_FILE"
        
        rm .env
        echo -e "${GREEN}✓ .env file deleted${NC}"
    else
        echo -e "${YELLOW}Keeping .env file${NC}"
    fi
else
    echo "  No .env file found"
fi

echo ""
echo "=================================="
echo -e "${GREEN}Cleanup Complete!${NC}"
echo "=================================="
echo ""
echo "Removed Resources:"
echo "  • IAM User: $IAM_USER_NAME"
echo "  • Custom Policy: $BEDROCK_POLICY_NAME"
echo "  • AWS Managed Policy: ReadOnlyAccess (detached)"
echo "  • Access Keys: All associated keys"
echo ""

if [ -f "$BACKUP_FILE" ]; then
    echo -e "${YELLOW}Note: .env backup saved to: $BACKUP_FILE${NC}"
    echo ""
fi

echo "Your AWS account has been cleaned up."
echo ""