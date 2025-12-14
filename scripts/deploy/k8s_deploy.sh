#!/bin/bash
# Favonius Energy - Kubernetes Deployment Script
# Reference: PRD_v2.md Section 10.3; Development Plan Phase 7 Step 7.2

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

NAMESPACE="${NAMESPACE:-ev-charging}"
K8S_DIR="${K8S_DIR:-k8s}"

echo "=========================================="
echo "Favonius Energy - Kubernetes Deployment"
echo "=========================================="
echo ""

# Check prerequisites
echo "Checking prerequisites..."

if ! command -v kubectl &> /dev/null; then
    echo -e "${RED}✗ kubectl not found. Please install kubectl.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ kubectl found${NC}"

if ! kubectl cluster-info &> /dev/null; then
    echo -e "${RED}✗ Cannot connect to Kubernetes cluster${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Kubernetes cluster accessible${NC}"

echo ""

# Deploy in order
echo "Deploying Favonius Energy platform..."
echo ""

# 1. Namespace
echo "1. Creating namespace..."
kubectl apply -f "$K8S_DIR/namespace.yaml"
echo -e "${GREEN}✓ Namespace created${NC}"
echo ""

# 2. ConfigMap
echo "2. Creating ConfigMaps..."
kubectl apply -f "$K8S_DIR/configmap.yaml"
echo -e "${GREEN}✓ ConfigMaps created${NC}"
echo ""

# 3. Secrets (warn if using placeholders)
echo "3. Creating Secrets..."
if grep -q "change-me-in-production" "$K8S_DIR/secret.yaml"; then
    echo -e "${YELLOW}⚠ Warning: Secrets contain placeholder values${NC}"
    echo -e "${YELLOW}⚠ Please update secrets.yaml with actual values before production${NC}"
fi
kubectl apply -f "$K8S_DIR/secret.yaml"
echo -e "${GREEN}✓ Secrets created${NC}"
echo ""

# 4. RBAC
echo "4. Creating RBAC resources..."
kubectl apply -f "$K8S_DIR/rbac.yaml"
echo -e "${GREEN}✓ RBAC configured${NC}"
echo ""

# 5. TimescaleDB (optional - may use managed service)
echo "5. Deploying TimescaleDB..."
if [ -f "$K8S_DIR/timescaledb-deployment.yaml" ]; then
    kubectl apply -f "$K8S_DIR/timescaledb-deployment.yaml"
    echo -e "${GREEN}✓ TimescaleDB deployed${NC}"
    echo "   Waiting for database to be ready..."
    kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=timescaledb -n "$NAMESPACE" --timeout=300s || true
else
    echo -e "${YELLOW}⚠ TimescaleDB deployment file not found${NC}"
    echo -e "${YELLOW}⚠ Using managed database service?${NC}"
fi
echo ""

# 6. API Deployment
echo "6. Deploying API service..."
kubectl apply -f "$K8S_DIR/api-deployment.yaml"
echo -e "${GREEN}✓ API deployment created${NC}"
echo ""

# 7. Services
echo "7. Creating services..."
kubectl apply -f "$K8S_DIR/api-service.yaml"
echo -e "${GREEN}✓ Services created${NC}"
echo ""

# 8. Ingress
echo "8. Creating ingress..."
kubectl apply -f "$K8S_DIR/api-ingress.yaml"
echo -e "${GREEN}✓ Ingress created${NC}"
echo ""

# 9. Autoscaling
echo "9. Configuring autoscaling..."
kubectl apply -f "$K8S_DIR/autoscaling.yaml"
echo -e "${GREEN}✓ Autoscaling configured${NC}"
echo ""

# 10. Pod Disruption Budget
echo "10. Creating Pod Disruption Budget..."
kubectl apply -f "$K8S_DIR/pdb.yaml"
echo -e "${GREEN}✓ PDB created${NC}"
echo ""

# Wait for deployment
echo "Waiting for API pods to be ready..."
kubectl wait --for=condition=available deployment/favonius-api -n "$NAMESPACE" --timeout=300s || {
    echo -e "${YELLOW}⚠ Deployment not ready within timeout${NC}"
    echo "   Check pod status: kubectl get pods -n $NAMESPACE"
}

echo ""
echo "=========================================="
echo "Deployment Complete"
echo "=========================================="
echo ""

# Show status
echo "Deployment Status:"
kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=favonius-api
echo ""

echo "Services:"
kubectl get svc -n "$NAMESPACE" -l app.kubernetes.io/name=favonius-api
echo ""

echo "Ingress:"
kubectl get ingress -n "$NAMESPACE" -l app.kubernetes.io/name=favonius-api
echo ""

echo "Next steps:"
echo "  1. Verify deployment: ./scripts/deploy/verify_deployment.sh"
echo "  2. Check logs: kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=favonius-api"
echo "  3. Monitor metrics: kubectl port-forward -n $NAMESPACE svc/favonius-api-internal 8000:8000"
echo ""

