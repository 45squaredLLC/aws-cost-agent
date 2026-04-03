#!/bin/bash

# AWS Cost Optimizer - Cross-Account Role Cleanup Script
# Removes the CostOptimizerReadOnly role from a member account.
#
# Usage: ./destroy-org-role.sh [role-name]

set -e

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

ROLE_NAME="${1:-CostOptimizerReadOnly}"

if ! command -v aws &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not installed${NC}"
    exit 1
fi

if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured${NC}"
    exit 1
fi

# Confirmation helper: skips prompt with -y or errors on non-interactive stdin
confirm() {
    local prompt="$1"
    if [ "$YES_FLAG" = true ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        echo -e "${RED}Error: Running non-interactively without -y/--yes flag.${NC}"
        echo "Use -y to skip confirmation prompts:"
        echo "  ./destroy-org-role.sh -y $ROLE_NAME"
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
echo "AWS Cost Optimizer - Remove Cross-Account Role"
echo "=================================="
echo ""
echo "Account:   $CURRENT_ACCOUNT"
echo "Role Name: $ROLE_NAME"
echo ""

# Check if role exists
if ! aws iam get-role --role-name "$ROLE_NAME" &> /dev/null; then
    echo -e "${YELLOW}Role '$ROLE_NAME' does not exist. Nothing to do.${NC}"
    exit 0
fi

confirm "Delete role '$ROLE_NAME'? This cannot be undone. (y/n) "

echo ""
echo "Step 1: Detaching policies..."

ATTACHED_POLICIES=$(aws iam list-attached-role-policies --role-name "$ROLE_NAME" --query 'AttachedPolicies[].PolicyArn' --output text)
for POLICY in $ATTACHED_POLICIES; do
    echo "  Detaching: $POLICY"
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY"
done

echo -e "${GREEN}✓ Policies detached${NC}"
echo ""

echo "Step 2: Deleting role..."
aws iam delete-role --role-name "$ROLE_NAME"

echo -e "${GREEN}✓ Role deleted${NC}"
echo ""

echo "=================================="
echo -e "${GREEN}Cleanup Complete!${NC}"
echo "=================================="
echo ""
echo "Role '$ROLE_NAME' has been removed from account $CURRENT_ACCOUNT."
echo ""
