# Favonius Energy Platform - Kubernetes Deployment Guide

## Overview

This guide covers deploying the Favonius Energy EV Fleet Depot Optimization Platform to Kubernetes. The deployment includes:

- Main API service (FastAPI REST API + OCPP WebSocket + Optimization Engine)
- TimescaleDB database (or managed service)
- Ingress with TLS termination
- Horizontal Pod Autoscaling
- Monitoring and health checks

**Reference:** PRD_v2.md Section 10.3 (Security), Section 10.1 (Performance), Development Plan Phase 7 Step 7.2

## Prerequisites

### Required Tools

- `kubectl` (v1.24+) configured with cluster access
- Kubernetes cluster (v1.24+)
- `helm` (optional, for cert-manager installation)

### Required Components

1. **Ingress Controller**: Nginx Ingress Controller or similar
2. **Cert-Manager**: For automatic TLS certificate management (recommended)
3. **Storage Class**: For TimescaleDB persistent volumes
4. **Metrics Server**: For HPA to function

### Optional Components

- Prometheus Operator (for advanced monitoring)
- External Secrets Operator (for secrets management)
- VPA (Vertical Pod Autoscaler) controller

## Deployment Steps

### 1. Install Cert-Manager (Recommended)

Cert-manager automatically manages TLS certificates from Let's Encrypt:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.0/cert-manager.yaml
```

Wait for cert-manager to be ready:

```bash
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=300s
```

### 2. Create ClusterIssuer for Let's Encrypt

Create `k8s/cluster-issuer.yaml`:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: your-email@example.com  # Replace with your email
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
    - http01:
        ingress:
          class: nginx
```

Apply:

```bash
kubectl apply -f k8s/cluster-issuer.yaml
```

### 3. Configure Secrets

**IMPORTANT:** Replace placeholder values in `k8s/secret.yaml` with actual secrets:

```bash
# Generate JWT secret
JWT_SECRET=$(openssl rand -hex 32)

# Update secret.yaml with:
# - DATABASE_PASSWORD: Your database password
# - JWT_SECRET_KEY: Generated JWT secret
# - gurobi.lic: Your Gurobi license file content
```

For production, use external secrets manager:
- AWS Secrets Manager
- Google Cloud Secret Manager
- HashiCorp Vault
- External Secrets Operator

### 4. Update Configuration

Edit `k8s/configmap.yaml` and `k8s/api-ingress.yaml` to set your domain names:

```yaml
# In api-ingress.yaml, replace:
- host: api.yourdomain.com  # Your API domain
- host: ocpp.yourdomain.com  # Your OCPP WebSocket domain
- host: monitoring.yourdomain.com  # Your monitoring domain
```

### 5. Deploy Namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

### 6. Deploy ConfigMap and Secrets

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
```

### 7. Deploy TimescaleDB (Optional)

**For Production:** Use managed TimescaleDB service (AWS RDS, Google Cloud SQL) instead.

For self-hosted deployment:

```bash
kubectl apply -f k8s/timescaledb-deployment.yaml
```

Wait for database to be ready:

```bash
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=timescaledb -n ev-charging --timeout=300s
```

### 8. Deploy RBAC

```bash
kubectl apply -f k8s/rbac.yaml
```

### 9. Deploy API Service

```bash
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/api-service.yaml
```

### 10. Deploy Ingress

```bash
kubectl apply -f k8s/api-ingress.yaml
```

### 11. Deploy Autoscaling and PDB

```bash
kubectl apply -f k8s/autoscaling.yaml
kubectl apply -f k8s/pdb.yaml
```

### 12. Verify Deployment

Check pod status:

```bash
kubectl get pods -n ev-charging
```

Expected output:

```
NAME                              READY   STATUS    RESTARTS   AGE
favonius-api-xxxxxxxxxx-xxxxx     1/1     Running   0          2m
timescaledb-0                     1/1     Running   0          5m
```

Check services:

```bash
kubectl get svc -n ev-charging
```

Check ingress:

```bash
kubectl get ingress -n ev-charging
```

### 13. Test Health Endpoint

```bash
# Get ingress IP
INGRESS_IP=$(kubectl get ingress favonius-api-ingress -n ev-charging -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# Test health endpoint (replace with your domain)
curl https://api.yourdomain.com/health
```

Expected response:

```json
{
  "status": "healthy",
  "timestamp": "2025-12-04T10:00:00Z",
  "components": {
    "database": "healthy",
    "ocpp_server": "healthy",
    "gurobi_license": "valid"
  }
}
```

## Configuration

### Environment Variables

Key configuration is managed via ConfigMap (`k8s/configmap.yaml`):

- `API_HOST`, `API_PORT`: REST API server settings
- `OCPP_SERVER_*`: OCPP WebSocket server settings
- `OPTIMIZATION_TIMEOUT`: Solver timeout (60s per PRD Section 8.2)
- `OPTIMIZATION_MIP_GAP`: Optimality gap (0.01 = 1% per PRD Section 8.2)
- `DATABASE_*`: Database connection settings (non-sensitive)

### Resource Limits

Per PRD Section 8.2 and 10.1:

- **API Pods**: 4GB memory, 2 CPU (limits)
- **TimescaleDB**: 2GB memory, 1 CPU (limits)

### Scaling

HPA configuration (per PRD Section 10.4):

- **Min replicas**: 2 (high availability)
- **Max replicas**: 10
- **CPU target**: 70%
- **Memory target**: 80%

## Security

### TLS/SSL

Per PRD Section 10.3:

- **HTTPS required** for all API endpoints
- **WSS required** for OCPP in production
- **TLS 1.3** support configured in ingress

Cert-manager automatically manages certificates from Let's Encrypt.

### Authentication

- **JWT tokens** for API authentication (per PRD Section 10.3)
- **Basic auth** for monitoring endpoints (optional)
- **OCPP authentication**: Basic auth + TLS 1.3 (per PRD Section 9.1)

### Secrets Management

**Never commit actual secrets to repository.**

For production, use:
- External Secrets Operator
- Cloud provider secrets manager
- HashiCorp Vault

## Monitoring

### Health Checks

- **Liveness probe**: `/health` endpoint, 30s interval
- **Readiness probe**: `/health` endpoint, 10s interval
- **Health check response time**: < 500ms (per PRD Section 10.1)

### Metrics

Prometheus scraping configured via annotations:

```yaml
prometheus.io/scrape: "true"
prometheus.io/port: "8000"
prometheus.io/path: "/metrics"
```

### Logging

Logs are written to `/app/logs` (emptyDir volume, 1GB limit).

For production, consider:
- Centralized logging (ELK, Loki, CloudWatch)
- Log aggregation and analysis

## Troubleshooting

### Pods Not Starting

Check pod logs:

```bash
kubectl logs -n ev-charging <pod-name>
```

Common issues:

1. **Database connection failure**: Verify `DATABASE_PASSWORD` in secrets
2. **Gurobi license error**: Verify `gurobi.lic` in secrets
3. **Image pull error**: Verify image name and registry access

### Health Check Failures

Check health endpoint directly:

```bash
kubectl exec -n ev-charging <pod-name> -- curl http://localhost:8000/health
```

### Ingress Not Working

1. Verify ingress controller is installed
2. Check ingress status: `kubectl describe ingress -n ev-charging`
3. Verify DNS points to ingress IP
4. Check TLS certificate: `kubectl get certificate -n ev-charging`

### Autoscaling Not Working

1. Verify metrics server is installed: `kubectl top nodes`
2. Check HPA status: `kubectl describe hpa -n ev-charging`
3. Verify resource requests/limits are set in deployment

### Database Connection Issues

1. Verify TimescaleDB pod is running: `kubectl get pods -n ev-charging -l app.kubernetes.io/name=timescaledb`
2. Check database logs: `kubectl logs -n ev-charging timescaledb-0`
3. Test connection: `kubectl exec -n ev-charging <api-pod> -- psql -h timescaledb-service -U favonius -d favonius`

## Production Considerations

### High Availability

- **Min replicas**: 2 (ensures availability during updates)
- **Pod Disruption Budget**: Min 1 available pod
- **Pod Anti-Affinity**: Spread pods across nodes

### Database

**Recommended:** Use managed TimescaleDB service:

- AWS RDS for PostgreSQL with TimescaleDB extension
- Google Cloud SQL for PostgreSQL with TimescaleDB extension
- Azure Database for PostgreSQL with TimescaleDB extension

Benefits:
- Automated backups
- High availability
- Managed updates
- Monitoring and alerting

### Backup and Recovery

1. **Database backups**: Configure automated backups for TimescaleDB
2. **Configuration backups**: Version control all Kubernetes manifests
3. **Secrets backup**: Store in external secrets manager with backup

### Performance Tuning

Per PRD Section 10.1:

- **API response time**: < 500ms (99th percentile)
- **Optimization latency**: < 60 seconds (95th percentile)
- **Database query time**: < 100ms (average)

Monitor and adjust:
- Resource limits
- HPA thresholds
- Database connection pool size
- Optimization solver settings

## Updating Deployment

### Rolling Updates

Deployment uses `RollingUpdate` strategy:

```bash
# Update image
kubectl set image deployment/favonius-api api=favonius/api:v1.1.0 -n ev-charging

# Or update entire deployment
kubectl apply -f k8s/api-deployment.yaml
```

### Configuration Updates

```bash
# Update ConfigMap
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/favonius-api -n ev-charging
```

### Secret Updates

```bash
# Update Secret (use external secrets manager in production)
kubectl apply -f k8s/secret.yaml
kubectl rollout restart deployment/favonius-api -n ev-charging
```

## Cleanup

To remove all resources:

```bash
kubectl delete -f k8s/
```

**Warning:** This will delete all data. Backup first!

## Additional Resources

- [PRD_v2.md](../docs/PRD_v2.md) - Product Requirements Document
- [Development Plan](../favonius_development_plan_v2.md) - Development Plan
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Cert-Manager Documentation](https://cert-manager.io/docs/)
