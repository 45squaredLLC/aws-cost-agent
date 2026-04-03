# Deployment Examples

Example configurations for deploying AWS Cost Optimizer in production environments.

## Docker (Production)

The production Docker Compose file adds health checks, restart policies, resource limits, and persistent volumes.

```bash
cd examples/docker

# Copy your .env file or create one from the template
cp ../../.env .env

# Start the application
docker compose -f docker-compose.production.yml up -d

# View logs
docker compose -f docker-compose.production.yml logs -f

# Stop
docker compose -f docker-compose.production.yml down
```

An example `nginx.conf` is included for running behind a reverse proxy with SSL termination.

## Kubernetes

Deploy to any Kubernetes cluster (k3s, EKS, GKE, AKS, etc.).

### Quick Start

```bash
cd examples/kubernetes

# 1. Create the namespace
kubectl apply -f namespace.yaml

# 2. Create secrets (edit secret.yaml first with your credentials)
#    Generate values:
#    echo -n 'YOUR_ACCESS_KEY' | base64
#    echo -n 'YOUR_SECRET_KEY' | base64
#    echo -n "$(openssl rand -hex 32)" | base64
kubectl apply -f secret.yaml

# 3. Create persistent storage for analysis history
kubectl apply -f pvc.yaml

# 4. Deploy the application
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml

# 5. (Optional) Expose via ingress — edit ingress.yaml with your hostname first
kubectl apply -f ingress.yaml
```

### Building and Pushing the Image

The deployment manifest expects an image at `your-registry.example.com/aws-cost-optimizer:latest`. Build and push it:

```bash
# From the project root:
docker build -t your-registry.example.com/aws-cost-optimizer:latest .
docker push your-registry.example.com/aws-cost-optimizer:latest
```

Then update `deployment.yaml` with your image path.

### Without Ingress

To access the app directly without an ingress controller:

```bash
# Port-forward to your local machine
kubectl port-forward -n aws-cost-optimizer svc/aws-cost-optimizer 5000:5000

# Open http://localhost:5000
```

Or change the Service type to `NodePort` or `LoadBalancer` in `service.yaml`.

### Serving at a Sub-Path

If you want to serve the app at a sub-path (e.g. `/aws-cost-optimizer`) behind an ingress:

1. Uncomment the `APPLICATION_ROOT` env var in `deployment.yaml`
2. Update the ingress path to match
3. Redeploy

### Notes

- **Secrets**: Never commit `secret.yaml` with real values. Consider using [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets), [External Secrets Operator](https://external-secrets.io/), or your cloud provider's secrets manager.
- **Storage**: The PVC uses the default StorageClass. Set `storageClassName` if your cluster requires a specific class.
- **Resources**: The default limits (512Mi memory, 1 CPU) are sufficient for most accounts. Increase if scanning many accounts or regions simultaneously.
- **Timeouts**: Analysis scans can take several minutes. The ingress annotations set a 5-minute read timeout — adjust if needed for your ingress controller.
