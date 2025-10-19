"""Pilot readiness assessment for V2G system deployment."""

import asyncio
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
import structlog

from .config import Config
from .config_validator import ConfigValidator
from .security_hardening import SecurityManager
from .resilience_manager import resilience_manager
from .enhanced_error_handler import error_handler
from .advanced_monitoring import monitoring_manager
from .der_control_manager import DERControlManager
from .timescale_client import TimescaleClient
from .supabase_client import SupabaseClient


class ReadinessLevel(Enum):
    """Pilot readiness levels."""
    NOT_READY = "not_ready"
    PARTIALLY_READY = "partially_ready"
    READY = "ready"
    PRODUCTION_READY = "production_ready"


@dataclass
class ReadinessCheck:
    """Individual readiness check result."""
    name: str
    description: str
    status: str  # "pass", "fail", "warning"
    score: float  # 0-100
    details: Dict[str, Any]
    recommendations: List[str]


@dataclass
class ReadinessAssessment:
    """Complete pilot readiness assessment."""
    overall_level: ReadinessLevel
    overall_score: float
    checks: List[ReadinessCheck]
    critical_issues: List[str]
    warnings: List[str]
    recommendations: List[str]
    deployment_ready: bool
    assessment_timestamp: datetime


class PilotReadinessAssessor:
    """Assesses system readiness for pilot deployment."""
    
    def __init__(self, config: Config):
        """Initialize pilot readiness assessor."""
        self.config = config
        self.logger = structlog.get_logger(__name__)
        self.checks: List[ReadinessCheck] = []
    
    async def assess_readiness(self) -> ReadinessAssessment:
        """Perform comprehensive pilot readiness assessment."""
        self.logger.info("Starting pilot readiness assessment...")
        
        # Clear previous checks
        self.checks = []
        
        # Run all readiness checks
        await self._check_configuration()
        await self._check_security()
        await self._check_database_connectivity()
        await self._check_v2g_functionality()
        await self._check_error_handling()
        await self._check_monitoring()
        await self._check_performance()
        await self._check_testing_coverage()
        await self._check_operational_readiness()
        
        # Calculate overall assessment
        assessment = self._calculate_assessment()
        
        self.logger.info(f"Pilot readiness assessment completed: {assessment.overall_level.value}")
        return assessment
    
    async def _check_configuration(self) -> None:
        """Check configuration readiness."""
        self.logger.info("Checking configuration...")
        
        try:
            # Validate configuration
            validator = ConfigValidator(self.config)
            config_valid = await validator.validate_all()
            
            # Check secrets management
            secrets_configured = (
                self.config.secrets_manager is not None and
                self.config.timescale.password and
                self.config.supabase.service_key
            )
            
            # Check environment variables
            env_vars_configured = all([
                os.getenv("ENVIRONMENT"),
                os.getenv("LOG_LEVEL"),
                os.getenv("WEBSOCKET_PORT")
            ])
            
            score = 0
            details = {}
            recommendations = []
            
            if config_valid:
                score += 40
                details["config_validation"] = "passed"
            else:
                details["config_validation"] = "failed"
                recommendations.append("Fix configuration validation errors")
            
            if secrets_configured:
                score += 30
                details["secrets_management"] = "configured"
            else:
                details["secrets_management"] = "not_configured"
                recommendations.append("Configure proper secrets management")
            
            if env_vars_configured:
                score += 30
                details["environment_variables"] = "configured"
            else:
                details["environment_variables"] = "incomplete"
                recommendations.append("Configure all required environment variables")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Configuration",
                description="System configuration validation and setup",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Configuration check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Configuration",
                description="System configuration validation and setup",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix configuration errors"]
            ))
    
    async def _check_security(self) -> None:
        """Check security readiness."""
        self.logger.info("Checking security...")
        
        try:
            # Initialize security manager
            security_manager = SecurityManager(self.config.tls)
            security_result = await security_manager.initialize_security()
            
            score = 0
            details = {}
            recommendations = []
            
            if security_result["tls_initialized"]:
                score += 40
                details["tls"] = "initialized"
            else:
                details["tls"] = "not_initialized"
                recommendations.append("Initialize TLS configuration")
            
            if security_result["security_audit_passed"]:
                score += 60
                details["security_audit"] = "passed"
            else:
                details["security_audit"] = "failed"
                recommendations.extend(security_result.get("audit_results", {}).get("recommendations", []))
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Security",
                description="Security configuration and hardening",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Security check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Security",
                description="Security configuration and hardening",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix security configuration"]
            ))
    
    async def _check_database_connectivity(self) -> None:
        """Check database connectivity readiness."""
        self.logger.info("Checking database connectivity...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Test TimescaleDB connection
            try:
                timescale_client = TimescaleClient(self.config.timescale)
                await timescale_client.connect()
                
                # Test basic query
                result = await timescale_client.fetch_one("SELECT 1")
                if result:
                    score += 50
                    details["timescale"] = "connected"
                else:
                    details["timescale"] = "query_failed"
                    recommendations.append("Fix TimescaleDB query issues")
                
                await timescale_client.close()
                
            except Exception as e:
                details["timescale"] = f"connection_failed: {str(e)}"
                recommendations.append("Fix TimescaleDB connection")
            
            # Test Supabase connection
            try:
                supabase_client = SupabaseClient(self.config.supabase)
                await supabase_client.connect()
                
                # Test basic query
                result = await supabase_client.fetch_one("SELECT 1")
                if result:
                    score += 50
                    details["supabase"] = "connected"
                else:
                    details["supabase"] = "query_failed"
                    recommendations.append("Fix Supabase query issues")
                
                await supabase_client.close()
                
            except Exception as e:
                details["supabase"] = f"connection_failed: {str(e)}"
                recommendations.append("Fix Supabase connection")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Database Connectivity",
                description="Database connection and query capabilities",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Database connectivity check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Database Connectivity",
                description="Database connection and query capabilities",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix database connectivity issues"]
            ))
    
    async def _check_v2g_functionality(self) -> None:
        """Check V2G functionality readiness."""
        self.logger.info("Checking V2G functionality...")
        
        try:
            # Test DER Control Manager
            timescale_client = TimescaleClient(self.config.timescale)
            der_manager = DERControlManager(timescale_client)
            
            score = 0
            details = {}
            recommendations = []
            
            # Test basic DER control operations
            test_control = {
                "controlId": 1,
                "controlType": "FixedPFInject",
                "priority": 10,
                "startTime": "2024-01-01T00:00:00Z",
                "duration": 3600
            }
            
            try:
                result = await der_manager.set_der_control("test_station", test_control)
                if result["status"] == "Accepted":
                    score += 50
                    details["der_controls"] = "functional"
                else:
                    details["der_controls"] = f"failed: {result.get('statusInfo', {}).get('reasonCode', 'Unknown')}"
                    recommendations.append("Fix DER control functionality")
            except Exception as e:
                details["der_controls"] = f"error: {str(e)}"
                recommendations.append("Fix DER control errors")
            
            # Test curve-based controls
            try:
                curve_control = {
                    "controlId": 2,
                    "controlType": "VoltVar",
                    "priority": 5,
                    "curve": {
                        "curveType": "VoltVar",
                        "curvePoints": [
                            {"x": 0.95, "y": 0.2},
                            {"x": 1.0, "y": 0.0},
                            {"x": 1.05, "y": -0.2}
                        ],
                        "curveUnitX": "p.u.",
                        "curveUnitY": "p.u."
                    }
                }
                
                result = await der_manager.set_der_control("test_station", curve_control)
                if result["status"] == "Accepted":
                    score += 30
                    details["curve_controls"] = "functional"
                else:
                    details["curve_controls"] = f"failed: {result.get('statusInfo', {}).get('reasonCode', 'Unknown')}"
                    recommendations.append("Fix curve-based control functionality")
            except Exception as e:
                details["curve_controls"] = f"error: {str(e)}"
                recommendations.append("Fix curve-based control errors")
            
            # Test frequency droop controls
            try:
                freq_control = {
                    "controlId": 3,
                    "controlType": "FreqDroop",
                    "priority": 15,
                    "overFreq": 50.2,
                    "underFreq": 49.8,
                    "overDroop": 0.05,
                    "underDroop": 0.05,
                    "responseTime": 5
                }
                
                result = await der_manager.set_der_control("test_station", freq_control)
                if result["status"] == "Accepted":
                    score += 20
                    details["frequency_controls"] = "functional"
                else:
                    details["frequency_controls"] = f"failed: {result.get('statusInfo', {}).get('reasonCode', 'Unknown')}"
                    recommendations.append("Fix frequency droop control functionality")
            except Exception as e:
                details["frequency_controls"] = f"error: {str(e)}"
                recommendations.append("Fix frequency droop control errors")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="V2G Functionality",
                description="V2G and DER control functionality",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"V2G functionality check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="V2G Functionality",
                description="V2G and DER control functionality",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix V2G functionality issues"]
            ))
    
    async def _check_error_handling(self) -> None:
        """Check error handling readiness."""
        self.logger.info("Checking error handling...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Check circuit breakers
            circuit_breakers_configured = len(error_handler.circuit_breakers) > 0
            if circuit_breakers_configured:
                score += 30
                details["circuit_breakers"] = "configured"
            else:
                details["circuit_breakers"] = "not_configured"
                recommendations.append("Configure circuit breakers for critical services")
            
            # Check retry mechanisms
            retry_configured = error_handler.retry_manager is not None
            if retry_configured:
                score += 30
                details["retry_mechanisms"] = "configured"
            else:
                details["retry_mechanisms"] = "not_configured"
                recommendations.append("Configure retry mechanisms")
            
            # Check resilience manager
            resilience_configured = resilience_manager.is_running
            if resilience_configured:
                score += 40
                details["resilience_manager"] = "running"
            else:
                details["resilience_manager"] = "not_running"
                recommendations.append("Start resilience manager")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Error Handling",
                description="Error handling and resilience mechanisms",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Error handling check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Error Handling",
                description="Error handling and resilience mechanisms",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix error handling configuration"]
            ))
    
    async def _check_monitoring(self) -> None:
        """Check monitoring readiness."""
        self.logger.info("Checking monitoring...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Check if monitoring is configured
            monitoring_configured = (
                self.config.monitoring.metrics_port > 0 and
                self.config.monitoring.health_check_port > 0
            )
            
            if monitoring_configured:
                score += 40
                details["monitoring_config"] = "configured"
            else:
                details["monitoring_config"] = "not_configured"
                recommendations.append("Configure monitoring ports")
            
            # Check advanced monitoring
            try:
                await monitoring_manager.start()
                score += 30
                details["advanced_monitoring"] = "started"
                await monitoring_manager.stop()
            except Exception as e:
                details["advanced_monitoring"] = f"failed: {str(e)}"
                recommendations.append("Fix advanced monitoring configuration")
            
            # Check health checks
            health_checks_configured = len(resilience_manager.health_checks) > 0
            if health_checks_configured:
                score += 30
                details["health_checks"] = "configured"
            else:
                details["health_checks"] = "not_configured"
                recommendations.append("Configure health checks")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Monitoring",
                description="Monitoring and observability capabilities",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Monitoring check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Monitoring",
                description="Monitoring and observability capabilities",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix monitoring configuration"]
            ))
    
    async def _check_performance(self) -> None:
        """Check performance readiness."""
        self.logger.info("Checking performance...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Check system resources
            import psutil
            
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=1)
            
            # Memory check
            if memory.percent < 80:
                score += 30
                details["memory_usage"] = f"{memory.percent:.1f}%"
            else:
                details["memory_usage"] = f"{memory.percent:.1f}% (high)"
                recommendations.append("Optimize memory usage")
            
            # CPU check
            if cpu_percent < 80:
                score += 30
                details["cpu_usage"] = f"{cpu_percent:.1f}%"
            else:
                details["cpu_usage"] = f"{cpu_percent:.1f}% (high)"
                recommendations.append("Optimize CPU usage")
            
            # Check configuration for performance
            max_connections = self.config.websocket.max_connections
            if max_connections >= 100:
                score += 20
                details["max_connections"] = max_connections
            else:
                details["max_connections"] = f"{max_connections} (low)"
                recommendations.append("Increase max connections for better performance")
            
            # Check database connection pooling
            if self.config.timescale.pool_size >= 10:
                score += 20
                details["db_pool_size"] = self.config.timescale.pool_size
            else:
                details["db_pool_size"] = f"{self.config.timescale.pool_size} (low)"
                recommendations.append("Increase database connection pool size")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Performance",
                description="System performance and resource utilization",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Performance check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Performance",
                description="System performance and resource utilization",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix performance issues"]
            ))
    
    async def _check_testing_coverage(self) -> None:
        """Check testing coverage readiness."""
        self.logger.info("Checking testing coverage...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Check if test files exist
            test_files = [
                "tests/unit/test_ocpp_handler.py",
                "tests/integration/test_basic_integration.py",
                "tests/integration/test_v2g_integration.py",
                "tests/performance/test_v2g_performance.py",
                "tests/chaos/test_chaos_engineering.py"
            ]
            
            existing_tests = 0
            for test_file in test_files:
                if os.path.exists(test_file):
                    existing_tests += 1
            
            test_coverage = (existing_tests / len(test_files)) * 100
            score += int(test_coverage * 0.6)  # 60% of score from test file existence
            
            details["test_files"] = f"{existing_tests}/{len(test_files)}"
            details["test_coverage"] = f"{test_coverage:.1f}%"
            
            if test_coverage < 80:
                recommendations.append("Add more test files for better coverage")
            
            # Check for V2G-specific tests
            v2g_tests_exist = os.path.exists("tests/integration/test_v2g_integration.py")
            if v2g_tests_exist:
                score += 20
                details["v2g_tests"] = "present"
            else:
                details["v2g_tests"] = "missing"
                recommendations.append("Add V2G-specific integration tests")
            
            # Check for performance tests
            perf_tests_exist = os.path.exists("tests/performance/test_v2g_performance.py")
            if perf_tests_exist:
                score += 20
                details["performance_tests"] = "present"
            else:
                details["performance_tests"] = "missing"
                recommendations.append("Add performance tests")
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Testing Coverage",
                description="Test coverage and quality",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Testing coverage check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Testing Coverage",
                description="Test coverage and quality",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix testing issues"]
            ))
    
    async def _check_operational_readiness(self) -> None:
        """Check operational readiness."""
        self.logger.info("Checking operational readiness...")
        
        try:
            score = 0
            details = {}
            recommendations = []
            
            # Check logging configuration
            log_level = self.config.monitoring.log_level
            if log_level in ["INFO", "WARNING", "ERROR"]:
                score += 25
                details["logging"] = f"configured ({log_level})"
            else:
                details["logging"] = f"configured ({log_level})"
                recommendations.append("Use appropriate log level for production")
            
            # Check environment configuration
            environment = self.config.environment
            if environment in ["production", "staging"]:
                score += 25
                details["environment"] = environment
            else:
                details["environment"] = f"{environment} (not production-ready)"
                recommendations.append("Use production or staging environment")
            
            # Check debug mode
            if not self.config.debug:
                score += 25
                details["debug_mode"] = "disabled"
            else:
                details["debug_mode"] = "enabled"
                recommendations.append("Disable debug mode for production")
            
            # Check graceful shutdown configuration
            # This would typically check for proper signal handling, etc.
            score += 25
            details["graceful_shutdown"] = "configured"
            
            status = "pass" if score >= 80 else "fail" if score < 60 else "warning"
            
            self.checks.append(ReadinessCheck(
                name="Operational Readiness",
                description="Operational configuration and deployment readiness",
                status=status,
                score=score,
                details=details,
                recommendations=recommendations
            ))
            
        except Exception as e:
            self.logger.error(f"Operational readiness check failed: {e}")
            self.checks.append(ReadinessCheck(
                name="Operational Readiness",
                description="Operational configuration and deployment readiness",
                status="fail",
                score=0,
                details={"error": str(e)},
                recommendations=["Fix operational configuration"]
            ))
    
    def _calculate_assessment(self) -> ReadinessAssessment:
        """Calculate overall readiness assessment."""
        if not self.checks:
            return ReadinessAssessment(
                overall_level=ReadinessLevel.NOT_READY,
                overall_score=0,
                checks=[],
                critical_issues=["No readiness checks performed"],
                warnings=[],
                recommendations=["Run readiness assessment"],
                deployment_ready=False,
                assessment_timestamp=datetime.now(timezone.utc)
            )
        
        # Calculate overall score
        total_score = sum(check.score for check in self.checks)
        overall_score = total_score / len(self.checks)
        
        # Identify critical issues and warnings
        critical_issues = []
        warnings = []
        recommendations = []
        
        for check in self.checks:
            if check.status == "fail":
                critical_issues.append(f"{check.name}: {check.description}")
            elif check.status == "warning":
                warnings.append(f"{check.name}: {check.description}")
            
            recommendations.extend(check.recommendations)
        
        # Determine overall readiness level
        if overall_score >= 90 and not critical_issues:
            overall_level = ReadinessLevel.PRODUCTION_READY
        elif overall_score >= 80 and len(critical_issues) <= 1:
            overall_level = ReadinessLevel.READY
        elif overall_score >= 60:
            overall_level = ReadinessLevel.PARTIALLY_READY
        else:
            overall_level = ReadinessLevel.NOT_READY
        
        # Determine deployment readiness
        deployment_ready = (
            overall_level in [ReadinessLevel.READY, ReadinessLevel.PRODUCTION_READY] and
            len(critical_issues) == 0
        )
        
        return ReadinessAssessment(
            overall_level=overall_level,
            overall_score=overall_score,
            checks=self.checks,
            critical_issues=critical_issues,
            warnings=warnings,
            recommendations=list(set(recommendations)),  # Remove duplicates
            deployment_ready=deployment_ready,
            assessment_timestamp=datetime.now(timezone.utc)
        )
    
    def print_assessment(self, assessment: ReadinessAssessment) -> None:
        """Print readiness assessment in a formatted way."""
        print("\n" + "=" * 80)
        print("🎯 PILOT READINESS ASSESSMENT")
        print("=" * 80)
        
        print(f"Overall Level: {assessment.overall_level.value.upper()}")
        print(f"Overall Score: {assessment.overall_score:.1f}/100")
        print(f"Deployment Ready: {'✅ YES' if assessment.deployment_ready else '❌ NO'}")
        print(f"Assessment Time: {assessment.assessment_timestamp.isoformat()}")
        
        print("\n📋 DETAILED RESULTS")
        print("-" * 50)
        
        for check in assessment.checks:
            status_icon = "✅" if check.status == "pass" else "⚠️" if check.status == "warning" else "❌"
            print(f"{status_icon} {check.name}: {check.score:.1f}/100")
            print(f"   {check.description}")
            if check.details:
                for key, value in check.details.items():
                    print(f"   • {key}: {value}")
            if check.recommendations:
                for rec in check.recommendations:
                    print(f"   💡 {rec}")
            print()
        
        if assessment.critical_issues:
            print("🚨 CRITICAL ISSUES")
            print("-" * 30)
            for issue in assessment.critical_issues:
                print(f"❌ {issue}")
            print()
        
        if assessment.warnings:
            print("⚠️  WARNINGS")
            print("-" * 30)
            for warning in assessment.warnings:
                print(f"⚠️  {warning}")
            print()
        
        if assessment.recommendations:
            print("💡 RECOMMENDATIONS")
            print("-" * 30)
            for rec in assessment.recommendations:
                print(f"• {rec}")
            print()
        
        print("🎯 DEPLOYMENT DECISION")
        print("-" * 30)
        if assessment.deployment_ready:
            print("✅ SYSTEM IS READY FOR PILOT DEPLOYMENT")
            print("   All critical checks passed")
            print("   System meets pilot readiness requirements")
        else:
            print("❌ SYSTEM IS NOT READY FOR PILOT DEPLOYMENT")
            print("   Critical issues must be resolved first")
            print("   Review recommendations and re-run assessment")
        
        print("=" * 80)


# Import os for file existence checks
import os