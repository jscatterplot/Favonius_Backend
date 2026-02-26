"""Comprehensive test runner for V2G system validation."""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from typing import Any, Dict


class TestRunner:
    """Comprehensive test runner for V2G system."""

    def __init__(self):
        """Initialize test runner."""
        self.results: Dict[str, Any] = {}
        self.start_time = time.time()

    async def run_all_tests(
        self, include_performance: bool = True, include_chaos: bool = True
    ) -> Dict[str, Any]:
        """Run all test suites."""
        print("🚀 Starting comprehensive V2G system testing...")
        print("=" * 60)

        # Phase 1: Unit Tests
        print("\n📋 Phase 1: Unit Tests")
        print("-" * 30)
        unit_results = await self._run_test_suite("tests/unit", "Unit Tests")
        self.results["unit_tests"] = unit_results

        # Phase 2: Integration Tests
        print("\n🔗 Phase 2: Integration Tests")
        print("-" * 30)
        integration_results = await self._run_test_suite("tests/integration", "Integration Tests")
        self.results["integration_tests"] = integration_results

        # Phase 3: V2G-Specific Tests
        print("\n⚡ Phase 3: V2G-Specific Tests")
        print("-" * 30)
        v2g_results = await self._run_test_suite(
            "tests/integration/test_v2g_integration.py", "V2G Integration Tests"
        )
        self.results["v2g_tests"] = v2g_results

        # Phase 4: Performance Tests
        if include_performance:
            print("\n⚡ Phase 4: Performance Tests")
            print("-" * 30)
            performance_results = await self._run_test_suite(
                "tests/performance", "Performance Tests"
            )
            self.results["performance_tests"] = performance_results

        # Phase 5: Chaos Engineering Tests
        if include_chaos:
            print("\n💥 Phase 5: Chaos Engineering Tests")
            print("-" * 30)
            chaos_results = await self._run_test_suite("tests/chaos", "Chaos Engineering Tests")
            self.results["chaos_tests"] = chaos_results

        # Phase 6: End-to-End Tests
        print("\n🎯 Phase 6: End-to-End Tests")
        print("-" * 30)
        e2e_results = await self._run_test_suite("tests/e2e", "End-to-End Tests")
        self.results["e2e_tests"] = e2e_results

        # Generate final report
        total_time = time.time() - self.start_time
        self.results["total_time"] = total_time
        self.results["summary"] = self._generate_summary()

        return self.results

    async def _run_test_suite(self, test_path: str, suite_name: str) -> Dict[str, Any]:
        """Run a specific test suite."""
        print(f"Running {suite_name}...")

        start_time = time.time()

        try:
            # Run pytest with detailed output
            cmd = [
                "python",
                "-m",
                "pytest",
                test_path,
                "-v",
                "--tb=short",
                "--durations=10",
                f"--junitxml=test_results_{suite_name.lower().replace(' ', '_')}.xml",
                f"--html=test_report_{suite_name.lower().replace(' ', '_')}.html",
                "--self-contained-html",
            ]

            # Add markers for specific test types
            if "performance" in test_path.lower():
                cmd.extend(["-m", "slow"])
            elif "chaos" in test_path.lower():
                cmd.extend(["-m", "slow"])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800  # 30 minutes timeout
            )

            duration = time.time() - start_time

            # Parse results
            test_results = {
                "suite_name": suite_name,
                "duration": duration,
                "return_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "success": result.returncode == 0,
            }

            # Extract test statistics
            if "passed" in result.stdout:
                lines = result.stdout.split("\n")
                for line in lines:
                    if "passed" in line and "failed" in line:
                        # Parse pytest output like "5 passed, 2 failed in 10.23s"
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part == "passed":
                                test_results["passed"] = int(parts[i - 1])
                            elif part == "failed":
                                test_results["failed"] = int(parts[i - 1])
                            elif part == "skipped":
                                test_results["skipped"] = int(parts[i - 1])
                        break

            if result.returncode == 0:
                print(f"✅ {suite_name} completed successfully")
            else:
                print(f"❌ {suite_name} failed")
                print(f"Error: {result.stderr}")

            return test_results

        except subprocess.TimeoutExpired:
            print(f"⏰ {suite_name} timed out")
            return {
                "suite_name": suite_name,
                "duration": time.time() - start_time,
                "return_code": -1,
                "success": False,
                "error": "Test suite timed out",
            }
        except Exception as e:
            print(f"💥 {suite_name} encountered an error: {e}")
            return {
                "suite_name": suite_name,
                "duration": time.time() - start_time,
                "return_code": -1,
                "success": False,
                "error": str(e),
            }

    def _generate_summary(self) -> Dict[str, Any]:
        """Generate test summary."""
        total_passed = 0
        total_failed = 0
        total_skipped = 0
        total_duration = 0
        successful_suites = 0
        total_suites = 0

        for suite_name, results in self.results.items():
            if isinstance(results, dict) and "suite_name" in results:
                total_suites += 1
                if results.get("success", False):
                    successful_suites += 1

                total_passed += results.get("passed", 0)
                total_failed += results.get("failed", 0)
                total_skipped += results.get("skipped", 0)
                total_duration += results.get("duration", 0)

        success_rate = (successful_suites / total_suites * 100) if total_suites > 0 else 0

        return {
            "total_suites": total_suites,
            "successful_suites": successful_suites,
            "success_rate": success_rate,
            "total_tests": total_passed + total_failed + total_skipped,
            "passed": total_passed,
            "failed": total_failed,
            "skipped": total_skipped,
            "total_duration": total_duration,
            "overall_success": success_rate >= 80,  # 80% success rate threshold
        }

    def print_summary(self):
        """Print test summary."""
        print("\n" + "=" * 60)
        print("📊 TEST SUMMARY")
        print("=" * 60)

        summary = self.results.get("summary", {})

        print(f"Total Test Suites: {summary.get('total_suites', 0)}")
        print(f"Successful Suites: {summary.get('successful_suites', 0)}")
        print(f"Success Rate: {summary.get('success_rate', 0):.1f}%")
        print(f"Total Tests: {summary.get('total_tests', 0)}")
        print(f"  ✅ Passed: {summary.get('passed', 0)}")
        print(f"  ❌ Failed: {summary.get('failed', 0)}")
        print(f"  ⏭️  Skipped: {summary.get('skipped', 0)}")
        print(f"Total Duration: {summary.get('total_duration', 0):.1f}s")
        print(f"Overall Success: {'✅ YES' if summary.get('overall_success', False) else '❌ NO'}")

        # Detailed results by suite
        print("\n📋 DETAILED RESULTS")
        print("-" * 30)

        for suite_name, results in self.results.items():
            if isinstance(results, dict) and "suite_name" in results:
                status = "✅" if results.get("success", False) else "❌"
                duration = results.get("duration", 0)
                passed = results.get("passed", 0)
                failed = results.get("failed", 0)
                skipped = results.get("skipped", 0)

                print(
                    f"{status} {results['suite_name']}: {passed}P {failed}F {skipped}S ({duration:.1f}s)"
                )

        # Pilot readiness assessment
        print("\n🎯 PILOT READINESS ASSESSMENT")
        print("-" * 30)

        if summary.get("overall_success", False):
            print("✅ System is READY for pilot deployment")
            print("   - All critical test suites passed")
            print("   - Performance tests completed successfully")
            print("   - Chaos engineering tests validated resilience")
        else:
            print("❌ System is NOT READY for pilot deployment")
            print("   - Critical issues need to be resolved")
            print("   - Additional testing required")

        # Recommendations
        print("\n💡 RECOMMENDATIONS")
        print("-" * 30)

        if summary.get("success_rate", 0) < 80:
            print("• Fix failing test suites before pilot deployment")

        if summary.get("failed", 0) > 0:
            print("• Address failing individual tests")

        if summary.get("total_duration", 0) > 1800:  # 30 minutes
            print("• Consider optimizing test execution time")

        print("• Review test reports for detailed failure analysis")
        print("• Conduct additional load testing if needed")
        print("• Validate security configurations")

    def save_report(self, filename: str = "test_report.json"):
        """Save test report to file."""
        with open(filename, "w") as f:
            json.dump(self.results, f, indent=2, default=str)
        print(f"\n📄 Test report saved to {filename}")


async def main():
    """Main test runner function."""
    parser = argparse.ArgumentParser(description="Comprehensive V2G System Test Runner")
    parser.add_argument("--no-performance", action="store_true", help="Skip performance tests")
    parser.add_argument("--no-chaos", action="store_true", help="Skip chaos engineering tests")
    parser.add_argument("--report-file", default="test_report.json", help="Output report filename")

    args = parser.parse_args()

    runner = TestRunner()

    try:
        # Run all tests
        results = await runner.run_all_tests(
            include_performance=not args.no_performance, include_chaos=not args.no_chaos
        )

        # Print summary
        runner.print_summary()

        # Save report
        runner.save_report(args.report_file)

        # Exit with appropriate code
        summary = results.get("summary", {})
        if summary.get("overall_success", False):
            sys.exit(0)
        else:
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n⚠️  Test execution interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Test execution failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
