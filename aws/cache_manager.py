import os
import json
import time
from datetime import datetime


class CacheManager:
    def __init__(self, data_dir='./data'):
        self.cache_dir = os.path.join(data_dir, 'cache')
        self.history_dir = os.path.join(data_dir, 'history')
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.history_dir, exist_ok=True)

    # --- Caching ---
    def get_cached_scan(self, cache_key, max_age_minutes=60):
        """Read cached scan if fresh. cache_key = account_id or 'single'."""
        cache_file = os.path.join(self.cache_dir, f'{cache_key}.json')
        if not os.path.exists(cache_file):
            return None

        try:
            mtime = os.path.getmtime(cache_file)
            age_minutes = (time.time() - mtime) / 60
            if age_minutes > max_age_minutes:
                return None

            with open(cache_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading cache for {cache_key}: {e}")
            return None

    def save_scan(self, cache_key, data):
        """Save scan result as JSON."""
        cache_file = os.path.join(self.cache_dir, f'{cache_key}.json')
        try:
            with open(cache_file, 'w') as f:
                json.dump(data, f, default=str)
        except Exception as e:
            print(f"Error saving cache for {cache_key}: {e}")

    # --- History ---
    def save_analysis(self, cost_data, resources, recommendations):
        """Save analysis as timestamped markdown. Returns filename."""
        timestamp = datetime.now()
        filename = f'analysis-{timestamp.strftime("%Y%m%d-%H%M%S")}.md'
        filepath = os.path.join(self.history_dir, filename)

        markdown = self._format_as_markdown(cost_data, resources, recommendations, timestamp)

        try:
            with open(filepath, 'w') as f:
                f.write(markdown)
            return filename
        except Exception as e:
            print(f"Error saving analysis history: {e}")
            return None

    def list_history(self, limit=20):
        """List saved analyses, most recent first."""
        try:
            files = [f for f in os.listdir(self.history_dir) if f.endswith('.md')]
            files.sort(reverse=True)
            files = files[:limit]

            entries = []
            for f in files:
                filepath = os.path.join(self.history_dir, f)
                stat = os.stat(filepath)
                # Parse basic info from filename
                # Format: analysis-YYYYMMDD-HHMMSS.md
                try:
                    date_str = f.replace('analysis-', '').replace('.md', '')
                    dt = datetime.strptime(date_str, '%Y%m%d-%H%M%S')
                    date_display = dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    date_display = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')

                # Read first few lines to extract summary
                summary = self._extract_summary(filepath)

                entries.append({
                    'filename': f,
                    'date': date_display,
                    'size_bytes': stat.st_size,
                    **summary
                })

            return entries
        except Exception as e:
            print(f"Error listing history: {e}")
            return []

    def get_history_entry(self, filename):
        """Read a specific history markdown file."""
        # Sanitize filename to prevent directory traversal
        filename = os.path.basename(filename)
        filepath = os.path.join(self.history_dir, filename)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, 'r') as f:
                return f.read()
        except Exception as e:
            print(f"Error reading history entry {filename}: {e}")
            return None

    def _extract_summary(self, filepath):
        """Extract summary info from a history markdown file."""
        summary = {}
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('- Total Cost'):
                        summary['total_cost'] = line.split(': ', 1)[-1] if ': ' in line else ''
                    elif line.startswith('- Accounts Analyzed'):
                        summary['accounts'] = line.split(': ', 1)[-1] if ': ' in line else ''
                    elif line.startswith('- Regions Scanned'):
                        summary['regions'] = line.split(': ', 1)[-1] if ': ' in line else ''
                    if len(summary) >= 3:
                        break
        except Exception:
            pass
        return summary

    def _format_as_markdown(self, cost_data, resources, recommendations, timestamp):
        """Convert analysis data to readable markdown."""
        lines = []
        lines.append(f'# AWS Cost Analysis - {timestamp.strftime("%Y-%m-%d %H:%M")}')
        lines.append('')

        # Summary section
        lines.append('## Summary')
        total_cost = cost_data.get('total_cost', 0)
        lines.append(f'- Total Cost ({cost_data.get("period_days", 30)} days): ${total_cost}')

        # Determine if org or single mode
        mode = resources.get('mode', 'single')
        if mode == 'organization':
            account_count = len(resources.get('accounts', {}))
            lines.append(f'- Accounts Analyzed: {account_count}')
        else:
            account_id = resources.get('account_id', 'Unknown')
            lines.append(f'- Accounts Analyzed: Single Account ({account_id})')

        regions = resources.get('regions_scanned', [])
        lines.append(f'- Regions Scanned: {len(regions)}')
        lines.append('')

        # Cost breakdown
        if mode == 'organization' and 'account_costs' in cost_data:
            lines.append('## Cost Breakdown by Account')
            for acct_id, acct_cost in cost_data.get('account_costs', {}).items():
                acct_name = acct_cost.get('account_name', acct_id)
                lines.append(f'### {acct_name} ({acct_id})')
                lines.append('| Service | Cost |')
                lines.append('|---------|------|')
                for svc in acct_cost.get('top_services', []):
                    lines.append(f'| {svc["service"]} | ${svc["cost"]} |')
                lines.append('')
        else:
            lines.append('## Cost Breakdown by Service')
            lines.append('| Service | Cost |')
            lines.append('|---------|------|')
            for svc in cost_data.get('top_services', []):
                lines.append(f'| {svc["service"]} | ${svc["cost"]} |')
            lines.append('')

        # Resources summary
        lines.append('## Resources')
        if mode == 'organization':
            for acct_id, acct_data in resources.get('accounts', {}).items():
                acct_name = acct_data.get('account_name', acct_id)
                totals = acct_data.get('totals', {})
                lines.append(f'### {acct_name} ({acct_id})')
                lines.append(f'- EC2 Instances: {totals.get("ec2_instances", 0)}')
                lines.append(f'- RDS Instances: {totals.get("rds_instances", 0)}')
                lines.append(f'- S3 Buckets: {totals.get("s3_buckets", 0)}')
                lines.append('')
        else:
            totals = resources.get('totals', {})
            lines.append(f'- EC2 Instances: {totals.get("ec2_instances", 0)}')
            lines.append(f'- RDS Instances: {totals.get("rds_instances", 0)}')
            lines.append(f'- S3 Buckets: {totals.get("s3_buckets", 0)}')
            lines.append('')

        # Orphaned resources
        orphaned = resources.get('orphaned_resources', {})
        if mode == 'organization':
            # Aggregate orphaned from all accounts
            all_volumes = []
            all_snapshots = []
            all_eips = []
            all_amis = []
            for acct_data in resources.get('accounts', {}).values():
                acct_orphaned = acct_data.get('orphaned_resources', {})
                all_volumes.extend(acct_orphaned.get('unattached_volumes', []))
                all_snapshots.extend(acct_orphaned.get('orphaned_snapshots', []))
                all_eips.extend(acct_orphaned.get('unassociated_eips', []))
                all_amis.extend(acct_orphaned.get('stale_amis', []))
            orphaned = {
                'unattached_volumes': all_volumes,
                'orphaned_snapshots': all_snapshots,
                'unassociated_eips': all_eips,
                'stale_amis': all_amis
            }

        has_orphaned = any(len(v) > 0 for v in orphaned.values())
        if has_orphaned:
            lines.append('## Orphaned Resources')

            volumes = orphaned.get('unattached_volumes', [])
            if volumes:
                total_cost = sum(v.get('estimated_monthly_cost', 0) for v in volumes)
                lines.append(f'### Unattached EBS Volumes ({len(volumes)} found, ~${total_cost:.2f}/month waste)')
                for v in volumes[:10]:
                    lines.append(f'- {v["id"]}: {v["size_gb"]}GB {v["volume_type"]} in {v["region"]} (${v.get("estimated_monthly_cost", 0)}/month)')
                if len(volumes) > 10:
                    lines.append(f'- ...and {len(volumes) - 10} more')
                lines.append('')

            snapshots = orphaned.get('orphaned_snapshots', [])
            if snapshots:
                total_cost = sum(s.get('estimated_monthly_cost', 0) for s in snapshots)
                lines.append(f'### Orphaned Snapshots ({len(snapshots)} found, ~${total_cost:.2f}/month waste)')
                for s in snapshots[:10]:
                    lines.append(f'- {s["id"]}: {s["size_gb"]}GB in {s["region"]} (${s.get("estimated_monthly_cost", 0)}/month)')
                if len(snapshots) > 10:
                    lines.append(f'- ...and {len(snapshots) - 10} more')
                lines.append('')

            eips = orphaned.get('unassociated_eips', [])
            if eips:
                total_cost = sum(e.get('estimated_monthly_cost', 0) for e in eips)
                lines.append(f'### Unassociated Elastic IPs ({len(eips)} found, ~${total_cost:.2f}/month waste)')
                for e in eips[:10]:
                    lines.append(f'- {e["public_ip"]} in {e["region"]} (${e.get("estimated_monthly_cost", 0)}/month)')
                if len(eips) > 10:
                    lines.append(f'- ...and {len(eips) - 10} more')
                lines.append('')

            amis = orphaned.get('stale_amis', [])
            if amis:
                total_cost = sum(a.get('estimated_monthly_cost', 0) for a in amis)
                lines.append(f'### Stale AMIs ({len(amis)} found, ~${total_cost:.2f}/month waste)')
                for a in amis[:10]:
                    lines.append(f'- {a["id"]}: {a.get("name", "Unnamed")} - {a.get("age_days", 0)} days old (${a.get("estimated_monthly_cost", 0)}/month)')
                if len(amis) > 10:
                    lines.append(f'- ...and {len(amis) - 10} more')
                lines.append('')

        # AI Recommendations
        recs = recommendations.get('recommendations', []) if isinstance(recommendations, dict) else []
        if recs:
            lines.append('## AI Recommendations')
            for i, rec in enumerate(recs, 1):
                risk = rec.get('risk', 'Unknown')
                lines.append(f'{i}. **{rec.get("title", "Recommendation")}** - {rec.get("estimated_savings", "N/A")} savings ({risk} risk)')
                lines.append(f'   {rec.get("description", "")}')
                if rec.get('cli_command'):
                    lines.append(f'   ```')
                    lines.append(f'   {rec["cli_command"]}')
                    lines.append(f'   ```')
                lines.append('')

        return '\n'.join(lines)
