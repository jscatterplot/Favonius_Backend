# Production Readiness Checklist

## Favonius Energy EV Fleet Depot Optimization Platform

**Reference:** PRD_v2.md, Development Plan Phase 7

This checklist ensures all deployment components are properly configured for production.

## Pre-Deployment Checklist

### 1. Security Configuration

- [ ] **TLS/SSL Certificates**
  - [ ] TLS certificates configured for API (HTTPS)
  - [ ] TLS certificates configured for OCPP (WSS)
  - [ ] Cert-manager installed and configured (Kubernetes)
  - [ ] TLS 1.3 enabled (per PRD Section 10.3)

- [ ] **Secrets Management**
  - [ ] Database password stored in secure secret manager
  - [ ] JWT secret key generated and stored securely
  - [ ] Gurobi license file stored securely
  - [ ] No secrets committed to repository
  - [ ] External secrets operator configured (if using)

- [ ] **Authentication**
  - [ ] JWT authentication enabled (per PRD Section 10.3)
  - [ ] API keys configured for inter-depot communication
  - [ ] OCPP authentication configured (Basic auth + TLS)

- [ ] **Network Security**
  - [ ] Firewall rules configured
  - [ ] Ingress rate limiting enabled (per PRD Section 10.4)
  - [ ] CORS configured for production domains
  - [ ] Network policies configured (Kubernetes)

### 2. Database Configuration

- [ ] **TimescaleDB Setup**
  - [ ] Database schema deployed (migrations/001_initial_schema.sql)
  - [ ] Hypertables created for time-series data
  - [ ] Compression policies configured
  - [ ] Retention policies configured
  - [ ] Backup strategy implemented
  - [ ] Connection pooling configured

- [ ] **Alternative: Managed Database**
  - [ ] Managed TimescaleDB service configured (AWS RDS, GCP Cloud SQL, etc.)
  - [ ] Connection string configured
  - [ ] Backup and high availability enabled

### 3. Solver Configuration

- [ ] **Gurobi License**
  - [ ] Gurobi license file obtained and validated
  - [ ] License file mounted in container/Kubernetes
  - [ ] License status verified via `/health` endpoint
  - [ ] License expiration date documented

- [ ] **HiGHS Fallback**
  - [ ] HiGHS solver installed and available
  - [ ] Fallback logic tested
  - [ ] Monitoring for fallback events configured

### 4. Monitoring and Observability

- [ ] **Health Checks**
  - [ ] `/health` endpoint returns 200 OK
  - [ ] All components report healthy status
  - [ ] Gurobi license status verified
  - [ ] Health check response time < 500ms (per PRD Section 10.1)

- [ ] **Metrics**
  - [ ] Prometheus configured and scraping metrics
  - [ ] `/metrics` endpoint accessible
  - [ ] Key metrics defined:
    - Optimization runs, duration, failures
    - Solver usage (Gurobi vs HiGHS)
    - OCPP connection counts
    - API response times
    - Database query performance

- [ ] **Logging**
  - [ ] Structured JSON logging configured
  - [ ] Log aggregation configured (ELK, Loki, CloudWatch)
  - [ ] Log retention policy configured
  - [ ] Sensitive data excluded from logs

- [ ] **Alerting**
  - [ ] Critical alerts configured:
    - System availability < 99.5%
    - Optimization failures
    - Database connectivity issues
    - Gurobi license expiration warnings
  - [ ] Alert channels configured (email, Slack, PagerDuty)

### 5. Performance Configuration

- [ ] **Resource Limits**
  - [ ] API pods: 4GB memory, 2 CPU (per PRD Section 8.2)
  - [ ] Database: 2GB memory, 1 CPU
  - [ ] Resource requests set appropriately

- [ ] **Autoscaling**
  - [ ] HPA configured (min 2, max 10 replicas)
  - [ ] CPU target: 70%
  - [ ] Memory target: 80%
  - [ ] Autoscaling tested under load

- [ ] **Optimization Settings**
  - [ ] Timeout: 60 seconds (per PRD Section 8.2)
  - [ ] MIP gap: 1% (0.01)
  - [ ] Warm-starting enabled
  - [ ] Solve time < 60s verified for 20 vehicles

### 6. High Availability

- [ ] **Replication**
  - [ ] Minimum 2 API replicas running
  - [ ] Pod Disruption Budget configured
  - [ ] Pod anti-affinity configured
  - [ ] Database replication configured (if self-hosted)

- [ ] **Rolling Updates**
  - [ ] Rolling update strategy tested
  - [ ] Zero-downtime deployments verified
  - [ ] Rollback procedure documented

### 7. Data Freshness

- [ ] **Data Sources**
  - [ ] Telemetry data freshness: < 15 minutes (per PRD Section 5.3)
  - [ ] Price data freshness: < 24 hours
  - [ ] Weather data freshness: < 6 hours
  - [ ] Building load data freshness: < 30 minutes
  - [ ] Staleness checks implemented

### 8. Integration Points

- [ ] **OCPP Server**
  - [ ] OCPP 1.6 primary support verified
  - [ ] OCPP 2.0.1 ready (if needed)
  - [ ] WebSocket connections tested
  - [ ] Session affinity configured (3-hour timeout)
  - [ ] Charger registration verified

- [ ] **Price Feeds**
  - [ ] CAISO OASIS API access configured
  - [ ] Fallback TOU pricing configured
  - [ ] Price ingestion tested

- [ ] **Weather API**
  - [ ] Open-Meteo API access configured
  - [ ] Forecast ingestion tested

- [ ] **Building Load**
  - [ ] Building load source configured (meter, API, or forecast)
  - [ ] Data ingestion tested
  - [ ] Fallback forecast model ready

### 9. Testing

- [ ] **Unit Tests**
  - [ ] Test coverage ≥ 90% for optimizer
  - [ ] Test coverage ≥ 90% for surrogate model
  - [ ] All unit tests passing

- [ ] **Integration Tests**
  - [ ] Full pipeline test passing
  - [ ] OCPP integration tested
  - [ ] Database operations tested

- [ ] **Performance Tests**
  - [ ] Solve time < 60s for 20 vehicles verified
  - [ ] API response time < 500ms (99th percentile)
  - [ ] Load testing completed

### 10. Documentation

- [ ] **Deployment Documentation**
  - [ ] `docs/DEPLOYMENT.md` reviewed and accurate
  - [ ] Kubernetes deployment guide complete
  - [ ] Docker Compose guide complete
  - [ ] Troubleshooting guide available

- [ ] **API Documentation**
  - [ ] OpenAPI spec up to date
  - [ ] API endpoints documented
  - [ ] Authentication documented

- [ ] **Runbooks**
  - [ ] Incident response procedures documented
  - [ ] Common issues and solutions documented
  - [ ] Escalation procedures defined

## Deployment Verification

After deployment, run the verification script:

```bash
# Docker Compose
./scripts/deploy/verify_deployment.sh

# Kubernetes
API_URL=https://api.yourdomain.com ./scripts/deploy/verify_deployment.sh
```

Expected results:
- ✓ Health endpoint returns 200 OK
- ✓ All components healthy
- ✓ Metrics endpoint accessible
- ✓ Database connectivity verified

## Post-Deployment Monitoring

### First 24 Hours

- [ ] Monitor health endpoint every 5 minutes
- [ ] Check optimization runs are completing successfully
- [ ] Verify OCPP charger connections
- [ ] Monitor error rates and logs
- [ ] Check resource utilization
- [ ] Verify autoscaling behavior

### First Week

- [ ] Review optimization performance metrics
- [ ] Verify demand charge reduction targets
- [ ] Check solver usage (Gurobi vs HiGHS)
- [ ] Review API response times
- [ ] Validate data freshness
- [ ] Check for any recurring errors

## Rollback Plan

If issues are detected:

1. **Immediate Rollback**
   ```bash
   # Kubernetes
   kubectl rollout undo deployment/favonius-api -n ev-charging
   
   # Docker Compose
   docker-compose down
   docker-compose up -d <previous-version>
   ```

2. **Investigation**
   - Check logs: `kubectl logs -n ev-charging deployment/favonius-api`
   - Review metrics in Prometheus/Grafana
   - Check health endpoint status

3. **Communication**
   - Notify stakeholders
   - Document issue and resolution
   - Update runbook if needed

## Success Criteria

Per PRD Section 1.3 and 11.1:

- [ ] **Demand Charge Reduction**: ≥ 30% reduction achieved
- [ ] **Optimization Solve Time**: < 60 seconds (95th percentile)
- [ ] **Vehicle Departure SoC**: 100% of departures ≥ 99% SoC
- [ ] **System Uptime**: ≥ 99.5% (excluding maintenance)
- [ ] **API Response Time**: < 500ms (99th percentile)
- [ ] **Energy Consumption Prediction**: R² ≥ 0.85

## References

- **PRD**: `docs/PRD_v2.md`
- **Deployment Guide**: `docs/DEPLOYMENT.md`
- **Development Plan**: `favonius_development_plan_v2.md`
- **API Documentation**: `docs/API.md`

---

**Last Updated**: 2025-12-13
**Version**: 1.0

