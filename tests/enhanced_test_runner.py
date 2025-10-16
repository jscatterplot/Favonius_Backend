"""Enhanced test runner with comprehensive reporting and automation."""

import pytest
import asyncio
import subprocess
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any
import logging

# Import test dependencies
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from e2e.citrineos_simulator import CitrineOSSimulator, CitrineOSFleetSimulator
from websocket_handler.server import OCPPWebSocketServer
from websocket_handler.config import Config


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EnhancedTestRunner:
    """Enhanced test runner with comprehensive reporting and automation."""

    def __init__(self, config: Config):
        self.config = config
        self.test_results = {}
        self.start_time = None
        self.end_time = None

    async def run_unit_tests(self) -> Dict[str, Any]:
        """Run unit tests with enhanced reporting."""
        logger.info("Running unit tests...")
        
        start_time = datetime.now()
        
        try:
            # Run pytest for unit tests
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/unit/",
                "-v",
                "--tb=short",
                "--junitxml=tests/results/unit_tests.xml",
                "--cov=src/websocket_handler",
                "--cov-report=xml:tests/results/coverage.xml",
                "--cov-report=html:tests/results/coverage_html"
            ], capture_output=True, text=True, cwd=os.getcwd())
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Parse results
            unit_results = {
                "status": "passed" if result.returncode == 0 else "failed",
                "duration": duration,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "test_count": self._parse_test_count(result.stdout),
                "coverage": self._parse_coverage(result.stdout)
            }
            
            logger.info(f"Unit tests completed: {unit_results['status']} in {duration:.2f}s")
            return unit_results
            
        except Exception as e:
            logger.error(f"Unit tests failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_integration_tests(self) -> Dict[str, Any]:
        """Run integration tests with enhanced reporting."""
        logger.info("Running integration tests...")
        
        start_time = datetime.now()
        
        try:
            # Run pytest for integration tests
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/integration/",
                "-v",
                "--tb=short",
                "--junitxml=tests/results/integration_tests.xml"
            ], capture_output=True, text=True, cwd=os.getcwd())
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Parse results
            integration_results = {
                "status": "passed" if result.returncode == 0 else "failed",
                "duration": duration,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "test_count": self._parse_test_count(result.stdout)
            }
            
            logger.info(f"Integration tests completed: {integration_results['status']} in {duration:.2f}s")
            return integration_results
            
        except Exception as e:
            logger.error(f"Integration tests failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_load_tests(self) -> Dict[str, Any]:
        """Run load tests with enhanced reporting."""
        logger.info("Running load tests...")
        
        start_time = datetime.now()
        
        try:
            # Run pytest for load tests
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/load/",
                "-v",
                "--tb=short",
                "--junitxml=tests/results/load_tests.xml"
            ], capture_output=True, text=True, cwd=os.getcwd())
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Parse results
            load_results = {
                "status": "passed" if result.returncode == 0 else "failed",
                "duration": duration,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "test_count": self._parse_test_count(result.stdout)
            }
            
            logger.info(f"Load tests completed: {load_results['status']} in {duration:.2f}s")
            return load_results
            
        except Exception as e:
            logger.error(f"Load tests failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_security_tests(self) -> Dict[str, Any]:
        """Run security tests with enhanced reporting."""
        logger.info("Running security tests...")
        
        start_time = datetime.now()
        
        try:
            # Run pytest for security tests
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/security/",
                "-v",
                "--tb=short",
                "--junitxml=tests/results/security_tests.xml"
            ], capture_output=True, text=True, cwd=os.getcwd())
            
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            
            # Parse results
            security_results = {
                "status": "passed" if result.returncode == 0 else "failed",
                "duration": duration,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "test_count": self._parse_test_count(result.stdout)
            }
            
            logger.info(f"Security tests completed: {security_results['status']} in {duration:.2f}s")
            return security_results
            
        except Exception as e:
            logger.error(f"Security tests failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_end_to_end_tests(self) -> Dict[str, Any]:
        """Run end-to-end tests with enhanced reporting."""
        logger.info("Running end-to-end tests...")
        
        start_time = datetime.now()
        
        try:
            # Start test server
            server = WebSocketServer(self.config)
            await server.start()
            
            try:
                # Run pytest for end-to-end tests
                result = subprocess.run([
                    sys.executable, "-m", "pytest", 
                    "tests/e2e/",
                    "-v",
                    "--tb=short",
                    "--junitxml=tests/results/e2e_tests.xml"
                ], capture_output=True, text=True, cwd=os.getcwd())
                
                end_time = datetime.now()
                duration = (end_time - start_time).total_seconds()
                
                # Parse results
                e2e_results = {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "duration": duration,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "test_count": self._parse_test_count(result.stdout)
                }
                
                logger.info(f"End-to-end tests completed: {e2e_results['status']} in {duration:.2f}s")
                return e2e_results
                
            finally:
                await server.stop()
                
        except Exception as e:
            logger.error(f"End-to-end tests failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_citrineos_simulation(self) -> Dict[str, Any]:
        """Run CitrineOS simulation tests."""
        logger.info("Running CitrineOS simulation tests...")
        
        start_time = datetime.now()
        
        try:
            # Start test server
            server = WebSocketServer(self.config)
            await server.start()
            
            try:
                # Run CitrineOS simulation
                simulator = CitrineOSSimulator("ENHANCED_TEST_001", "ws://localhost:9000")
                
                await simulator.connect()
                
                # Boot notification
                boot_result = await simulator.boot_notification()
                assert boot_result[2]["status"] == "Accepted"
                
                # Heartbeat
                heartbeat_result = await simulator.heartbeat()
                assert heartbeat_result[2]["status"] == "Accepted"
                
                # Meter values
                meter_result = await simulator.meter_values(1, 22.5)
                assert meter_result is None
                
                # Status notification
                status_result = await simulator.status_notification(1, "Available")
                assert status_result is None
                
                await simulator.disconnect()
                
                end_time = datetime.now()
                duration = (end_time - start_time).total_seconds()
                
                citrineos_results = {
                    "status": "passed",
                    "duration": duration,
                    "test_count": 4,
                    "simulation_type": "single_station"
                }
                
                logger.info(f"CitrineOS simulation completed: {citrineos_results['status']} in {duration:.2f}s")
                return citrineos_results
                
            finally:
                await server.stop()
                
        except Exception as e:
            logger.error(f"CitrineOS simulation failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_fleet_simulation(self) -> Dict[str, Any]:
        """Run fleet simulation tests."""
        logger.info("Running fleet simulation tests...")
        
        start_time = datetime.now()
        
        try:
            # Start test server
            server = WebSocketServer(self.config)
            await server.start()
            
            try:
                # Run fleet simulation
                fleet = CitrineOSFleetSimulator(num_stations=20, server_url="ws://localhost:9000")
                
                await fleet.connect_all()
                
                # Boot all stations
                boot_results = await fleet.boot_all_stations()
                successful_boots = sum(1 for result in boot_results if result[2].get("status") == "Accepted")
                
                # Test various operations
                await fleet.test_device_configuration()
                await fleet.test_charging_profiles()
                await fleet.start_charging_sessions(10)
                await fleet.send_meter_values([22.5] * 20)
                await fleet.test_monitoring()
                await fleet.test_display_messages()
                await fleet.test_privacy_compliance()
                await fleet.heartbeat_all()
                
                await fleet.disconnect_all()
                
                end_time = datetime.now()
                duration = (end_time - start_time).total_seconds()
                
                fleet_results = {
                    "status": "passed" if successful_boots >= 18 else "failed",
                    "duration": duration,
                    "test_count": 8,
                    "simulation_type": "fleet",
                    "stations": 20,
                    "successful_boots": successful_boots
                }
                
                logger.info(f"Fleet simulation completed: {fleet_results['status']} in {duration:.2f}s")
                return fleet_results
                
            finally:
                await server.stop()
                
        except Exception as e:
            logger.error(f"Fleet simulation failed: {e}")
            return {
                "status": "error",
                "duration": 0,
                "error": str(e)
            }

    async def run_all_tests(self) -> Dict[str, Any]:
        """Run all test suites with comprehensive reporting."""
        logger.info("Starting comprehensive test suite...")
        
        self.start_time = datetime.now()
        
        # Create results directory
        self.create_test_results_directory()
        
        # Run all test suites
        test_suites = {
            "unit_tests": self.run_unit_tests(),
            "integration_tests": self.run_integration_tests(),
            "load_tests": self.run_load_tests(),
            "security_tests": self.run_security_tests(),
            "end_to_end_tests": self.run_end_to_end_tests(),
            "citrineos_simulation": self.run_citrineos_simulation(),
            "fleet_simulation": self.run_fleet_simulation()
        }
        
        # Wait for all tests to complete
        results = {}
        for suite_name, coro in test_suites.items():
            results[suite_name] = await coro
        
        self.end_time = datetime.now()
        total_duration = (self.end_time - self.start_time).total_seconds()
        
        # Generate comprehensive report
        comprehensive_report = self.generate_comprehensive_report(results, total_duration)
        
        # Save report
        self.save_comprehensive_report(comprehensive_report)
        
        logger.info(f"Comprehensive test suite completed in {total_duration:.2f}s")
        return comprehensive_report

    def create_test_results_directory(self):
        """Create test results directory."""
        results_dir = Path("tests/results")
        results_dir.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        (results_dir / "coverage_html").mkdir(exist_ok=True)
        (results_dir / "reports").mkdir(exist_ok=True)

    def generate_comprehensive_report(self, results: Dict[str, Any], total_duration: float) -> Dict[str, Any]:
        """Generate comprehensive test report."""
        
        # Calculate overall statistics
        total_tests = sum(result.get("test_count", 0) for result in results.values())
        passed_tests = sum(1 for result in results.values() if result.get("status") == "passed")
        failed_tests = sum(1 for result in results.values() if result.get("status") == "failed")
        error_tests = sum(1 for result in results.values() if result.get("status") == "error")
        
        # Calculate coverage
        coverage = results.get("unit_tests", {}).get("coverage", {})
        
        # Generate report
        report = {
            "test_suite": "Favonius Energy V2G System",
            "timestamp": self.start_time.isoformat(),
            "duration": total_duration,
            "overall_status": "passed" if failed_tests == 0 and error_tests == 0 else "failed",
            "statistics": {
                "total_tests": total_tests,
                "passed_tests": passed_tests,
                "failed_tests": failed_tests,
                "error_tests": error_tests,
                "success_rate": (passed_tests / len(results)) * 100 if results else 0
            },
            "coverage": coverage,
            "test_suites": results,
            "recommendations": self._generate_recommendations(results),
            "next_steps": self._generate_next_steps(results)
        }
        
        return report

    def save_comprehensive_report(self, report: Dict[str, Any]):
        """Save comprehensive test report."""
        
        # Save JSON report
        json_path = Path("tests/results/reports/comprehensive_report.json")
        with open(json_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        
        # Save Markdown report
        markdown_path = Path("tests/results/reports/comprehensive_report.md")
        with open(markdown_path, "w") as f:
            f.write(self._generate_markdown_report(report))
        
        logger.info(f"Comprehensive report saved to {json_path} and {markdown_path}")

    def _generate_markdown_report(self, report: Dict[str, Any]) -> str:
        """Generate Markdown test report."""
        
        markdown = f"""# Comprehensive Test Report

## Test Suite: {report['test_suite']}
**Timestamp:** {report['timestamp']}  
**Duration:** {report['duration']:.2f} seconds  
**Overall Status:** {report['overall_status'].upper()}

## Statistics
- **Total Tests:** {report['statistics']['total_tests']}
- **Passed:** {report['statistics']['passed_tests']}
- **Failed:** {report['statistics']['failed_tests']}
- **Errors:** {report['statistics']['error_tests']}
- **Success Rate:** {report['statistics']['success_rate']:.1f}%

## Coverage
- **Overall Coverage:** {report['coverage'].get('overall', 'N/A')}%
- **Line Coverage:** {report['coverage'].get('lines', 'N/A')}%
- **Branch Coverage:** {report['coverage'].get('branches', 'N/A')}%

## Test Suites

"""
        
        for suite_name, suite_result in report['test_suites'].items():
            status_emoji = "✅" if suite_result.get("status") == "passed" else "❌"
            markdown += f"### {status_emoji} {suite_name.replace('_', ' ').title()}\n"
            markdown += f"- **Status:** {suite_result.get('status', 'unknown')}\n"
            markdown += f"- **Duration:** {suite_result.get('duration', 0):.2f}s\n"
            markdown += f"- **Tests:** {suite_result.get('test_count', 0)}\n"
            
            if suite_result.get("error"):
                markdown += f"- **Error:** {suite_result['error']}\n"
            
            markdown += "\n"
        
        # Add recommendations
        if report['recommendations']:
            markdown += "## Recommendations\n\n"
            for recommendation in report['recommendations']:
                markdown += f"- {recommendation}\n"
            markdown += "\n"
        
        # Add next steps
        if report['next_steps']:
            markdown += "## Next Steps\n\n"
            for step in report['next_steps']:
                markdown += f"- {step}\n"
            markdown += "\n"
        
        return markdown

    def _parse_test_count(self, output: str) -> int:
        """Parse test count from pytest output."""
        try:
            lines = output.split('\n')
            for line in lines:
                if 'passed' in line and 'failed' in line:
                    # Extract number from line like "5 passed, 1 failed"
                    parts = line.split()
                    for part in parts:
                        if part.isdigit():
                            return int(part)
        except:
            pass
        return 0

    def _parse_coverage(self, output: str) -> Dict[str, str]:
        """Parse coverage information from pytest output."""
        coverage = {}
        try:
            lines = output.split('\n')
            for line in lines:
                if 'TOTAL' in line and '%' in line:
                    # Extract coverage percentages
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part == 'TOTAL':
                            if i + 1 < len(parts):
                                coverage['overall'] = parts[i + 1].replace('%', '')
                            break
        except:
            pass
        return coverage

    def _generate_recommendations(self, results: Dict[str, Any]) -> List[str]:
        """Generate recommendations based on test results."""
        recommendations = []
        
        for suite_name, result in results.items():
            if result.get("status") == "failed":
                recommendations.append(f"Investigate and fix failures in {suite_name}")
            elif result.get("status") == "error":
                recommendations.append(f"Resolve errors in {suite_name}")
            elif result.get("duration", 0) > 60:  # More than 1 minute
                recommendations.append(f"Optimize performance of {suite_name}")
        
        # Coverage recommendations
        unit_coverage = results.get("unit_tests", {}).get("coverage", {})
        if unit_coverage.get("overall", "0").replace("%", "") < "80":
            recommendations.append("Improve unit test coverage to at least 80%")
        
        return recommendations

    def _generate_next_steps(self, results: Dict[str, Any]) -> List[str]:
        """Generate next steps based on test results."""
        next_steps = []
        
        # Check if all tests passed
        all_passed = all(result.get("status") == "passed" for result in results.values())
        
        if all_passed:
            next_steps.extend([
                "Deploy to staging environment",
                "Run production smoke tests",
                "Prepare for pilot deployment",
                "Update documentation"
            ])
        else:
            next_steps.extend([
                "Fix failing tests",
                "Address test errors",
                "Re-run test suite",
                "Review test coverage"
            ])
        
        return next_steps


async def main():
    """Main function to run comprehensive test suite."""
    
    # Create configuration
    config = Config()
    
    # Create test runner
    test_runner = EnhancedTestRunner(config)
    
    # Run all tests
    report = await test_runner.run_all_tests()
    
    # Print summary
    print("\n" + "="*50)
    print("COMPREHENSIVE TEST SUITE COMPLETED")
    print("="*50)
    print(f"Overall Status: {report['overall_status'].upper()}")
    print(f"Duration: {report['duration']:.2f} seconds")
    print(f"Success Rate: {report['statistics']['success_rate']:.1f}%")
    print(f"Total Tests: {report['statistics']['total_tests']}")
    print("="*50)
    
    # Print test suite results
    for suite_name, result in report['test_suites'].items():
        status_emoji = "✅" if result.get("status") == "passed" else "❌"
        print(f"{status_emoji} {suite_name.replace('_', ' ').title()}: {result.get('status', 'unknown')}")
    
    print("="*50)
    
    # Print recommendations
    if report['recommendations']:
        print("\nRecommendations:")
        for recommendation in report['recommendations']:
            print(f"- {recommendation}")
    
    # Print next steps
    if report['next_steps']:
        print("\nNext Steps:")
        for step in report['next_steps']:
            print(f"- {step}")
    
    print("\nDetailed report saved to tests/results/reports/")


if __name__ == "__main__":
    asyncio.run(main())
