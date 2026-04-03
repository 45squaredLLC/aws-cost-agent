import boto3
import os
import time
import threading


class AccountManager:
    """Discovers AWS Organization accounts and provides cross-account sessions."""

    def __init__(self):
        self.is_org_mode = False
        self.management_account_id = None
        self.accounts = []
        self._role_name = os.getenv('CROSS_ACCOUNT_ROLE_NAME', 'CostOptimizerReadOnly')
        self._session_cache = {}
        self._cache_lock = threading.Lock()
        self._discover()

    def _discover(self):
        """Try Organizations API. Fall back to single-account via STS."""
        try:
            org_client = boto3.client('organizations', region_name=os.getenv('AWS_REGION', 'us-east-1'))
            paginator = org_client.get_paginator('list_accounts')

            accounts = []
            for page in paginator.paginate():
                for acct in page['Accounts']:
                    accounts.append({
                        'id': acct['Id'],
                        'name': acct.get('Name', acct['Id']),
                        'email': acct.get('Email', ''),
                        'status': acct.get('Status', 'UNKNOWN')
                    })

            if accounts:
                self.is_org_mode = True
                self.accounts = [a for a in accounts if a['status'] == 'ACTIVE']

                # Determine management account
                try:
                    org_info = org_client.describe_organization()
                    self.management_account_id = org_info['Organization']['MasterAccountId']
                except Exception:
                    sts = boto3.client('sts')
                    self.management_account_id = sts.get_caller_identity()['Account']

                print(f"Organization mode: {len(self.accounts)} active accounts discovered")
                return

        except Exception as e:
            error_str = str(e)
            if 'AccessDenied' not in error_str and 'AWSOrganizationsNotInUseException' not in error_str:
                print(f"Unexpected error checking Organizations: {error_str}")

        # Fall back to single-account mode
        try:
            sts = boto3.client('sts')
            identity = sts.get_caller_identity()
            account_id = identity['Account']
            self.management_account_id = account_id
            self.accounts = [{
                'id': account_id,
                'name': f'Account {account_id}',
                'email': '',
                'status': 'ACTIVE'
            }]
            print(f"Single-account mode: {account_id}")
        except Exception as e:
            print(f"Error getting caller identity: {e}")
            self.accounts = []

    def get_session(self, account_id=None):
        """Return default session or AssumeRole session. Cache by account_id."""
        if account_id is None or account_id == self.management_account_id:
            return boto3.Session()

        with self._cache_lock:
            if account_id in self._session_cache:
                session, expiry = self._session_cache[account_id]
                if time.time() < expiry:
                    return session

        # Assume role into member account
        sts = boto3.client('sts')
        role_arn = f'arn:aws:iam::{account_id}:role/{self._role_name}'

        try:
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=f'CostOptimizer-{account_id}',
                DurationSeconds=3600
            )
            creds = response['Credentials']
            session = boto3.Session(
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken']
            )

            # Cache with 5 min buffer before expiry
            expiry = time.time() + 3300  # 55 minutes
            with self._cache_lock:
                self._session_cache[account_id] = (session, expiry)

            return session

        except Exception as e:
            print(f"Error assuming role for account {account_id}: {e}")
            raise

    def get_accounts(self):
        """Return discovered accounts list."""
        return self.accounts
