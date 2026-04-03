import boto3
import json
from datetime import datetime, timedelta
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# All resource types scanned per-region. Adding a new scanner only requires:
# 1. Add the key here
# 2. Add the _scan_<key> method
# 3. Call it in _scan_region
RESOURCE_KEYS = [
    "ec2_instances", "rds_instances",
    "unattached_volumes", "orphaned_snapshots", "unassociated_eips", "stale_amis",
    "idle_load_balancers", "nat_gateways", "cloudwatch_no_retention",
    "gp2_volumes", "idle_rds", "lambda_functions",
    # Database & Cache
    "elasticache_clusters", "opensearch_domains", "dynamodb_tables",
    "rds_read_replicas", "redshift_clusters",
    # Containers
    "ecs_services",
    # Streaming & Analytics
    "kinesis_streams", "glue_jobs",
    # ML
    "sagemaker_resources", "bedrock_resources",
    # Networking & API
    "cloudfront_distributions", "api_gateways", "route53_hosted_zones",
    # Security & Config
    "secrets_manager",
]

# Keys that are NOT orphaned/waste (they're inventory counts, not actionable)
INVENTORY_ONLY_KEYS = {"ec2_instances", "rds_instances"}


class ResourceScanner:
    """
    Scans AWS resources across multiple regions to identify optimization opportunities
    """

    def __init__(self, account_manager=None):
        self.default_region = os.getenv('AWS_REGION', 'us-east-1')
        self.account_manager = account_manager
        self._api_semaphore = threading.Semaphore(10)
        self.regions = self._get_regions_to_scan()

    def _get_regions_to_scan(self, session=None):
        """
        Get list of regions to scan based on configuration
        """
        regions_config = os.getenv('AWS_REGIONS', '').strip()

        if not regions_config or regions_config.lower() == 'all':
            return self._get_all_enabled_regions(session)
        else:
            return [r.strip() for r in regions_config.split(',') if r.strip()]

    def _get_all_enabled_regions(self, session=None):
        """
        Get all AWS regions that are enabled for the account
        """
        try:
            session = session or boto3.Session()
            ec2_client = session.client('ec2', region_name=self.default_region)
            response = ec2_client.describe_regions(
                Filters=[{'Name': 'opt-in-status', 'Values': ['opt-in-not-required', 'opted-in']}]
            )
            regions = [region['RegionName'] for region in response['Regions']]
            print(f"Scanning {len(regions)} enabled regions: {', '.join(regions)}")
            return regions
        except Exception as e:
            print(f"Error getting regions, falling back to default: {str(e)}")
            return [self.default_region]

    def scan_accounts(self, account_ids):
        """Scan multiple accounts in parallel. Returns org-mode result structure."""
        if not self.account_manager:
            raise RuntimeError("AccountManager required for multi-account scanning")

        results = {
            "mode": "organization",
            "accounts": {},
            "org_totals": {k: 0 for k in [*RESOURCE_KEYS, "s3_buckets"]},
            "scan_timestamp": datetime.now().isoformat()
        }

        # Look up account names
        account_names = {}
        for acct in self.account_manager.get_accounts():
            account_names[acct['id']] = acct['name']

        with ThreadPoolExecutor(max_workers=min(len(account_ids), 3)) as executor:
            future_to_account = {
                executor.submit(self._scan_account, acct_id): acct_id
                for acct_id in account_ids
            }
            for future in as_completed(future_to_account):
                acct_id = future_to_account[future]
                try:
                    acct_data = future.result()
                    acct_data['account_name'] = account_names.get(acct_id, acct_id)
                    results['accounts'][acct_id] = acct_data

                    # Aggregate org totals
                    for key in results['org_totals']:
                        results['org_totals'][key] += acct_data.get('totals', {}).get(key, 0)
                except Exception as e:
                    print(f"Error scanning account {acct_id}: {e}")
                    results['accounts'][acct_id] = {
                        'account_name': account_names.get(acct_id, acct_id),
                        'error': str(e),
                        'totals': {k: 0 for k in [*RESOURCE_KEYS, "s3_buckets"]},
                        'orphaned_resources': {k: [] for k in RESOURCE_KEYS if k not in INVENTORY_ONLY_KEYS}
                    }

        return results

    def _scan_account(self, account_id):
        """Scan a single account's resources."""
        try:
            session = self.account_manager.get_session(account_id)
        except Exception as e:
            return {
                'error': str(e),
                'totals': {k: 0 for k in [*RESOURCE_KEYS, "s3_buckets"]},
                'orphaned_resources': {k: [] for k in RESOURCE_KEYS if k not in INVENTORY_ONLY_KEYS}
            }

        regions = self._get_regions_to_scan(session)
        return self._scan_resources_with_session(session, regions)

    def _scan_resources_with_session(self, session, regions):
        """Scan resources using a specific session."""
        totals = {k: 0 for k in RESOURCE_KEYS}
        totals["s3_buckets"] = 0
        orphaned = {k: [] for k in RESOURCE_KEYS if k not in INVENTORY_ONLY_KEYS}

        results = {
            "regions_scanned": regions,
            "by_region": {},
            "totals": totals,
            "orphaned_resources": orphaned,
            "scan_timestamp": datetime.now().isoformat()
        }

        with ThreadPoolExecutor(max_workers=min(len(regions), 5)) as executor:
            future_to_region = {
                executor.submit(self._scan_region, region, session): region
                for region in regions
            }
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    region_data = future.result()
                    results["by_region"][region] = region_data
                    for key in RESOURCE_KEYS:
                        items = region_data.get(key, [])
                        results["totals"][key] += len(items)
                        if key in results["orphaned_resources"]:
                            results["orphaned_resources"][key].extend(items)
                except Exception as e:
                    print(f"Error scanning region {region}: {str(e)}")
                    results["by_region"][region] = {"error": str(e)}

        # S3 is global, scan once
        results["s3_buckets"] = self._scan_s3_buckets(session)
        results["totals"]["s3_buckets"] = len(results["s3_buckets"])

        # Create flattened lists for backward compatibility
        results["ec2_instances"] = self._flatten_ec2_instances(results["by_region"])
        results["rds_instances"] = self._flatten_rds_instances(results["by_region"])

        # Bedrock deep-dive: CloudWatch usage metrics + CloudTrail attribution
        # Always attempt — on-demand usage won't show in CloudWatch list_metrics
        # but will appear in CloudTrail. The scan returns empty data if nothing found.
        results["bedrock_usage"] = self._scan_bedrock_usage(session, results["regions_scanned"])

        return results

    def scan_resources(self):
        """
        Scan all major resource types across all configured regions (single-account mode).

        Returns:
            dict: Resource inventory organized by region
        """
        return self._scan_resources_with_session(boto3.Session(), self.regions)
    
    def _scan_region(self, region, session=None):
        """
        Scan a single region for resources
        """
        print(f"Scanning region: {region}")
        session = session or boto3.Session()

        # Create EC2 client once for the region
        ec2_client = session.client('ec2', region_name=region)

        # Get existing volume IDs for orphan detection
        with self._api_semaphore:
            volumes_response = ec2_client.describe_volumes()
        existing_volume_ids = {v['VolumeId'] for v in volumes_response.get('Volumes', [])}

        # Get AMIs and build a set of snapshot IDs used by AMIs (single API call)
        with self._api_semaphore:
            amis_response = ec2_client.describe_images(Owners=['self'])
        ami_snapshot_ids = set()
        for ami in amis_response.get('Images', []):
            for block_device in ami.get('BlockDeviceMappings', []):
                ebs = block_device.get('Ebs', {})
                if ebs.get('SnapshotId'):
                    ami_snapshot_ids.add(ebs['SnapshotId'])

        # Run all scanners in parallel within the region
        scanners = {
            "ec2_instances": lambda: self._scan_ec2_instances(region, session),
            "rds_instances": lambda: self._scan_rds_instances(region, session),
            "unattached_volumes": lambda: self._scan_unattached_volumes(region, ec2_client),
            "orphaned_snapshots": lambda: self._scan_orphaned_snapshots(region, ec2_client, existing_volume_ids, ami_snapshot_ids, session),
            "unassociated_eips": lambda: self._scan_unassociated_eips(region, ec2_client),
            "stale_amis": lambda: self._scan_stale_amis(region, ec2_client),
            "idle_load_balancers": lambda: self._scan_idle_load_balancers(region, session),
            "nat_gateways": lambda: self._scan_nat_gateways(region, ec2_client),
            "cloudwatch_no_retention": lambda: self._scan_cloudwatch_no_retention(region, session),
            "gp2_volumes": lambda: self._scan_gp2_volumes(region, ec2_client),
            "idle_rds": lambda: self._scan_idle_rds(region, session),
            "lambda_functions": lambda: self._scan_lambda_functions(region, session),
            "elasticache_clusters": lambda: self._scan_elasticache(region, session),
            "opensearch_domains": lambda: self._scan_opensearch(region, session),
            "dynamodb_tables": lambda: self._scan_dynamodb(region, session),
            "rds_read_replicas": lambda: self._scan_rds_read_replicas(region, session),
            "redshift_clusters": lambda: self._scan_redshift(region, session),
            "ecs_services": lambda: self._scan_ecs(region, session),
            "kinesis_streams": lambda: self._scan_kinesis(region, session),
            "glue_jobs": lambda: self._scan_glue_jobs(region, session),
            "sagemaker_resources": lambda: self._scan_sagemaker(region, session),
            "bedrock_resources": lambda: self._scan_bedrock(region, session),
            "cloudfront_distributions": lambda: self._scan_cloudfront(region, session),
            "api_gateways": lambda: self._scan_api_gateway(region, session),
            "route53_hosted_zones": lambda: self._scan_route53(region, session),
            "secrets_manager": lambda: self._scan_secrets_manager(region, session),
        }

        results = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_key = {
                executor.submit(fn): key for key, fn in scanners.items()
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    print(f"Error in {key} scanner for {region}: {e}")
                    results[key] = []

        return results
    
    def _scan_ec2_instances(self, region, session=None):
        """Scan EC2 instances in a specific region"""
        instances = []
        session = session or boto3.Session()

        try:
            ec2_client = session.client('ec2', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            
            response = ec2_client.describe_instances()
            
            for reservation in response['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    
                    # Get CPU utilization
                    cpu_util = self._get_cpu_utilization(instance_id, cloudwatch)
                    
                    instances.append({
                        "id": instance_id,
                        "type": instance['InstanceType'],
                        "state": instance['State']['Name'],
                        "cpu_utilization": cpu_util,
                        "launch_time": instance['LaunchTime'].isoformat(),
                        "region": region,
                        "tags": {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                    })
        
        except Exception as e:
            print(f"Error scanning EC2 in {region}: {str(e)}")
        
        return instances
    
    def _scan_rds_instances(self, region, session=None):
        """Scan RDS instances in a specific region"""
        instances = []
        session = session or boto3.Session()

        try:
            rds_client = session.client('rds', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            response = rds_client.describe_db_instances()

            for db in response['DBInstances']:
                db_id = db['DBInstanceIdentifier']

                # Get CPU utilization
                cpu_util = None
                if db['DBInstanceStatus'] == 'available':
                    try:
                        cw_resp = cloudwatch.get_metric_statistics(
                            Namespace='AWS/RDS',
                            MetricName='CPUUtilization',
                            Dimensions=[{'Name': 'DBInstanceIdentifier', 'Value': db_id}],
                            StartTime=datetime.now() - timedelta(days=7),
                            EndTime=datetime.now(),
                            Period=86400,
                            Statistics=['Average']
                        )
                        if cw_resp['Datapoints']:
                            cpu_util = round(
                                sum(d['Average'] for d in cw_resp['Datapoints']) / len(cw_resp['Datapoints']), 2
                            )
                    except Exception:
                        pass

                instances.append({
                    "id": db_id,
                    "class": db['DBInstanceClass'],
                    "engine": db['Engine'],
                    "status": db['DBInstanceStatus'],
                    "storage_gb": db['AllocatedStorage'],
                    "multi_az": db['MultiAZ'],
                    "region": region,
                    "cpu_utilization": cpu_util
                })
        
        except Exception as e:
            print(f"Error scanning RDS in {region}: {str(e)}")
        
        return instances
    
    def _scan_s3_buckets(self, session=None):
        """Scan S3 buckets (S3 is global)"""
        buckets = []
        session = session or boto3.Session()

        try:
            s3_client = session.client('s3')
            response = s3_client.list_buckets()
            
            for bucket in response['Buckets']:
                bucket_name = bucket['Name']
                
                # Get bucket region
                try:
                    location = s3_client.get_bucket_location(Bucket=bucket_name)
                    region = location['LocationConstraint'] or 'us-east-1'
                except:
                    region = 'unknown'
                
                # Check lifecycle configuration
                has_lifecycle = False
                try:
                    s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                    has_lifecycle = True
                except s3_client.exceptions.ClientError:
                    pass
                except Exception:
                    pass

                # Check intelligent tiering
                has_intelligent_tiering = False
                try:
                    it_response = s3_client.list_bucket_intelligent_tiering_configurations(Bucket=bucket_name)
                    if it_response.get('IntelligentTieringConfigurationList'):
                        has_intelligent_tiering = True
                except Exception:
                    pass

                buckets.append({
                    "name": bucket_name,
                    "creation_date": bucket['CreationDate'].isoformat(),
                    "region": region,
                    "has_lifecycle": has_lifecycle,
                    "has_intelligent_tiering": has_intelligent_tiering
                })
        
        except Exception as e:
            print(f"Error scanning S3: {str(e)}")
        
        return buckets
    
    def _scan_unattached_volumes(self, region, ec2_client):
        """Scan for EBS volumes not attached to any instance"""
        volumes = []

        try:
            response = ec2_client.describe_volumes(
                Filters=[{'Name': 'status', 'Values': ['available']}]
            )

            for volume in response.get('Volumes', []):
                # Calculate monthly cost estimate ($0.10/GB for gp2, varies by type)
                volume_type = volume.get('VolumeType', 'gp2')
                size_gb = volume.get('Size', 0)
                # Approximate pricing per GB/month
                price_per_gb = {'gp2': 0.10, 'gp3': 0.08, 'io1': 0.125, 'io2': 0.125, 'st1': 0.045, 'sc1': 0.025, 'standard': 0.05}
                monthly_cost = size_gb * price_per_gb.get(volume_type, 0.10)

                volumes.append({
                    "id": volume['VolumeId'],
                    "size_gb": size_gb,
                    "volume_type": volume_type,
                    "state": volume['State'],
                    "created": volume['CreateTime'].isoformat(),
                    "region": region,
                    "estimated_monthly_cost": round(monthly_cost, 2),
                    "tags": {tag['Key']: tag['Value'] for tag in volume.get('Tags', [])}
                })

        except Exception as e:
            print(f"Error scanning unattached volumes in {region}: {str(e)}")

        return volumes

    def _scan_orphaned_snapshots(self, region, ec2_client, existing_volume_ids, ami_snapshot_ids, session=None):
        """Scan for EBS snapshots not linked to existing volumes or AMIs"""
        orphaned = []
        session = session or boto3.Session()

        try:
            # Get account ID for filtering own snapshots
            sts_client = session.client('sts')
            account_id = sts_client.get_caller_identity()['Account']

            response = ec2_client.describe_snapshots(OwnerIds=[account_id])

            for snapshot in response.get('Snapshots', []):
                volume_id = snapshot.get('VolumeId', '')
                snapshot_id = snapshot['SnapshotId']

                # Check if volume still exists
                volume_exists = volume_id in existing_volume_ids if volume_id else False

                # Check if snapshot is used by any AMI (O(1) lookup)
                ami_uses_snapshot = snapshot_id in ami_snapshot_ids

                # If volume doesn't exist and no AMI uses it, it's orphaned
                if not volume_exists and not ami_uses_snapshot:
                    size_gb = snapshot.get('VolumeSize', 0)
                    # Snapshot storage costs ~$0.05/GB/month
                    monthly_cost = size_gb * 0.05

                    orphaned.append({
                        "id": snapshot_id,
                        "volume_id": volume_id or "deleted",
                        "size_gb": size_gb,
                        "started": snapshot['StartTime'].isoformat(),
                        "description": snapshot.get('Description', '')[:100],
                        "region": region,
                        "estimated_monthly_cost": round(monthly_cost, 2),
                        "tags": {tag['Key']: tag['Value'] for tag in snapshot.get('Tags', [])}
                    })

        except Exception as e:
            print(f"Error scanning orphaned snapshots in {region}: {str(e)}")

        return orphaned

    def _scan_unassociated_eips(self, region, ec2_client):
        """Scan for Elastic IPs not associated with any instance or network interface"""
        unassociated = []

        try:
            response = ec2_client.describe_addresses()

            for address in response.get('Addresses', []):
                # EIP is unassociated if it has no InstanceId and no NetworkInterfaceId
                if not address.get('InstanceId') and not address.get('NetworkInterfaceId'):
                    # Unassociated EIPs cost $0.005/hour = ~$3.60/month
                    unassociated.append({
                        "public_ip": address['PublicIp'],
                        "allocation_id": address.get('AllocationId', 'N/A'),
                        "domain": address.get('Domain', 'vpc'),
                        "region": region,
                        "estimated_monthly_cost": 3.60,
                        "tags": {tag['Key']: tag['Value'] for tag in address.get('Tags', [])}
                    })

        except Exception as e:
            print(f"Error scanning unassociated EIPs in {region}: {str(e)}")

        return unassociated

    def _scan_stale_amis(self, region, ec2_client, days_threshold=90):
        """Scan for AMIs older than threshold with no recent launches"""
        stale = []

        try:
            response = ec2_client.describe_images(Owners=['self'])
            cutoff_date = datetime.now() - timedelta(days=days_threshold)

            for ami in response.get('Images', []):
                # Parse creation date
                creation_date_str = ami.get('CreationDate', '')
                if not creation_date_str:
                    continue

                try:
                    # AWS format: 2024-01-15T10:30:00.000Z
                    creation_date = datetime.fromisoformat(creation_date_str.replace('Z', '+00:00')).replace(tzinfo=None)
                except:
                    continue

                if creation_date < cutoff_date:
                    # Estimate storage cost based on snapshot sizes
                    total_snapshot_size = 0
                    for block_device in ami.get('BlockDeviceMappings', []):
                        ebs = block_device.get('Ebs', {})
                        total_snapshot_size += ebs.get('VolumeSize', 0)

                    # Snapshot storage ~$0.05/GB/month
                    monthly_cost = total_snapshot_size * 0.05

                    stale.append({
                        "id": ami['ImageId'],
                        "name": ami.get('Name', 'Unnamed'),
                        "description": ami.get('Description', '')[:100],
                        "created": creation_date_str,
                        "age_days": (datetime.now() - creation_date).days,
                        "snapshot_size_gb": total_snapshot_size,
                        "region": region,
                        "estimated_monthly_cost": round(monthly_cost, 2),
                        "state": ami.get('State', 'unknown')
                    })

        except Exception as e:
            print(f"Error scanning stale AMIs in {region}: {str(e)}")

        return stale

    def _scan_idle_load_balancers(self, region, session=None):
        """Scan for ELBv2 (ALB/NLB) and Classic LBs with no healthy targets or no traffic."""
        idle = []
        session = session or boto3.Session()

        # ALB / NLB
        try:
            elbv2 = session.client('elbv2', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)

            lbs = elbv2.describe_load_balancers().get('LoadBalancers', [])
            for lb in lbs:
                lb_arn = lb['LoadBalancerArn']
                lb_name = lb.get('LoadBalancerName', '')
                lb_type = lb.get('Type', 'application')

                # Check target groups for healthy targets
                tg_response = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)
                has_healthy = False
                total_targets = 0

                for tg in tg_response.get('TargetGroups', []):
                    health = elbv2.describe_target_health(TargetGroupArn=tg['TargetGroupArn'])
                    targets = health.get('TargetHealthDescriptions', [])
                    total_targets += len(targets)
                    if any(t['TargetHealth']['State'] == 'healthy' for t in targets):
                        has_healthy = True

                # Check request count over last 7 days
                metric_name = 'RequestCount' if lb_type == 'application' else 'ActiveFlowCount'
                namespace = 'AWS/ApplicationELB' if lb_type == 'application' else 'AWS/NetworkELB'
                # Extract the ALB/NLB suffix for the dimension
                lb_suffix = '/'.join(lb_arn.split(':loadbalancer/')[1:]) if ':loadbalancer/' in lb_arn else ''

                request_count = 0
                try:
                    cw_response = cloudwatch.get_metric_statistics(
                        Namespace=namespace,
                        MetricName=metric_name,
                        Dimensions=[{'Name': 'LoadBalancer', 'Value': lb_suffix}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(),
                        Period=604800,
                        Statistics=['Sum']
                    )
                    if cw_response['Datapoints']:
                        request_count = sum(d['Sum'] for d in cw_response['Datapoints'])
                except Exception:
                    pass

                is_idle = not has_healthy or (total_targets == 0) or (request_count == 0)

                if is_idle:
                    # ALB ~$16.20/mo base, NLB ~$6.75/mo base
                    monthly_cost = 16.20 if lb_type == 'application' else 6.75
                    idle.append({
                        "name": lb_name,
                        "arn": lb_arn,
                        "type": lb_type,
                        "state": lb.get('State', {}).get('Code', 'unknown'),
                        "total_targets": total_targets,
                        "has_healthy_targets": has_healthy,
                        "request_count_7d": int(request_count),
                        "region": region,
                        "reason": "no targets" if total_targets == 0 else ("no healthy targets" if not has_healthy else "zero traffic (7d)"),
                        "estimated_monthly_cost": monthly_cost
                    })

        except Exception as e:
            print(f"Error scanning load balancers in {region}: {str(e)}")

        # Classic LBs
        try:
            elb = session.client('elb', region_name=region)
            classic_lbs = elb.describe_load_balancers().get('LoadBalancerDescriptions', [])
            for clb in classic_lbs:
                clb_name = clb['LoadBalancerName']
                instance_ids = [i['InstanceId'] for i in clb.get('Instances', [])]
                if len(instance_ids) == 0:
                    idle.append({
                        "name": clb_name,
                        "arn": f"classic:{clb_name}",
                        "type": "classic",
                        "total_targets": 0,
                        "has_healthy_targets": False,
                        "request_count_7d": 0,
                        "region": region,
                        "reason": "no instances registered",
                        "estimated_monthly_cost": 18.00
                    })
        except Exception as e:
            print(f"Error scanning classic LBs in {region}: {str(e)}")

        return idle

    def _scan_nat_gateways(self, region, ec2_client):
        """Scan NAT Gateways — always a cost concern at $32+/mo each."""
        gateways = []

        try:
            response = ec2_client.describe_nat_gateways(
                Filters=[{'Name': 'state', 'Values': ['available']}]
            )
            for nat in response.get('NatGateways', []):
                # $0.045/hr = ~$32.40/mo base + $0.045/GB processed
                gateways.append({
                    "id": nat['NatGatewayId'],
                    "vpc_id": nat['VpcId'],
                    "subnet_id": nat['SubnetId'],
                    "state": nat['State'],
                    "region": region,
                    "estimated_monthly_cost": 32.40
                })

        except Exception as e:
            print(f"Error scanning NAT Gateways in {region}: {str(e)}")

        return gateways

    def _scan_cloudwatch_no_retention(self, region, session=None):
        """Scan CloudWatch Log Groups with no retention policy (never expire = unbounded cost)."""
        no_retention = []
        session = session or boto3.Session()

        try:
            logs_client = session.client('logs', region_name=region)
            paginator = logs_client.get_paginator('describe_log_groups')

            for page in paginator.paginate():
                for lg in page.get('logGroups', []):
                    if 'retentionInDays' not in lg:
                        stored_bytes = lg.get('storedBytes', 0)
                        stored_gb = stored_bytes / (1024 ** 3)
                        # CloudWatch Logs storage: $0.03/GB/month
                        monthly_cost = stored_gb * 0.03

                        no_retention.append({
                            "name": lg['logGroupName'],
                            "stored_bytes": stored_bytes,
                            "stored_gb": round(stored_gb, 2),
                            "region": region,
                            "estimated_monthly_cost": round(monthly_cost, 2)
                        })

        except Exception as e:
            print(f"Error scanning CloudWatch Log Groups in {region}: {str(e)}")

        return no_retention

    def _scan_gp2_volumes(self, region, ec2_client):
        """Scan for gp2 EBS volumes that could be upgraded to gp3 (20% cheaper, better perf)."""
        gp2_vols = []

        try:
            response = ec2_client.describe_volumes(
                Filters=[{'Name': 'volume-type', 'Values': ['gp2']}]
            )
            for vol in response.get('Volumes', []):
                size_gb = vol.get('Size', 0)
                # gp2 = $0.10/GB, gp3 = $0.08/GB — saves $0.02/GB/mo
                current_cost = size_gb * 0.10
                gp3_cost = size_gb * 0.08
                savings = current_cost - gp3_cost

                gp2_vols.append({
                    "id": vol['VolumeId'],
                    "size_gb": size_gb,
                    "state": vol['State'],
                    "attached": len(vol.get('Attachments', [])) > 0,
                    "region": region,
                    "current_monthly_cost": round(current_cost, 2),
                    "gp3_monthly_cost": round(gp3_cost, 2),
                    "estimated_monthly_cost": round(savings, 2)
                })

        except Exception as e:
            print(f"Error scanning gp2 volumes in {region}: {str(e)}")

        return gp2_vols

    def _scan_idle_rds(self, region, session=None):
        """Scan for RDS instances with near-zero CPU utilization (likely idle)."""
        idle = []
        session = session or boto3.Session()

        try:
            rds_client = session.client('rds', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)

            response = rds_client.describe_db_instances()
            for db in response.get('DBInstances', []):
                if db['DBInstanceStatus'] != 'available':
                    continue

                db_id = db['DBInstanceIdentifier']

                # Check average CPU over last 7 days
                try:
                    cw_response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/RDS',
                        MetricName='CPUUtilization',
                        Dimensions=[{'Name': 'DBInstanceIdentifier', 'Value': db_id}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(),
                        Period=86400,
                        Statistics=['Average']
                    )
                    if cw_response['Datapoints']:
                        avg_cpu = sum(d['Average'] for d in cw_response['Datapoints']) / len(cw_response['Datapoints'])
                    else:
                        avg_cpu = 0
                except Exception:
                    avg_cpu = None

                # Check database connections over last 7 days
                total_connections = None
                try:
                    cw_response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/RDS',
                        MetricName='DatabaseConnections',
                        Dimensions=[{'Name': 'DBInstanceIdentifier', 'Value': db_id}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(),
                        Period=604800,
                        Statistics=['Sum']
                    )
                    if cw_response['Datapoints']:
                        total_connections = sum(d['Sum'] for d in cw_response['Datapoints'])
                except Exception:
                    pass

                is_idle = False
                reason = ""
                if avg_cpu is not None and avg_cpu < 2.0:
                    is_idle = True
                    reason = f"avg CPU {avg_cpu:.1f}% over 7 days"
                if total_connections is not None and total_connections == 0:
                    is_idle = True
                    reason = "zero connections over 7 days"

                if is_idle:
                    idle.append({
                        "id": db_id,
                        "class": db['DBInstanceClass'],
                        "engine": db['Engine'],
                        "storage_gb": db['AllocatedStorage'],
                        "multi_az": db['MultiAZ'],
                        "avg_cpu_7d": round(avg_cpu, 1) if avg_cpu is not None else None,
                        "total_connections_7d": int(total_connections) if total_connections is not None else None,
                        "region": region,
                        "reason": reason,
                        "estimated_monthly_cost": 0  # varies too much by instance class
                    })

        except Exception as e:
            print(f"Error scanning idle RDS in {region}: {str(e)}")

        return idle

    def _scan_lambda_functions(self, region, session=None):
        """Scan Lambda functions for unused or over-provisioned functions."""
        functions = []
        session = session or boto3.Session()

        try:
            lambda_client = session.client('lambda', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)

            paginator = lambda_client.get_paginator('list_functions')
            for page in paginator.paginate():
                for fn in page.get('Functions', []):
                    fn_name = fn['FunctionName']
                    memory_mb = fn.get('MemorySize', 128)
                    runtime = fn.get('Runtime', 'unknown')
                    code_size = fn.get('CodeSize', 0)
                    last_modified = fn.get('LastModified', '')

                    # Check invocations over last 30 days
                    invocations = 0
                    avg_duration = None
                    try:
                        cw_response = cloudwatch.get_metric_statistics(
                            Namespace='AWS/Lambda',
                            MetricName='Invocations',
                            Dimensions=[{'Name': 'FunctionName', 'Value': fn_name}],
                            StartTime=datetime.now() - timedelta(days=30),
                            EndTime=datetime.now(),
                            Period=2592000,
                            Statistics=['Sum']
                        )
                        if cw_response['Datapoints']:
                            invocations = int(sum(d['Sum'] for d in cw_response['Datapoints']))
                    except Exception:
                        pass

                    try:
                        cw_response = cloudwatch.get_metric_statistics(
                            Namespace='AWS/Lambda',
                            MetricName='Duration',
                            Dimensions=[{'Name': 'FunctionName', 'Value': fn_name}],
                            StartTime=datetime.now() - timedelta(days=30),
                            EndTime=datetime.now(),
                            Period=2592000,
                            Statistics=['Average']
                        )
                        if cw_response['Datapoints']:
                            avg_duration = round(cw_response['Datapoints'][0]['Average'], 1)
                    except Exception:
                        pass

                    # Flag if: no invocations in 30d, or memory seems over-provisioned
                    issues = []
                    if invocations == 0:
                        issues.append("zero invocations (30d)")
                    if avg_duration and memory_mb >= 512 and avg_duration < 100:
                        issues.append(f"possibly over-provisioned ({memory_mb}MB, avg {avg_duration}ms)")

                    if issues:
                        functions.append({
                            "name": fn_name,
                            "runtime": runtime,
                            "memory_mb": memory_mb,
                            "code_size_bytes": code_size,
                            "invocations_30d": invocations,
                            "avg_duration_ms": avg_duration,
                            "last_modified": last_modified,
                            "region": region,
                            "issues": issues,
                            "estimated_monthly_cost": 0  # minimal unless high invocations
                        })

        except Exception as e:
            print(f"Error scanning Lambda functions in {region}: {str(e)}")

        return functions

    # ── Database & Cache ──────────────────────────────────────────────

    def _scan_elasticache(self, region, session=None):
        """Scan ElastiCache clusters for idle nodes (low CPU/connections)."""
        idle = []
        session = session or boto3.Session()
        try:
            client = session.client('elasticache', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            response = client.describe_cache_clusters(ShowCacheNodeInfo=True)

            for cluster in response.get('CacheClusters', []):
                cluster_id = cluster['CacheClusterId']
                node_type = cluster.get('CacheNodeType', 'unknown')
                engine = cluster.get('Engine', 'unknown')
                num_nodes = cluster.get('NumCacheNodes', 0)

                # Check CPU over 7 days
                avg_cpu = None
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/ElastiCache',
                        MetricName='CPUUtilization',
                        Dimensions=[{'Name': 'CacheClusterId', 'Value': cluster_id}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(),
                        Period=86400, Statistics=['Average']
                    )
                    if cw['Datapoints']:
                        avg_cpu = round(sum(d['Average'] for d in cw['Datapoints']) / len(cw['Datapoints']), 1)
                except Exception:
                    pass

                is_idle = avg_cpu is not None and avg_cpu < 2.0
                if is_idle:
                    idle.append({
                        "id": cluster_id, "node_type": node_type, "engine": engine,
                        "num_nodes": num_nodes, "avg_cpu_7d": avg_cpu,
                        "region": region, "reason": f"avg CPU {avg_cpu}% over 7 days",
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning ElastiCache in {region}: {e}")
        return idle

    def _scan_opensearch(self, region, session=None):
        """Scan OpenSearch/Elasticsearch domains for idle clusters."""
        idle = []
        session = session or boto3.Session()
        try:
            client = session.client('opensearch', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            names = client.list_domain_names().get('DomainNames', [])

            for entry in names:
                name = entry['DomainName']
                try:
                    info = client.describe_domain(DomainName=name)['DomainStatus']
                except Exception:
                    continue

                instance_type = info.get('ClusterConfig', {}).get('InstanceType', 'unknown')
                instance_count = info.get('ClusterConfig', {}).get('InstanceCount', 0)

                # Check search rate over 7 days
                search_rate = None
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/ES', MetricName='SearchRate',
                        Dimensions=[{'Name': 'DomainName', 'Value': name}, {'Name': 'ClientId', 'Value': info.get('DomainId', '').split('/')[0]}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        search_rate = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                is_idle = search_rate is not None and search_rate < 10
                if is_idle:
                    idle.append({
                        "name": name, "instance_type": instance_type,
                        "instance_count": instance_count,
                        "search_requests_7d": int(search_rate) if search_rate else 0,
                        "region": region, "reason": "near-zero search activity (7d)",
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning OpenSearch in {region}: {e}")
        return idle

    def _scan_dynamodb(self, region, session=None):
        """Scan DynamoDB tables for unused or over-provisioned tables."""
        issues = []
        session = session or boto3.Session()
        try:
            client = session.client('dynamodb', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            paginator = client.get_paginator('list_tables')

            for page in paginator.paginate():
                for table_name in page.get('TableNames', []):
                    try:
                        desc = client.describe_table(TableName=table_name)['Table']
                    except Exception:
                        continue

                    billing = desc.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
                    size_bytes = desc.get('TableSizeBytes', 0)
                    item_count = desc.get('ItemCount', 0)
                    size_gb = round(size_bytes / (1024 ** 3), 2)

                    # Check consumed read/write over 7 days
                    total_consumed = 0
                    for metric in ['ConsumedReadCapacityUnits', 'ConsumedWriteCapacityUnits']:
                        try:
                            cw = cloudwatch.get_metric_statistics(
                                Namespace='AWS/DynamoDB', MetricName=metric,
                                Dimensions=[{'Name': 'TableName', 'Value': table_name}],
                                StartTime=datetime.now() - timedelta(days=7),
                                EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                            )
                            if cw['Datapoints']:
                                total_consumed += sum(d['Sum'] for d in cw['Datapoints'])
                        except Exception:
                            pass

                    table_issues = []
                    if total_consumed < 10:
                        table_issues.append("near-zero read/write activity (7d)")
                    if billing == 'PROVISIONED':
                        prov_read = desc.get('ProvisionedThroughput', {}).get('ReadCapacityUnits', 0)
                        prov_write = desc.get('ProvisionedThroughput', {}).get('WriteCapacityUnits', 0)
                        if total_consumed < 10 and (prov_read > 5 or prov_write > 5):
                            table_issues.append(f"provisioned R:{prov_read}/W:{prov_write} but unused")

                    if table_issues:
                        # DynamoDB storage: $0.25/GB/month
                        storage_cost = size_gb * 0.25
                        issues.append({
                            "name": table_name, "billing_mode": billing,
                            "item_count": item_count, "size_gb": size_gb,
                            "consumed_capacity_7d": round(total_consumed),
                            "region": region, "issues": table_issues,
                            "estimated_monthly_cost": round(storage_cost, 2)
                        })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning DynamoDB in {region}: {e}")
        return issues

    def _scan_rds_read_replicas(self, region, session=None):
        """Scan for RDS read replicas with zero replica lag readers (unused)."""
        unused = []
        session = session or boto3.Session()
        try:
            rds = session.client('rds', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            response = rds.describe_db_instances()

            for db in response.get('DBInstances', []):
                # Only look at read replicas
                if not db.get('ReadReplicaSourceDBInstanceIdentifier'):
                    continue
                db_id = db['DBInstanceIdentifier']

                # Check if anyone is reading from this replica
                read_iops = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/RDS', MetricName='ReadIOPS',
                        Dimensions=[{'Name': 'DBInstanceIdentifier', 'Value': db_id}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Average']
                    )
                    if cw['Datapoints']:
                        read_iops = round(cw['Datapoints'][0]['Average'], 1)
                except Exception:
                    pass

                if read_iops < 1:
                    unused.append({
                        "id": db_id, "class": db['DBInstanceClass'],
                        "engine": db['Engine'],
                        "source": db['ReadReplicaSourceDBInstanceIdentifier'],
                        "avg_read_iops_7d": read_iops,
                        "region": region, "reason": "read replica with near-zero read IOPS",
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning RDS read replicas in {region}: {e}")
        return unused

    def _scan_redshift(self, region, session=None):
        """Scan Redshift clusters for idle clusters."""
        idle = []
        session = session or boto3.Session()
        try:
            client = session.client('redshift', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            response = client.describe_clusters()

            for cluster in response.get('Clusters', []):
                cluster_id = cluster['ClusterIdentifier']
                node_type = cluster.get('NodeType', 'unknown')
                num_nodes = cluster.get('NumberOfNodes', 0)

                # Check query activity
                queries = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/Redshift', MetricName='DatabaseConnections',
                        Dimensions=[{'Name': 'ClusterIdentifier', 'Value': cluster_id}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        queries = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                if queries < 5:
                    idle.append({
                        "id": cluster_id, "node_type": node_type,
                        "num_nodes": num_nodes,
                        "connections_7d": int(queries),
                        "region": region, "reason": "near-zero connections (7d)",
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning Redshift in {region}: {e}")
        return idle

    # ── Containers ────────────────────────────────────────────────────

    def _scan_ecs(self, region, session=None):
        """Scan ECS for services with zero running tasks or zero CPU."""
        issues = []
        session = session or boto3.Session()
        try:
            ecs = session.client('ecs', region_name=region)
            clusters = ecs.list_clusters().get('clusterArns', [])

            for cluster_arn in clusters:
                cluster_name = cluster_arn.split('/')[-1]
                svc_arns = ecs.list_services(cluster=cluster_arn).get('serviceArns', [])
                if not svc_arns:
                    continue

                services = ecs.describe_services(cluster=cluster_arn, services=svc_arns[:10]).get('services', [])
                for svc in services:
                    running = svc.get('runningCount', 0)
                    desired = svc.get('desiredCount', 0)
                    svc_name = svc.get('serviceName', '')
                    launch_type = svc.get('launchType', 'EC2')

                    svc_issues = []
                    if desired == 0 and running == 0:
                        svc_issues.append("desired=0, running=0 (inactive service)")
                    elif running == 0 and desired > 0:
                        svc_issues.append(f"desired={desired} but running=0 (failing)")

                    if svc_issues:
                        issues.append({
                            "cluster": cluster_name, "service": svc_name,
                            "launch_type": launch_type,
                            "desired_count": desired, "running_count": running,
                            "region": region, "issues": svc_issues,
                            "estimated_monthly_cost": 0
                        })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning ECS in {region}: {e}")
        return issues

    # ── Streaming & Analytics ─────────────────────────────────────────

    def _scan_kinesis(self, region, session=None):
        """Scan Kinesis streams for idle streams (zero records)."""
        idle = []
        session = session or boto3.Session()
        try:
            client = session.client('kinesis', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            streams = client.list_streams().get('StreamNames', [])

            for stream_name in streams:
                try:
                    desc = client.describe_stream_summary(StreamName=stream_name)['StreamDescriptionSummary']
                except Exception:
                    continue

                shard_count = desc.get('OpenShardCount', 0)

                # Check incoming records over 7 days
                records = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/Kinesis', MetricName='IncomingRecords',
                        Dimensions=[{'Name': 'StreamName', 'Value': stream_name}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        records = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                if records < 10:
                    # $0.04/shard/hour = ~$28.80/shard/month (on-demand)
                    monthly_cost = shard_count * 28.80
                    idle.append({
                        "name": stream_name, "shard_count": shard_count,
                        "incoming_records_7d": int(records),
                        "region": region, "reason": "near-zero incoming records (7d)",
                        "estimated_monthly_cost": round(monthly_cost, 2)
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning Kinesis in {region}: {e}")
        return idle

    def _scan_glue_jobs(self, region, session=None):
        """Scan Glue jobs for consistently failing or unused jobs."""
        issues = []
        session = session or boto3.Session()
        try:
            client = session.client('glue', region_name=region)
            jobs = client.get_jobs().get('Jobs', [])

            for job in jobs:
                job_name = job['Name']
                worker_type = job.get('WorkerType', 'Standard')
                num_workers = job.get('NumberOfWorkers', 2)

                # Get recent runs
                try:
                    runs = client.get_job_runs(JobName=job_name, MaxResults=5).get('JobRuns', [])
                except Exception:
                    runs = []

                if not runs:
                    issues.append({
                        "name": job_name, "worker_type": worker_type,
                        "num_workers": num_workers, "recent_runs": 0,
                        "region": region, "issues": ["no runs found (unused job definition)"],
                        "estimated_monthly_cost": 0
                    })
                else:
                    failed_count = sum(1 for r in runs if r.get('JobRunState') == 'FAILED')
                    if failed_count == len(runs):
                        issues.append({
                            "name": job_name, "worker_type": worker_type,
                            "num_workers": num_workers,
                            "recent_runs": len(runs), "failed_runs": failed_count,
                            "region": region, "issues": [f"all {failed_count} recent runs failed"],
                            "estimated_monthly_cost": 0
                        })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning Glue jobs in {region}: {e}")
        return issues

    # ── ML ────────────────────────────────────────────────────────────

    def _scan_sagemaker(self, region, session=None):
        """Scan SageMaker for idle notebook instances and endpoints."""
        issues = []
        session = session or boto3.Session()
        try:
            sm = session.client('sagemaker', region_name=region)

            # Notebook instances
            notebooks = sm.list_notebook_instances().get('NotebookInstances', [])
            for nb in notebooks:
                status = nb.get('NotebookInstanceStatus', '')
                if status == 'InService':
                    instance_type = nb.get('InstanceType', 'unknown')
                    issues.append({
                        "type": "notebook", "name": nb['NotebookInstanceName'],
                        "instance_type": instance_type, "status": status,
                        "region": region,
                        "issues": ["running notebook instance (charges while InService)"],
                        "estimated_monthly_cost": 0
                    })

            # Endpoints
            endpoints = sm.list_endpoints().get('Endpoints', [])
            cloudwatch = session.client('cloudwatch', region_name=region)
            for ep in endpoints:
                ep_name = ep['EndpointName']
                # Check invocations
                invocations = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/SageMaker', MetricName='Invocations',
                        Dimensions=[{'Name': 'EndpointName', 'Value': ep_name}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        invocations = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                if invocations < 10:
                    issues.append({
                        "type": "endpoint", "name": ep_name,
                        "status": ep.get('EndpointStatus', 'unknown'),
                        "invocations_7d": int(invocations),
                        "region": region,
                        "issues": ["near-zero invocations (7d) — endpoint still incurs compute charges"],
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning SageMaker in {region}: {e}")
        return issues

    # ── Bedrock ───────────────────────────────────────────────────────

    def _scan_bedrock(self, region, session=None):
        """Scan Bedrock provisioned resources that incur cost (Phase 1)."""
        issues = []
        session = session or boto3.Session()
        try:
            bedrock = session.client('bedrock', region_name=region)

            # Provisioned throughput — billed whether used or not
            try:
                pts = bedrock.list_provisioned_model_throughputs().get('provisionedModelSummaries', [])
                for pt in pts:
                    status = pt.get('status', 'Unknown')
                    issues.append({
                        "type": "provisioned_throughput",
                        "name": pt.get('provisionedModelName', pt.get('provisionedModelArn', 'unknown')),
                        "model": pt.get('modelArn', 'unknown').split('/')[-1],
                        "status": status,
                        "commitment": pt.get('commitmentDuration', 'None'),
                        "region": region,
                        "issues": [f"provisioned throughput ({status}) — billed continuously"],
                        "estimated_monthly_cost": 0
                    })
            except Exception:
                pass

            # Custom models — storage costs
            try:
                models = bedrock.list_custom_models().get('modelSummaries', [])
                for m in models:
                    issues.append({
                        "type": "custom_model",
                        "name": m.get('modelName', 'unknown'),
                        "base_model": m.get('baseModelIdentifier', 'unknown').split('/')[-1],
                        "status": "Active",
                        "region": region,
                        "issues": ["custom model storage cost"],
                        "estimated_monthly_cost": 0
                    })
            except Exception:
                pass

            # Active model customization jobs (training compute)
            try:
                jobs = bedrock.list_model_customization_jobs().get('modelCustomizationJobSummaries', [])
                for j in jobs:
                    status = j.get('status', 'Unknown')
                    if status in ('InProgress', 'Validating', 'Stopping'):
                        issues.append({
                            "type": "customization_job",
                            "name": j.get('jobName', 'unknown'),
                            "status": status,
                            "region": region,
                            "issues": [f"active customization job ({status}) — consuming compute"],
                            "estimated_monthly_cost": 0
                        })
            except Exception:
                pass

            # Guardrails
            try:
                guardrails = bedrock.list_guardrails().get('guardrails', [])
                for g in guardrails:
                    issues.append({
                        "type": "guardrail",
                        "name": g.get('name', 'unknown'),
                        "status": g.get('status', 'unknown'),
                        "region": region,
                        "issues": ["guardrail — invocation-based cost"],
                        "estimated_monthly_cost": 0
                    })
            except Exception:
                pass

            # Knowledge bases and agents via bedrock-agent
            try:
                agent_client = session.client('bedrock-agent', region_name=region)

                try:
                    kbs = agent_client.list_knowledge_bases().get('knowledgeBaseSummaries', [])
                    for kb in kbs:
                        issues.append({
                            "type": "knowledge_base",
                            "name": kb.get('name', 'unknown'),
                            "status": kb.get('status', 'unknown'),
                            "region": region,
                            "issues": ["knowledge base — associated storage/vector DB costs"],
                            "estimated_monthly_cost": 0
                        })
                except Exception:
                    pass

                try:
                    agents = agent_client.list_agents().get('agentSummaries', [])
                    for a in agents:
                        issues.append({
                            "type": "agent",
                            "name": a.get('agentName', 'unknown'),
                            "status": a.get('agentStatus', 'unknown'),
                            "region": region,
                            "issues": ["bedrock agent — invocation-based cost"],
                            "estimated_monthly_cost": 0
                        })
                except Exception:
                    pass
            except Exception:
                pass

        except Exception as e:
            if 'AccessDenied' not in str(e) and 'not supported' not in str(e).lower():
                print(f"Error scanning Bedrock in {region}: {e}")
        return issues

    def _has_bedrock_activity(self, session, regions):
        """Quick check: does any region have AWS/Bedrock CloudWatch metrics?"""
        session = session or boto3.Session()
        for region in regions:
            try:
                cw = session.client('cloudwatch', region_name=region)
                metrics = cw.list_metrics(Namespace='AWS/Bedrock', Limit=1).get('Metrics', [])
                if metrics:
                    return True
            except Exception:
                pass
        return False

    def _scan_bedrock_usage(self, session, regions):
        """
        Deep-dive Bedrock usage analysis:
        - CloudWatch: per-model invocation/token counts
        - IAM role scanning: find Lambda/ECS resources with Bedrock permissions
        - CloudTrail management events: catch bedrock.amazonaws.com API calls
        """
        session = session or boto3.Session()
        usage = {
            "model_usage": [],
            "top_callers": [],
            "regions_with_activity": [],
        }

        all_model_usage = {}
        all_callers = {}

        def scan_region_cloudwatch(region):
            """CloudWatch metrics per model."""
            region_models = {}
            try:
                cw = session.client('cloudwatch', region_name=region)
                metrics = cw.list_metrics(Namespace='AWS/Bedrock').get('Metrics', [])

                model_ids = set()
                for m in metrics:
                    for dim in m.get('Dimensions', []):
                        if dim['Name'] == 'ModelId':
                            model_ids.add(dim['Value'])

                now = datetime.now()
                start = now - timedelta(days=30)

                for model_id in model_ids:
                    dims = [{'Name': 'ModelId', 'Value': model_id}]
                    model_name = model_id.split('/')[-1] if '/' in model_id else model_id

                    invocations = 0
                    input_tokens = 0
                    output_tokens = 0

                    for metric_name, target in [('Invocations', 'invocations'), ('InputTokenCount', 'input'), ('OutputTokenCount', 'output')]:
                        try:
                            resp = cw.get_metric_statistics(
                                Namespace='AWS/Bedrock', MetricName=metric_name,
                                Dimensions=dims, StartTime=start, EndTime=now,
                                Period=2592000, Statistics=['Sum']
                            )
                            if resp['Datapoints']:
                                val = int(sum(d['Sum'] for d in resp['Datapoints']))
                                if target == 'invocations':
                                    invocations = val
                                elif target == 'input':
                                    input_tokens = val
                                else:
                                    output_tokens = val
                        except Exception:
                            pass

                    if invocations > 0 or input_tokens > 0:
                        region_models[model_name] = {
                            "model": model_name,
                            "model_id": model_id,
                            "region": region,
                            "invocations_30d": invocations,
                            "input_tokens_30d": input_tokens,
                            "output_tokens_30d": output_tokens,
                        }
            except Exception as e:
                if 'AccessDenied' not in str(e):
                    print(f"Error querying Bedrock CloudWatch in {region}: {e}")
            return region, region_models

        def _role_has_bedrock(iam, role_name):
            """Check if an IAM role grants Bedrock access."""
            try:
                # Check attached managed policies
                attached = iam.list_attached_role_policies(RoleName=role_name).get('AttachedPolicies', [])
                for pol in attached:
                    pname = pol.get('PolicyName', '').lower()
                    if 'bedrock' in pname or pname in ('administratoraccess', 'poweruseraccess'):
                        return True

                # Check inline policies
                inline_names = iam.list_role_policies(RoleName=role_name).get('PolicyNames', [])
                for pol_name in inline_names:
                    try:
                        doc = iam.get_role_policy(RoleName=role_name, PolicyName=pol_name).get('PolicyDocument', {})
                        doc_str = json.dumps(doc).lower()
                        if 'bedrock' in doc_str:
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
            return False

        def scan_region_callers(region):
            """Find Lambda functions and ECS services whose roles have Bedrock permissions."""
            callers = {}

            # --- Lambda functions with Bedrock access ---
            try:
                lambda_client = session.client('lambda', region_name=region)
                iam = session.client('iam')
                cw = session.client('cloudwatch', region_name=region)
                now = datetime.now()
                start = now - timedelta(days=30)

                paginator = lambda_client.get_paginator('list_functions')
                for page in paginator.paginate():
                    for fn in page.get('Functions', []):
                        role_arn = fn.get('Role', '')
                        role_name = role_arn.split('/')[-1] if '/' in role_arn else ''
                        if not role_name:
                            continue

                        if not _role_has_bedrock(iam, role_name):
                            continue

                        fn_name = fn['FunctionName']
                        # Get invocation count to gauge activity level
                        invocations = 0
                        try:
                            resp = cw.get_metric_statistics(
                                Namespace='AWS/Lambda', MetricName='Invocations',
                                Dimensions=[{'Name': 'FunctionName', 'Value': fn_name}],
                                StartTime=start, EndTime=now,
                                Period=2592000, Statistics=['Sum']
                            )
                            if resp['Datapoints']:
                                invocations = int(sum(d['Sum'] for d in resp['Datapoints']))
                        except Exception:
                            pass

                        key = f"lambda|{fn_name}|{region}"
                        callers[key] = {
                            "principal": fn_name,
                            "arn": fn.get('FunctionArn', ''),
                            "role_arn": role_arn,
                            "source_type": "Lambda",
                            "region": region,
                            "call_count": invocations,
                            "runtime": fn.get('Runtime', 'unknown'),
                            "memory_mb": fn.get('MemorySize', 0),
                            "actions": [{"action": "InvokeModel (via role)", "count": invocations}],
                        }
            except Exception as e:
                if 'AccessDenied' not in str(e):
                    print(f"Error scanning Lambda Bedrock callers in {region}: {e}")

            # --- ECS services/tasks with Bedrock access ---
            try:
                ecs = session.client('ecs', region_name=region)
                iam = session.client('iam')

                clusters = ecs.list_clusters().get('clusterArns', [])
                for cluster_arn in clusters:
                    cluster_name = cluster_arn.split('/')[-1]
                    try:
                        services = ecs.list_services(cluster=cluster_arn).get('serviceArns', [])
                        if not services:
                            continue
                        described = ecs.describe_services(cluster=cluster_arn, services=services[:10]).get('services', [])
                        for svc in described:
                            td_arn = svc.get('taskDefinition', '')
                            if not td_arn:
                                continue
                            try:
                                td = ecs.describe_task_definition(taskDefinition=td_arn).get('taskDefinition', {})
                                task_role_arn = td.get('taskRoleArn', '')
                                role_name = task_role_arn.split('/')[-1] if '/' in task_role_arn else ''
                                if not role_name or not _role_has_bedrock(iam, role_name):
                                    continue

                                svc_name = svc.get('serviceName', 'unknown')
                                running = svc.get('runningCount', 0)

                                key = f"ecs|{svc_name}|{region}"
                                callers[key] = {
                                    "principal": svc_name,
                                    "arn": svc.get('serviceArn', ''),
                                    "role_arn": task_role_arn,
                                    "source_type": "ECS",
                                    "region": region,
                                    "call_count": running,
                                    "cluster": cluster_name,
                                    "running_tasks": running,
                                    "actions": [{"action": "InvokeModel (via task role)", "count": running}],
                                }
                            except Exception:
                                pass
                    except Exception:
                        pass
            except Exception as e:
                if 'AccessDenied' not in str(e):
                    print(f"Error scanning ECS Bedrock callers in {region}: {e}")

            # --- CloudTrail management events (bedrock.amazonaws.com) ---
            try:
                ct = session.client('cloudtrail', region_name=region)
                events = []
                lookup_kwargs = {
                    'LookupAttributes': [
                        {'AttributeKey': 'EventSource', 'AttributeValue': 'bedrock.amazonaws.com'}
                    ],
                    'StartTime': datetime.now() - timedelta(days=30),
                    'EndTime': datetime.now(),
                    'MaxResults': 50
                }
                try:
                    resp = ct.lookup_events(**lookup_kwargs)
                    events = resp.get('Events', [])
                except Exception:
                    pass

                for event in events:
                    try:
                        detail = json.loads(event.get('CloudTrailEvent', '{}'))
                        user_identity = detail.get('userIdentity', {})
                        arn = user_identity.get('arn', 'unknown')
                        user_agent = detail.get('userAgent', 'unknown')
                        event_name = detail.get('eventName', 'unknown')

                        source_type = 'SDK/Other'
                        if 'console' in user_agent.lower() or 'signin' in user_agent.lower():
                            source_type = 'Console'
                        elif 'boto3' in user_agent.lower() or 'botocore' in user_agent.lower():
                            source_type = 'Python SDK'
                        elif 'aws-cli' in user_agent.lower():
                            source_type = 'AWS CLI'

                        principal = arn.split('/')[-1] if '/' in arn else arn
                        caller_key = f"ct|{principal}|{source_type}|{region}"

                        if caller_key not in callers:
                            callers[caller_key] = {
                                "principal": principal,
                                "arn": arn,
                                "source_type": source_type,
                                "region": region,
                                "call_count": 0,
                                "actions": {},
                            }
                        callers[caller_key]["call_count"] += 1
                        if isinstance(callers[caller_key]["actions"], dict):
                            callers[caller_key]["actions"][event_name] = \
                                callers[caller_key]["actions"].get(event_name, 0) + 1
                    except Exception:
                        pass

                # Convert any remaining dict-style actions to list
                for key in callers:
                    if isinstance(callers[key]["actions"], dict):
                        callers[key]["actions"] = [
                            {"action": a, "count": c}
                            for a, c in sorted(callers[key]["actions"].items(), key=lambda x: x[1], reverse=True)
                        ]
            except Exception as e:
                if 'AccessDenied' not in str(e):
                    print(f"Error querying CloudTrail for Bedrock in {region}: {e}")

            return region, callers

        # Run CloudWatch scans in parallel across regions
        with ThreadPoolExecutor(max_workers=min(len(regions), 5)) as executor:
            futures = {executor.submit(scan_region_cloudwatch, r): r for r in regions}
            for future in as_completed(futures):
                try:
                    region, models = future.result()
                    if models:
                        usage["regions_with_activity"].append(region)
                    for k, v in models.items():
                        if k in all_model_usage:
                            all_model_usage[k]["invocations_30d"] += v["invocations_30d"]
                            all_model_usage[k]["input_tokens_30d"] += v["input_tokens_30d"]
                            all_model_usage[k]["output_tokens_30d"] += v["output_tokens_30d"]
                            all_model_usage[k]["region"] = all_model_usage[k].get("region", "") + ", " + region
                        else:
                            all_model_usage[k] = v
                except Exception as e:
                    print(f"Error in Bedrock CloudWatch scan: {e}")

        # Only scan callers in regions where Bedrock activity was found
        active_regions = usage["regions_with_activity"] if usage["regions_with_activity"] else regions[:2]
        with ThreadPoolExecutor(max_workers=min(len(active_regions), 5)) as executor:
            futures = {executor.submit(scan_region_callers, r): r for r in active_regions}
            for future in as_completed(futures):
                try:
                    region, callers = future.result()
                    for k, v in callers.items():
                        if k in all_callers:
                            all_callers[k]["call_count"] += v["call_count"]
                        else:
                            all_callers[k] = v
                except Exception as e:
                    print(f"Error in Bedrock caller scan: {e}")

        # Sort by usage volume
        usage["model_usage"] = sorted(
            all_model_usage.values(),
            key=lambda x: x["invocations_30d"],
            reverse=True
        )
        usage["top_callers"] = sorted(
            all_callers.values(),
            key=lambda x: x["call_count"],
            reverse=True
        )[:20]

        return usage

    # ── Networking & API ──────────────────────────────────────────────

    def _scan_cloudfront(self, region, session=None):
        """Scan CloudFront distributions (global, only scan from us-east-1)."""
        if region != 'us-east-1':
            return []
        issues = []
        session = session or boto3.Session()
        try:
            cf = session.client('cloudfront')
            cloudwatch = session.client('cloudwatch', region_name='us-east-1')
            response = cf.list_distributions()
            items = response.get('DistributionList', {}).get('Items', []) or []

            for dist in items:
                dist_id = dist['Id']
                enabled = dist.get('Enabled', True)
                origins = dist.get('Origins', {}).get('Quantity', 0)

                if not enabled:
                    issues.append({
                        "id": dist_id,
                        "domain": dist.get('DomainName', ''),
                        "enabled": False, "origins": origins,
                        "region": "global",
                        "issues": ["distribution is disabled but still exists"],
                        "estimated_monthly_cost": 0
                    })
                    continue

                # Check requests over 7 days
                requests = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/CloudFront', MetricName='Requests',
                        Dimensions=[{'Name': 'DistributionId', 'Value': dist_id}, {'Name': 'Region', 'Value': 'Global'}],
                        StartTime=datetime.now() - timedelta(days=7),
                        EndTime=datetime.now(), Period=604800, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        requests = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                if requests < 100:
                    issues.append({
                        "id": dist_id,
                        "domain": dist.get('DomainName', ''),
                        "enabled": True, "origins": origins,
                        "requests_7d": int(requests),
                        "region": "global",
                        "issues": ["near-zero requests (7d)"],
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning CloudFront: {e}")
        return issues

    def _scan_api_gateway(self, region, session=None):
        """Scan API Gateway for unused REST/HTTP APIs."""
        issues = []
        session = session or boto3.Session()
        try:
            # REST APIs
            apigw = session.client('apigateway', region_name=region)
            cloudwatch = session.client('cloudwatch', region_name=region)
            apis = apigw.get_rest_apis().get('items', [])

            for api in apis:
                api_id = api['id']
                api_name = api.get('name', api_id)

                # Check request count
                requests = 0
                try:
                    cw = cloudwatch.get_metric_statistics(
                        Namespace='AWS/ApiGateway', MetricName='Count',
                        Dimensions=[{'Name': 'ApiName', 'Value': api_name}],
                        StartTime=datetime.now() - timedelta(days=30),
                        EndTime=datetime.now(), Period=2592000, Statistics=['Sum']
                    )
                    if cw['Datapoints']:
                        requests = sum(d['Sum'] for d in cw['Datapoints'])
                except Exception:
                    pass

                if requests < 10:
                    issues.append({
                        "id": api_id, "name": api_name, "type": "REST",
                        "requests_30d": int(requests),
                        "region": region, "issues": ["near-zero requests (30d)"],
                        "estimated_monthly_cost": 0
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning API Gateway in {region}: {e}")

        # HTTP APIs (v2)
        try:
            apigwv2 = session.client('apigatewayv2', region_name=region)
            apis_v2 = apigwv2.get_apis().get('Items', [])
            for api in apis_v2:
                api_id = api['ApiId']
                api_name = api.get('Name', api_id)
                # HTTP APIs are cheap, flag only if completely unused
                issues.append({
                    "id": api_id, "name": api_name,
                    "type": api.get('ProtocolType', 'HTTP'),
                    "region": region, "issues": ["review — HTTP API exists"],
                    "estimated_monthly_cost": 0
                })
        except Exception:
            pass
        return issues

    def _scan_route53(self, region, session=None):
        """Scan Route53 hosted zones (global, only from us-east-1)."""
        if region != 'us-east-1':
            return []
        issues = []
        session = session or boto3.Session()
        try:
            r53 = session.client('route53')
            zones = r53.list_hosted_zones().get('HostedZones', [])

            for zone in zones:
                zone_id = zone['Id'].split('/')[-1]
                zone_name = zone['Name']
                record_count = zone.get('ResourceRecordSetCount', 0)

                # A zone with only NS and SOA records (count=2) is likely orphaned
                if record_count <= 2:
                    issues.append({
                        "id": zone_id, "name": zone_name,
                        "record_count": record_count,
                        "private": zone.get('Config', {}).get('PrivateZone', False),
                        "region": "global",
                        "issues": ["only NS/SOA records — likely orphaned"],
                        "estimated_monthly_cost": 0.50
                    })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning Route53: {e}")
        return issues

    # ── Security & Config ─────────────────────────────────────────────

    def _scan_secrets_manager(self, region, session=None):
        """Scan Secrets Manager for unused secrets."""
        issues = []
        session = session or boto3.Session()
        try:
            client = session.client('secretsmanager', region_name=region)
            paginator = client.get_paginator('list_secrets')

            for page in paginator.paginate():
                for secret in page.get('SecretList', []):
                    last_accessed = secret.get('LastAccessedDate')
                    name = secret.get('Name', '')

                    if last_accessed:
                        days_since = (datetime.now() - last_accessed.replace(tzinfo=None)).days
                        if days_since > 90:
                            issues.append({
                                "name": name,
                                "last_accessed_days": days_since,
                                "region": region,
                                "issues": [f"not accessed in {days_since} days"],
                                "estimated_monthly_cost": 0.40
                            })
                    else:
                        # Never accessed
                        created = secret.get('CreatedDate')
                        if created:
                            age = (datetime.now() - created.replace(tzinfo=None)).days
                            if age > 30:
                                issues.append({
                                    "name": name,
                                    "last_accessed_days": None,
                                    "created_days_ago": age,
                                    "region": region,
                                    "issues": [f"never accessed, created {age} days ago"],
                                    "estimated_monthly_cost": 0.40
                                })
        except Exception as e:
            if 'AccessDenied' not in str(e):
                print(f"Error scanning Secrets Manager in {region}: {e}")
        return issues

    def _get_cpu_utilization(self, instance_id, cloudwatch_client):
        """Get average CPU utilization for last 7 days"""
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(days=7)
            
            response = cloudwatch_client.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='CPUUtilization',
                Dimensions=[
                    {'Name': 'InstanceId', 'Value': instance_id}
                ],
                StartTime=start_time,
                EndTime=end_time,
                Period=86400,  # 1 day
                Statistics=['Average']
            )
            
            if response['Datapoints']:
                avg = sum(d['Average'] for d in response['Datapoints']) / len(response['Datapoints'])
                return round(avg, 2)
            
        except Exception as e:
            print(f"Error getting CPU metrics for {instance_id}: {str(e)}")
        
        return None
    
    def _flatten_ec2_instances(self, by_region):
        """Flatten EC2 instances from all regions into a single list"""
        instances = []
        for region, data in by_region.items():
            if 'ec2_instances' in data:
                instances.extend(data['ec2_instances'])
        return instances
    
    def _flatten_rds_instances(self, by_region):
        """Flatten RDS instances from all regions into a single list"""
        instances = []
        for region, data in by_region.items():
            if 'rds_instances' in data:
                instances.extend(data['rds_instances'])
        return instances