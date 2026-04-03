import boto3
from datetime import datetime, timedelta
import os


class CostAnalyzer:
    """
    Analyzes AWS costs using Cost Explorer API
    """

    def __init__(self, account_manager=None):
        self.account_manager = account_manager
        self.client = boto3.client(
            'ce',
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
    
    def get_cost_summary(self, days=30):
        """
        Get cost summary for the last N days
        
        Args:
            days: Number of days to analyze (default 30)
            
        Returns:
            dict: Cost summary data
        """
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        
        try:
            # Get cost and usage data
            response = self.client.get_cost_and_usage(
                TimePeriod={
                    'Start': start_date.strftime('%Y-%m-%d'),
                    'End': end_date.strftime('%Y-%m-%d')
                },
                Granularity='MONTHLY',
                Metrics=['UnblendedCost'],
                GroupBy=[
                    {
                        'Type': 'DIMENSION',
                        'Key': 'SERVICE'
                    }
                ]
            )
            
            # Process results
            services = {}
            total_cost = 0.0
            
            for result in response['ResultsByTime']:
                for group in result['Groups']:
                    service = group['Keys'][0]
                    cost = float(group['Metrics']['UnblendedCost']['Amount'])
                    
                    if service in services:
                        services[service] += cost
                    else:
                        services[service] = cost
                    
                    total_cost += cost
            
            # Sort services by cost (descending)
            sorted_services = sorted(
                services.items(),
                key=lambda x: x[1],
                reverse=True
            )
            
            return {
                "total_cost": round(total_cost, 2),
                "period_days": days,
                "top_services": [
                    {"service": s[0], "cost": round(s[1], 2)}
                    for s in sorted_services[:10]
                ],
                "start_date": start_date.strftime('%Y-%m-%d'),
                "end_date": end_date.strftime('%Y-%m-%d')
            }
            
        except Exception as e:
            print(f"Error fetching cost data: {str(e)}")
            return {
                "error": str(e),
                "total_cost": 0,
                "period_days": days,
                "top_services": []
            }

    def get_org_cost_summary(self, days=30, account_ids=None):
        """
        Get cost summary across multiple accounts using a single CE call
        grouped by LINKED_ACCOUNT and SERVICE.

        Args:
            days: Number of days to analyze
            account_ids: Optional list of account IDs to filter

        Returns:
            dict: Cost summary with per-account breakdown
        """
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        try:
            kwargs = {
                'TimePeriod': {
                    'Start': start_date.strftime('%Y-%m-%d'),
                    'End': end_date.strftime('%Y-%m-%d')
                },
                'Granularity': 'MONTHLY',
                'Metrics': ['UnblendedCost'],
                'GroupBy': [
                    {'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'},
                    {'Type': 'DIMENSION', 'Key': 'SERVICE'}
                ]
            }

            if account_ids:
                kwargs['Filter'] = {
                    'Dimensions': {
                        'Key': 'LINKED_ACCOUNT',
                        'Values': account_ids
                    }
                }

            response = self.client.get_cost_and_usage(**kwargs)

            # Process results: {account_id: {total_cost, services: {name: cost}}}
            account_costs = {}
            org_total = 0.0

            for result in response['ResultsByTime']:
                for group in result['Groups']:
                    account_id = group['Keys'][0]
                    service = group['Keys'][1]
                    cost = float(group['Metrics']['UnblendedCost']['Amount'])

                    if account_id not in account_costs:
                        account_costs[account_id] = {
                            'total_cost': 0.0,
                            'services': {}
                        }

                    account_costs[account_id]['total_cost'] += cost
                    if service in account_costs[account_id]['services']:
                        account_costs[account_id]['services'][service] += cost
                    else:
                        account_costs[account_id]['services'][service] = cost

                    org_total += cost

            # Build account name mapping
            account_names = {}
            if self.account_manager:
                for acct in self.account_manager.get_accounts():
                    account_names[acct['id']] = acct['name']

            # Format output
            formatted = {}
            for acct_id, data in account_costs.items():
                sorted_services = sorted(
                    data['services'].items(), key=lambda x: x[1], reverse=True
                )
                formatted[acct_id] = {
                    'account_name': account_names.get(acct_id, acct_id),
                    'total_cost': round(data['total_cost'], 2),
                    'top_services': [
                        {'service': s[0], 'cost': round(s[1], 2)}
                        for s in sorted_services[:10]
                    ]
                }

            return {
                'org_total_cost': round(org_total, 2),
                'period_days': days,
                'start_date': start_date.strftime('%Y-%m-%d'),
                'end_date': end_date.strftime('%Y-%m-%d'),
                'account_costs': formatted
            }

        except Exception as e:
            print(f"Error fetching org cost data: {str(e)}")
            return {
                'error': str(e),
                'org_total_cost': 0,
                'period_days': days,
                'account_costs': {}
            }