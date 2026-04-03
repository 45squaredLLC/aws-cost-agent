"""
Custom Strands tools for AWS cost optimization analysis.
These tools provide efficient, purpose-built analysis that would otherwise
require multiple API calls through the generic use_aws tool.
"""

import boto3
from strands.tools import tool

# Global reference set by app.py at startup
_account_manager = None


def _get_session(account_id=None):
    """Get a boto3 session, optionally for a specific account."""
    if account_id and _account_manager:
        return _account_manager.get_session(account_id)
    return boto3.Session()


@tool
def analyze_nat_gateways(region: str = None, account_id: str = None) -> dict:
    """
    Analyze NAT Gateways and identify cost optimization opportunities.

    This tool scans for:
    - All NAT Gateways and their costs ($0.045/hr + $0.045/GB processed)
    - VPCs with NAT Gateways but missing free S3/DynamoDB Gateway Endpoints
    - Potential savings from adding VPC Gateway Endpoints

    Args:
        region: AWS region to scan. If not provided, scans the default region.
        account_id: AWS account ID to analyze. If not provided, uses default credentials.

    Returns:
        Dictionary containing NAT Gateway analysis and recommendations.
    """
    session = _get_session(account_id)
    default_region = region or session.region_name or 'us-east-1'

    try:
        ec2 = session.client('ec2', region_name=default_region)

        # Get all NAT Gateways
        nat_response = ec2.describe_nat_gateways(
            Filters=[{'Name': 'state', 'Values': ['available']}]
        )
        nat_gateways = nat_response.get('NatGateways', [])

        # Get all VPC Endpoints
        endpoints_response = ec2.describe_vpc_endpoints()
        vpc_endpoints = endpoints_response.get('VpcEndpoints', [])

        # Build a map of VPC -> endpoints
        vpc_endpoint_map = {}
        for ep in vpc_endpoints:
            vpc_id = ep['VpcId']
            if vpc_id not in vpc_endpoint_map:
                vpc_endpoint_map[vpc_id] = []
            vpc_endpoint_map[vpc_id].append({
                'id': ep['VpcEndpointId'],
                'service': ep['ServiceName'],
                'type': ep['VpcEndpointType']
            })

        # Analyze each NAT Gateway
        results = {
            'region': default_region,
            'nat_gateways': [],
            'vpcs_missing_endpoints': [],
            'total_nat_hourly_cost': 0,
            'recommendations': []
        }

        vpcs_with_nat = set()

        for nat in nat_gateways:
            vpc_id = nat['VpcId']
            subnet_id = nat['SubnetId']
            nat_id = nat['NatGatewayId']
            vpcs_with_nat.add(vpc_id)

            # NAT Gateway costs $0.045/hour = ~$32.40/month
            hourly_cost = 0.045
            monthly_cost = hourly_cost * 24 * 30
            results['total_nat_hourly_cost'] += hourly_cost

            results['nat_gateways'].append({
                'id': nat_id,
                'vpc_id': vpc_id,
                'subnet_id': subnet_id,
                'state': nat['State'],
                'hourly_cost': hourly_cost,
                'estimated_monthly_base_cost': round(monthly_cost, 2),
                'note': 'Plus $0.045/GB for data processed'
            })

        # Check which VPCs with NAT Gateways are missing S3/DynamoDB endpoints
        for vpc_id in vpcs_with_nat:
            endpoints = vpc_endpoint_map.get(vpc_id, [])
            endpoint_services = [ep['service'] for ep in endpoints]

            missing = []
            has_s3_endpoint = any('s3' in svc.lower() for svc in endpoint_services)
            has_dynamodb_endpoint = any('dynamodb' in svc.lower() for svc in endpoint_services)

            if not has_s3_endpoint:
                missing.append('S3')
            if not has_dynamodb_endpoint:
                missing.append('DynamoDB')

            if missing:
                results['vpcs_missing_endpoints'].append({
                    'vpc_id': vpc_id,
                    'missing_gateway_endpoints': missing,
                    'existing_endpoints': endpoints,
                    'potential_savings': 'VPC Gateway Endpoints for S3/DynamoDB are FREE and eliminate NAT data processing charges for those services'
                })

                for service in missing:
                    results['recommendations'].append({
                        'type': 'missing_vpc_endpoint',
                        'vpc_id': vpc_id,
                        'service': service,
                        'description': f'Create a free VPC Gateway Endpoint for {service} in {vpc_id} to eliminate NAT Gateway data processing charges',
                        'cli_command': f'aws ec2 create-vpc-endpoint --vpc-id {vpc_id} --service-name com.amazonaws.{default_region}.{service.lower()} --route-table-ids <route-table-id> --region {default_region}'
                    })

        # Summary
        total_monthly_base = results['total_nat_hourly_cost'] * 24 * 30
        results['summary'] = {
            'total_nat_gateways': len(nat_gateways),
            'total_monthly_base_cost': f'${round(total_monthly_base, 2)}',
            'vpcs_missing_free_endpoints': len(results['vpcs_missing_endpoints']),
            'note': 'Base cost only - actual cost includes $0.045/GB data processing fees'
        }

        return results

    except Exception as e:
        return {
            'error': str(e),
            'region': default_region
        }


@tool
def analyze_data_transfer(region: str = None, account_id: str = None) -> dict:
    """
    Analyze potential data transfer cost issues in AWS infrastructure.

    This tool checks for:
    - Instances in different AZs than their NAT Gateway (causes cross-AZ charges)
    - VPC peering connections (potential cross-region transfer costs)
    - Internet Gateways and their associated resources

    Args:
        region: AWS region to scan. If not provided, scans the default region.
        account_id: AWS account ID to analyze. If not provided, uses default credentials.

    Returns:
        Dictionary containing data transfer analysis and recommendations.
    """
    session = _get_session(account_id)
    default_region = region or session.region_name or 'us-east-1'

    try:
        ec2 = session.client('ec2', region_name=default_region)

        results = {
            'region': default_region,
            'cross_az_issues': [],
            'vpc_peering': [],
            'recommendations': []
        }

        # Get NAT Gateways and their AZs
        nat_response = ec2.describe_nat_gateways(
            Filters=[{'Name': 'state', 'Values': ['available']}]
        )

        # Get subnet info to map subnet -> AZ
        subnets_response = ec2.describe_subnets()
        subnet_az_map = {s['SubnetId']: s['AvailabilityZone'] for s in subnets_response['Subnets']}
        subnet_vpc_map = {s['SubnetId']: s['VpcId'] for s in subnets_response['Subnets']}

        # Map VPC -> NAT Gateway AZs
        vpc_nat_azs = {}
        for nat in nat_response.get('NatGateways', []):
            vpc_id = nat['VpcId']
            subnet_id = nat['SubnetId']
            az = subnet_az_map.get(subnet_id, 'unknown')
            if vpc_id not in vpc_nat_azs:
                vpc_nat_azs[vpc_id] = []
            vpc_nat_azs[vpc_id].append({'nat_id': nat['NatGatewayId'], 'az': az})

        # Get running instances
        instances_response = ec2.describe_instances(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )

        # Check for instances in different AZs than NAT
        for reservation in instances_response.get('Reservations', []):
            for instance in reservation['Instances']:
                instance_id = instance['InstanceId']
                subnet_id = instance.get('SubnetId')
                if not subnet_id:
                    continue

                vpc_id = subnet_vpc_map.get(subnet_id)
                instance_az = subnet_az_map.get(subnet_id)

                if vpc_id in vpc_nat_azs:
                    nat_azs = [n['az'] for n in vpc_nat_azs[vpc_id]]
                    if instance_az and instance_az not in nat_azs:
                        results['cross_az_issues'].append({
                            'instance_id': instance_id,
                            'instance_az': instance_az,
                            'vpc_id': vpc_id,
                            'nat_gateway_azs': nat_azs,
                            'issue': 'Instance uses NAT Gateway in different AZ - incurs cross-AZ data transfer charges ($0.01/GB each way)',
                            'estimated_extra_cost': '$0.02/GB for round-trip traffic'
                        })

        # Check VPC peering connections
        peering_response = ec2.describe_vpc_peering_connections(
            Filters=[{'Name': 'status-code', 'Values': ['active']}]
        )

        for peering in peering_response.get('VpcPeeringConnections', []):
            requester = peering['RequesterVpcInfo']
            accepter = peering['AccepterVpcInfo']

            is_cross_region = requester.get('Region') != accepter.get('Region')

            results['vpc_peering'].append({
                'peering_id': peering['VpcPeeringConnectionId'],
                'requester_vpc': requester.get('VpcId'),
                'requester_region': requester.get('Region'),
                'accepter_vpc': accepter.get('VpcId'),
                'accepter_region': accepter.get('Region'),
                'is_cross_region': is_cross_region,
                'cost_note': 'Cross-region peering: $0.01/GB each direction' if is_cross_region else 'Same-region peering: No data transfer cost within same AZ'
            })

        # Generate recommendations
        if results['cross_az_issues']:
            results['recommendations'].append({
                'type': 'cross_az_nat',
                'description': f"Found {len(results['cross_az_issues'])} instances using NAT Gateways in different AZs",
                'suggestion': 'Consider deploying NAT Gateways in each AZ where you have instances, or move instances to the same AZ as the NAT Gateway',
                'potential_savings': '$0.02/GB on round-trip traffic'
            })

        results['summary'] = {
            'cross_az_issues_count': len(results['cross_az_issues']),
            'vpc_peering_connections': len(results['vpc_peering']),
            'cross_region_peering': sum(1 for p in results['vpc_peering'] if p['is_cross_region'])
        }

        return results

    except Exception as e:
        return {
            'error': str(e),
            'region': default_region
        }


@tool
def get_cost_breakdown(days: int = 30, group_by: str = "SERVICE", account_id: str = None) -> dict:
    """
    Get a detailed cost breakdown from AWS Cost Explorer.

    Args:
        days: Number of days to analyze (default: 30, max: 90)
        group_by: How to group costs - SERVICE, REGION, USAGE_TYPE, or LINKED_ACCOUNT
        account_id: AWS account ID to analyze. If not provided, uses default credentials.

    Returns:
        Dictionary containing cost breakdown by the specified dimension.
    """
    from datetime import datetime, timedelta

    days = min(days, 90)  # Cap at 90 days

    try:
        session = _get_session(account_id)
        ce = session.client('ce', region_name='us-east-1')  # Cost Explorer is global

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        response = ce.get_cost_and_usage(
            TimePeriod={
                'Start': start_date.isoformat(),
                'End': end_date.isoformat()
            },
            Granularity='MONTHLY',
            Metrics=['UnblendedCost'],
            GroupBy=[{'Type': 'DIMENSION', 'Key': group_by}]
        )

        # Process results
        breakdown = []
        total_cost = 0

        for result in response.get('ResultsByTime', []):
            for group in result.get('Groups', []):
                key = group['Keys'][0]
                amount = float(group['Metrics']['UnblendedCost']['Amount'])
                if amount > 0.01:  # Filter out negligible costs
                    breakdown.append({
                        'name': key,
                        'cost': round(amount, 2)
                    })
                    total_cost += amount

        # Sort by cost descending
        breakdown.sort(key=lambda x: x['cost'], reverse=True)

        return {
            'period': f'{start_date} to {end_date}',
            'days': days,
            'group_by': group_by,
            'total_cost': round(total_cost, 2),
            'breakdown': breakdown[:20],  # Top 20
            'currency': 'USD'
        }

    except Exception as e:
        return {
            'error': str(e)
        }
