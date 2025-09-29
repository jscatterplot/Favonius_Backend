#!/bin/bash

# Redis Cluster Deployment Script for EV Charging Platform
# Supports both Docker Compose and Kubernetes deployments

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
DEPLOYMENT_TYPE="docker"
ENVIRONMENT="development"
REDIS_PASSWORD=""
NAMESPACE="redis-cluster"
STORAGE_CLASS="standard"

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

show_help() {
    cat << EOF
Redis Cluster Deployment Script for EV Charging Platform

Usage: $0 [OPTIONS]

Options:
    -t, --type TYPE         Deployment type: docker, kubernetes (default: docker)
    -e, --env ENV          Environment: development, staging, production (default: development)
    -p, --password PASS    Redis password (required for production)
    -n, --namespace NS     Kubernetes namespace (default: redis-cluster)
    -s, --storage-class SC Storage class for Kubernetes PVs (default: standard)
    -h, --help            Show this help message

Examples:
    # Deploy Redis cluster with Docker Compose
    $0 --type docker --env development

    # Deploy to Kubernetes production
    $0 --type kubernetes --env production --password "secure-password-123"

    # Deploy to Kubernetes with custom storage class
    $0 --type kubernetes --storage-class fast-ssd --namespace redis-prod

EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -t|--type)
            DEPLOYMENT_TYPE="$2"
            shift 2
            ;;
        -e|--env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        -p|--password)
            REDIS_PASSWORD="$2"
            shift 2
            ;;
        -n|--namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        -s|--storage-class)
            STORAGE_CLASS="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# Validation
if [[ "$DEPLOYMENT_TYPE" != "docker" && "$DEPLOYMENT_TYPE" != "kubernetes" ]]; then
    log_error "Invalid deployment type: $DEPLOYMENT_TYPE"
    exit 1
fi

if [[ "$ENVIRONMENT" == "production" && -z "$REDIS_PASSWORD" ]]; then
    log_error "Redis password is required for production environment"
    exit 1
fi

# Generate password if not provided
if [[ -z "$REDIS_PASSWORD" ]]; then
    REDIS_PASSWORD="redis-$(openssl rand -hex 8)"
    log_info "Generated Redis password: $REDIS_PASSWORD"
fi

deploy_docker() {
    log_info "Deploying Redis cluster with Docker Compose..."
    
    cd "$SCRIPT_DIR"
    
    # Set environment variables
    export REDIS_PASSWORD="$REDIS_PASSWORD"
    
    # Stop any existing deployment
    if docker-compose -f docker-compose.redis-cluster.yml ps -q > /dev/null 2>&1; then
        log_info "Stopping existing Redis cluster..."
        docker-compose -f docker-compose.redis-cluster.yml down
    fi
    
    # Start Redis cluster
    log_info "Starting Redis cluster nodes..."
    docker-compose -f docker-compose.redis-cluster.yml up -d
    
    # Wait for nodes to be ready
    log_info "Waiting for Redis nodes to be ready..."
    sleep 30
    
    # Initialize cluster
    log_info "Initializing Redis cluster..."
    docker-compose -f docker-compose.redis-cluster.yml --profile init run --rm redis-cluster-init
    
    # Verify cluster
    log_info "Verifying cluster status..."
    docker-compose -f docker-compose.redis-cluster.yml exec redis-cluster-1 redis-cli -a "$REDIS_PASSWORD" cluster info
    
    log_success "Redis cluster deployed successfully!"
    log_info "Cluster endpoints:"
    log_info "  - redis://localhost:7001 (with password: $REDIS_PASSWORD)"
    log_info "  - redis://localhost:7002 (with password: $REDIS_PASSWORD)"
    log_info "  - redis://localhost:7003 (with password: $REDIS_PASSWORD)"
    log_info ""
    log_info "To connect from application, use any of these endpoints with cluster mode enabled"
    log_info "Connection URL: redis-cluster://localhost:7001,localhost:7002,localhost:7003"
}

deploy_kubernetes() {
    log_info "Deploying Redis cluster to Kubernetes..."
    
    # Check if kubectl is available
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl is not installed or not in PATH"
        exit 1
    fi
    
    # Check if cluster is accessible
    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot connect to Kubernetes cluster"
        exit 1
    fi
    
    cd "$SCRIPT_DIR"
    
    # Create namespace
    log_info "Creating namespace: $NAMESPACE"
    kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
    
    # Update storage class in StatefulSet
    if [[ "$STORAGE_CLASS" != "standard" ]]; then
        log_info "Updating storage class to: $STORAGE_CLASS"
        sed -i.bak "s/storageClassName: \"fast-ssd\"/storageClassName: \"$STORAGE_CLASS\"/g" k8s/redis-cluster/statefulset.yaml
    fi
    
    # Update password in secret
    log_info "Creating Redis cluster secret..."
    sed "s/redis-cluster-production-password-change-me/$REDIS_PASSWORD/g" k8s/redis-cluster/secret.yaml > /tmp/redis-secret.yaml
    
    # Deploy Redis cluster
    log_info "Applying Kubernetes manifests..."
    kubectl apply -f k8s/redis-cluster/namespace.yaml
    kubectl apply -f /tmp/redis-secret.yaml
    kubectl apply -f k8s/redis-cluster/configmap.yaml
    kubectl apply -f k8s/redis-cluster/statefulset.yaml
    kubectl apply -f k8s/redis-cluster/service.yaml
    
    # Wait for StatefulSet to be ready
    log_info "Waiting for Redis pods to be ready..."
    kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=redis-cluster -n "$NAMESPACE" --timeout=300s
    
    # Initialize cluster
    log_info "Initializing Redis cluster..."
    kubectl apply -f k8s/redis-cluster/init-job.yaml
    
    # Wait for init job to complete
    log_info "Waiting for cluster initialization..."
    kubectl wait --for=condition=complete job/redis-cluster-init -n "$NAMESPACE" --timeout=180s
    
    # Clean up temporary files
    rm -f /tmp/redis-secret.yaml
    
    # Restore original StatefulSet if modified
    if [[ "$STORAGE_CLASS" != "standard" ]]; then
        mv k8s/redis-cluster/statefulset.yaml.bak k8s/redis-cluster/statefulset.yaml
    fi
    
    # Get cluster info
    log_success "Redis cluster deployed successfully to Kubernetes!"
    log_info "Namespace: $NAMESPACE"
    log_info ""
    log_info "Cluster endpoints:"
    kubectl get svc -n "$NAMESPACE" -o wide
    log_info ""
    log_info "Pod status:"
    kubectl get pods -n "$NAMESPACE" -o wide
    log_info ""
    log_info "To connect from within the cluster:"
    log_info "  Host: redis-cluster-service.$NAMESPACE.svc.cluster.local"
    log_info "  Port: 6379"
    log_info "  Password: $REDIS_PASSWORD"
    log_info ""
    log_info "For external access, check the LoadBalancer service:"
    kubectl get svc redis-cluster-service -n "$NAMESPACE"
}

# Main deployment logic
log_info "Starting Redis cluster deployment..."
log_info "Deployment type: $DEPLOYMENT_TYPE"
log_info "Environment: $ENVIRONMENT"
log_info "Namespace: $NAMESPACE"

case $DEPLOYMENT_TYPE in
    docker)
        deploy_docker
        ;;
    kubernetes)
        deploy_kubernetes
        ;;
    *)
        log_error "Invalid deployment type: $DEPLOYMENT_TYPE"
        exit 1
        ;;
esac

log_success "Deployment completed!"

# Health check
log_info "Running health check..."
sleep 5

case $DEPLOYMENT_TYPE in
    docker)
        if docker-compose -f docker-compose.redis-cluster.yml ps | grep -q "Up"; then
            log_success "Redis cluster is healthy and running"
        else
            log_warning "Some Redis nodes may not be running properly"
        fi
        ;;
    kubernetes)
        healthy_pods=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=redis-cluster --field-selector=status.phase=Running -o name | wc -l)
        if [[ $healthy_pods -eq 6 ]]; then
            log_success "All 6 Redis cluster nodes are healthy"
        else
            log_warning "Only $healthy_pods/6 Redis nodes are running"
        fi
        ;;
esac

log_info "Deployment script completed!"
echo ""
echo "========================================"
echo "Redis Cluster Connection Information"
echo "========================================"
echo "Password: $REDIS_PASSWORD"
echo ""
echo "Save this password securely!"
echo "You'll need it to configure your application."
echo "========================================"
