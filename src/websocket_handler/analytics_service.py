"""Analytics and reporting queries for TimescaleDB."""

import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

from .config import TimescaleConfig
from .timescale_client import TimescaleClient
from .monitoring import get_logger


class AnalyticsService:
    """Service for analytics and reporting queries."""
    
    def __init__(self, config: TimescaleConfig):
        """Initialize analytics service."""
        self.config = config
        self.logger = get_logger(__name__)
        self.timescale_client: Optional[TimescaleClient] = None
    
    async def initialize(self) -> None:
        """Initialize the analytics service."""
        self.timescale_client = TimescaleClient(self.config)
        await self.timescale_client.connect()
        self.logger.info("Analytics service initialized")
    
    async def close(self) -> None:
        """Close the analytics service."""
        if self.timescale_client:
            await self.timescale_client.disconnect()
        self.logger.info("Analytics service closed")
    
    # Energy Analytics
    async def get_energy_usage_analytics(self, fleet_operator_id: str, 
                                       start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Get comprehensive energy usage analytics."""
        try:
            # Get basic energy summary
            energy_summary = await self.timescale_client.get_energy_usage_summary(
                fleet_operator_id, start_time, end_time
            )
            
            # Get hourly aggregates
            hourly_data = await self._get_hourly_energy_data(fleet_operator_id, start_time, end_time)
            
            # Get daily fleet metrics
            daily_metrics = await self.timescale_client.get_daily_fleet_metrics(
                fleet_operator_id, start_time, end_time
            )
            
            # Calculate additional metrics
            analytics = {
                'summary': energy_summary,
                'hourly_data': hourly_data,
                'daily_metrics': daily_metrics.to_dict('records') if not daily_metrics.empty else [],
                'peak_power': await self._calculate_peak_power(fleet_operator_id, start_time, end_time),
                'energy_efficiency': await self._calculate_energy_efficiency(fleet_operator_id, start_time, end_time),
                'v2g_performance': await self._calculate_v2g_performance(fleet_operator_id, start_time, end_time)
            }
            
            return analytics
            
        except Exception as e:
            self.logger.error(f"Error getting energy usage analytics: {e}")
            raise
    
    async def _get_hourly_energy_data(self, fleet_operator_id: str, 
                                    start_time: datetime, end_time: datetime) -> List[Dict[str, Any]]:
        """Get hourly energy data aggregated by station."""
        try:
            # Get sessions for the fleet
            sessions = await self.timescale_client.get_charging_sessions(
                fleet_operator_id, start_time, end_time, limit=10000
            )
            
            if not sessions:
                return []
            
            # Group by station and hour
            hourly_data = {}
            
            for session in sessions:
                station_id = session['station_id']
                session_start = session['start_time']
                session_end = session['end_time']
                
                if not session_end:
                    continue
                
                # Calculate hourly breakdown
                current_hour = session_start.replace(minute=0, second=0, microsecond=0)
                end_hour = session_end.replace(minute=0, second=0, microsecond=0)
                
                while current_hour <= end_hour:
                    hour_key = f"{station_id}_{current_hour.isoformat()}"
                    
                    if hour_key not in hourly_data:
                        hourly_data[hour_key] = {
                            'station_id': station_id,
                            'hour': current_hour.isoformat(),
                            'energy_charged': 0,
                            'energy_discharged': 0,
                            'sessions': 0
                        }
                    
                    # Calculate energy for this hour
                    hour_start = max(current_hour, session_start)
                    hour_end = min(current_hour + timedelta(hours=1), session_end)
                    
                    if hour_start < hour_end:
                        duration_hours = (hour_end - hour_start).total_seconds() / 3600
                        total_duration = (session_end - session_start).total_seconds() / 3600
                        
                        if total_duration > 0:
                            energy_charged = (session['energy_delivered_kwh'] or 0) * (duration_hours / total_duration)
                            energy_discharged = (session['energy_received_kwh'] or 0) * (duration_hours / total_duration)
                            
                            hourly_data[hour_key]['energy_charged'] += energy_charged
                            hourly_data[hour_key]['energy_discharged'] += energy_discharged
                            hourly_data[hour_key]['sessions'] += 1
                    
                    current_hour += timedelta(hours=1)
            
            return list(hourly_data.values())
            
        except Exception as e:
            self.logger.error(f"Error getting hourly energy data: {e}")
            raise
    
    async def _calculate_peak_power(self, fleet_operator_id: str, 
                                  start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate peak power usage."""
        try:
            query = """
                SELECT 
                    station_id,
                    MAX(power_kw) as peak_power_kw,
                    AVG(power_kw) as avg_power_kw,
                    time_bucket('1 hour', time) as hour
                FROM telemetry_data td
                JOIN charging_sessions cs ON td.session_id = cs.session_id
                WHERE cs.fleet_operator_id = $1 
                    AND td.time >= $2 
                    AND td.time <= $3
                GROUP BY station_id, hour
                ORDER BY peak_power_kw DESC
                LIMIT 10
            """
            
            results = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if results:
                peak_power = max(results, key=lambda x: x['peak_power_kw'])
                return {
                    'peak_power_kw': peak_power['peak_power_kw'],
                    'peak_station_id': peak_power['station_id'],
                    'peak_hour': peak_power['hour'],
                    'top_stations': results[:5]
                }
            
            return {'peak_power_kw': 0, 'peak_station_id': None, 'peak_hour': None, 'top_stations': []}
            
        except Exception as e:
            self.logger.error(f"Error calculating peak power: {e}")
            raise
    
    async def _calculate_energy_efficiency(self, fleet_operator_id: str, 
                                         start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate energy efficiency metrics."""
        try:
            query = """
                SELECT 
                    AVG(power_factor) as avg_power_factor,
                    AVG(CASE WHEN power_kw > 0 THEN power_kw ELSE NULL END) as avg_charge_power,
                    AVG(CASE WHEN power_kw < 0 THEN ABS(power_kw) ELSE NULL END) as avg_discharge_power,
                    COUNT(CASE WHEN power_kw > 0 THEN 1 END) as charge_samples,
                    COUNT(CASE WHEN power_kw < 0 THEN 1 END) as discharge_samples
                FROM telemetry_data td
                JOIN charging_sessions cs ON td.session_id = cs.session_id
                WHERE cs.fleet_operator_id = $1 
                    AND td.time >= $2 
                    AND td.time <= $3
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                efficiency_data = result[0]
                return {
                    'avg_power_factor': float(efficiency_data['avg_power_factor'] or 0),
                    'avg_charge_power_kw': float(efficiency_data['avg_charge_power'] or 0),
                    'avg_discharge_power_kw': float(efficiency_data['avg_discharge_power'] or 0),
                    'charge_samples': efficiency_data['charge_samples'],
                    'discharge_samples': efficiency_data['discharge_samples']
                }
            
            return {
                'avg_power_factor': 0,
                'avg_charge_power_kw': 0,
                'avg_discharge_power_kw': 0,
                'charge_samples': 0,
                'discharge_samples': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating energy efficiency: {e}")
            raise
    
    async def _calculate_v2g_performance(self, fleet_operator_id: str, 
                                       start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate V2G performance metrics."""
        try:
            query = """
                SELECT 
                    SUM(energy_received_kwh) as total_v2g_energy,
                    COUNT(CASE WHEN energy_received_kwh > 0 THEN 1 END) as v2g_sessions,
                    AVG(CASE WHEN energy_received_kwh > 0 THEN energy_received_kwh END) as avg_v2g_energy_per_session,
                    MAX(energy_received_kwh) as max_v2g_energy_session
                FROM charging_sessions
                WHERE fleet_operator_id = $1 
                    AND start_time >= $2 
                    AND start_time <= $3
                    AND end_time IS NOT NULL
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                v2g_data = result[0]
                return {
                    'total_v2g_energy_kwh': float(v2g_data['total_v2g_energy'] or 0),
                    'v2g_sessions_count': v2g_data['v2g_sessions'],
                    'avg_v2g_energy_per_session_kwh': float(v2g_data['avg_v2g_energy_per_session'] or 0),
                    'max_v2g_energy_session_kwh': float(v2g_data['max_v2g_energy_session'] or 0)
                }
            
            return {
                'total_v2g_energy_kwh': 0,
                'v2g_sessions_count': 0,
                'avg_v2g_energy_per_session_kwh': 0,
                'max_v2g_energy_session_kwh': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating V2G performance: {e}")
            raise
    
    # Cost Analytics
    async def get_cost_analytics(self, fleet_operator_id: str, 
                               start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Get cost analytics including electricity prices and savings."""
        try:
            # Get electricity prices for the period
            price_data = await self._get_electricity_prices_for_period(start_time, end_time)
            
            # Calculate cost savings from optimization
            optimization_savings = await self._calculate_optimization_savings(
                fleet_operator_id, start_time, end_time
            )
            
            # Calculate demand charge analysis
            demand_analysis = await self._calculate_demand_charge_analysis(
                fleet_operator_id, start_time, end_time
            )
            
            return {
                'electricity_prices': price_data,
                'optimization_savings': optimization_savings,
                'demand_charge_analysis': demand_analysis,
                'total_cost_savings': optimization_savings.get('total_savings', 0) + demand_analysis.get('demand_savings', 0)
            }
            
        except Exception as e:
            self.logger.error(f"Error getting cost analytics: {e}")
            raise
    
    async def _get_electricity_prices_for_period(self, start_time: datetime, 
                                               end_time: datetime) -> List[Dict[str, Any]]:
        """Get electricity prices for the period."""
        try:
            # This would typically query electricity prices from a specific node
            # For now, return mock data structure
            return [
                {
                    'time': start_time.isoformat(),
                    'node_id': 'default_node',
                    'market_type': 'RTM',
                    'lmp_price_mwh': 50.0,
                    'energy_component_mwh': 45.0,
                    'congestion_component_mwh': 3.0,
                    'loss_component_mwh': 2.0
                }
            ]
            
        except Exception as e:
            self.logger.error(f"Error getting electricity prices: {e}")
            raise
    
    async def _calculate_optimization_savings(self, fleet_operator_id: str, 
                                            start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate savings from optimization decisions."""
        try:
            query = """
                SELECT 
                    COUNT(*) as total_decisions,
                    AVG(objective_value) as avg_objective_value,
                    SUM(objective_value) as total_objective_value,
                    AVG(computation_time_ms) as avg_computation_time_ms,
                    COUNT(CASE WHEN constraints_satisfied THEN 1 END) as successful_decisions
                FROM optimization_decisions
                WHERE fleet_operator_id = $1 
                    AND time >= $2 
                    AND time <= $3
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                opt_data = result[0]
                success_rate = (opt_data['successful_decisions'] / opt_data['total_decisions']) * 100 if opt_data['total_decisions'] > 0 else 0
                
                return {
                    'total_decisions': opt_data['total_decisions'],
                    'success_rate_percent': success_rate,
                    'avg_objective_value': float(opt_data['avg_objective_value'] or 0),
                    'total_savings': float(opt_data['total_objective_value'] or 0),
                    'avg_computation_time_ms': float(opt_data['avg_computation_time_ms'] or 0)
                }
            
            return {
                'total_decisions': 0,
                'success_rate_percent': 0,
                'avg_objective_value': 0,
                'total_savings': 0,
                'avg_computation_time_ms': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating optimization savings: {e}")
            raise
    
    async def _calculate_demand_charge_analysis(self, fleet_operator_id: str, 
                                              start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate demand charge analysis."""
        try:
            query = """
                SELECT 
                    time_bucket('15 minutes', td.time) as interval_15min,
                    MAX(td.power_kw) as peak_power_15min
                FROM telemetry_data td
                JOIN charging_sessions cs ON td.session_id = cs.session_id
                WHERE cs.fleet_operator_id = $1 
                    AND td.time >= $2 
                    AND td.time <= $3
                GROUP BY interval_15min
                ORDER BY peak_power_15min DESC
                LIMIT 10
            """
            
            results = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if results:
                peak_demand = max(results, key=lambda x: x['peak_power_15min'])
                avg_demand = sum(r['peak_power_15min'] for r in results) / len(results)
                
                return {
                    'peak_demand_kw': float(peak_demand['peak_power_15min']),
                    'peak_demand_time': peak_demand['interval_15min'].isoformat(),
                    'avg_demand_kw': float(avg_demand),
                    'demand_savings': float(peak_demand['peak_power_15min'] - avg_demand) * 0.15  # $0.15/kW
                }
            
            return {
                'peak_demand_kw': 0,
                'peak_demand_time': None,
                'avg_demand_kw': 0,
                'demand_savings': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating demand charge analysis: {e}")
            raise
    
    # Performance Analytics
    async def get_performance_analytics(self, fleet_operator_id: str, 
                                      start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Get performance analytics including uptime, efficiency, and reliability."""
        try:
            # Calculate uptime metrics
            uptime_metrics = await self._calculate_uptime_metrics(fleet_operator_id, start_time, end_time)
            
            # Calculate efficiency metrics
            efficiency_metrics = await self._calculate_efficiency_metrics(fleet_operator_id, start_time, end_time)
            
            # Calculate reliability metrics
            reliability_metrics = await self._calculate_reliability_metrics(fleet_operator_id, start_time, end_time)
            
            return {
                'uptime': uptime_metrics,
                'efficiency': efficiency_metrics,
                'reliability': reliability_metrics,
                'overall_score': self._calculate_overall_score(uptime_metrics, efficiency_metrics, reliability_metrics)
            }
            
        except Exception as e:
            self.logger.error(f"Error getting performance analytics: {e}")
            raise
    
    async def _calculate_uptime_metrics(self, fleet_operator_id: str, 
                                      start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate uptime metrics."""
        try:
            query = """
                SELECT 
                    COUNT(DISTINCT station_id) as total_stations,
                    COUNT(DISTINCT CASE WHEN end_time IS NOT NULL THEN station_id END) as operational_stations,
                    AVG(EXTRACT(EPOCH FROM (end_time - start_time))/3600) as avg_session_duration_hours
                FROM charging_sessions
                WHERE fleet_operator_id = $1 
                    AND start_time >= $2 
                    AND start_time <= $3
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                uptime_data = result[0]
                uptime_percentage = (uptime_data['operational_stations'] / uptime_data['total_stations']) * 100 if uptime_data['total_stations'] > 0 else 0
                
                return {
                    'uptime_percentage': uptime_percentage,
                    'total_stations': uptime_data['total_stations'],
                    'operational_stations': uptime_data['operational_stations'],
                    'avg_session_duration_hours': float(uptime_data['avg_session_duration_hours'] or 0)
                }
            
            return {
                'uptime_percentage': 0,
                'total_stations': 0,
                'operational_stations': 0,
                'avg_session_duration_hours': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating uptime metrics: {e}")
            raise
    
    async def _calculate_efficiency_metrics(self, fleet_operator_id: str, 
                                         start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate efficiency metrics."""
        try:
            query = """
                SELECT 
                    AVG(power_factor) as avg_power_factor,
                    AVG(CASE WHEN power_kw > 0 THEN power_kw ELSE NULL END) as avg_charge_efficiency,
                    AVG(CASE WHEN power_kw < 0 THEN ABS(power_kw) ELSE NULL END) as avg_discharge_efficiency,
                    COUNT(CASE WHEN power_factor > 0.9 THEN 1 END) as high_efficiency_samples,
                    COUNT(*) as total_samples
                FROM telemetry_data td
                JOIN charging_sessions cs ON td.session_id = cs.session_id
                WHERE cs.fleet_operator_id = $1 
                    AND td.time >= $2 
                    AND td.time <= $3
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                eff_data = result[0]
                efficiency_percentage = (eff_data['high_efficiency_samples'] / eff_data['total_samples']) * 100 if eff_data['total_samples'] > 0 else 0
                
                return {
                    'avg_power_factor': float(eff_data['avg_power_factor'] or 0),
                    'avg_charge_efficiency_kw': float(eff_data['avg_charge_efficiency'] or 0),
                    'avg_discharge_efficiency_kw': float(eff_data['avg_discharge_efficiency'] or 0),
                    'efficiency_percentage': efficiency_percentage,
                    'high_efficiency_samples': eff_data['high_efficiency_samples'],
                    'total_samples': eff_data['total_samples']
                }
            
            return {
                'avg_power_factor': 0,
                'avg_charge_efficiency_kw': 0,
                'avg_discharge_efficiency_kw': 0,
                'efficiency_percentage': 0,
                'high_efficiency_samples': 0,
                'total_samples': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating efficiency metrics: {e}")
            raise
    
    async def _calculate_reliability_metrics(self, fleet_operator_id: str, 
                                           start_time: datetime, end_time: datetime) -> Dict[str, Any]:
        """Calculate reliability metrics."""
        try:
            query = """
                SELECT 
                    COUNT(*) as total_sessions,
                    COUNT(CASE WHEN end_time IS NOT NULL THEN 1 END) as completed_sessions,
                    COUNT(CASE WHEN end_time IS NULL AND start_time < NOW() - INTERVAL '1 hour' THEN 1 END) as failed_sessions,
                    AVG(CASE WHEN end_time IS NOT NULL THEN EXTRACT(EPOCH FROM (end_time - start_time))/3600 END) as avg_completion_time_hours
                FROM charging_sessions
                WHERE fleet_operator_id = $1 
                    AND start_time >= $2 
                    AND start_time <= $3
            """
            
            result = await self.timescale_client.execute_query(query, fleet_operator_id, start_time, end_time)
            
            if result:
                rel_data = result[0]
                success_rate = (rel_data['completed_sessions'] / rel_data['total_sessions']) * 100 if rel_data['total_sessions'] > 0 else 0
                failure_rate = (rel_data['failed_sessions'] / rel_data['total_sessions']) * 100 if rel_data['total_sessions'] > 0 else 0
                
                return {
                    'success_rate_percent': success_rate,
                    'failure_rate_percent': failure_rate,
                    'total_sessions': rel_data['total_sessions'],
                    'completed_sessions': rel_data['completed_sessions'],
                    'failed_sessions': rel_data['failed_sessions'],
                    'avg_completion_time_hours': float(rel_data['avg_completion_time_hours'] or 0)
                }
            
            return {
                'success_rate_percent': 0,
                'failure_rate_percent': 0,
                'total_sessions': 0,
                'completed_sessions': 0,
                'failed_sessions': 0,
                'avg_completion_time_hours': 0
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating reliability metrics: {e}")
            raise
    
    def _calculate_overall_score(self, uptime: Dict[str, Any], 
                               efficiency: Dict[str, Any], reliability: Dict[str, Any]) -> float:
        """Calculate overall performance score."""
        try:
            uptime_score = uptime.get('uptime_percentage', 0) / 100
            efficiency_score = efficiency.get('efficiency_percentage', 0) / 100
            reliability_score = reliability.get('success_rate_percent', 0) / 100
            
            # Weighted average
            overall_score = (uptime_score * 0.3 + efficiency_score * 0.3 + reliability_score * 0.4) * 100
            
            return round(overall_score, 2)
            
        except Exception as e:
            self.logger.error(f"Error calculating overall score: {e}")
            return 0.0
    
    # Custom Analytics
    async def get_custom_analytics(self, query: str, params: List[Any] = None) -> List[Dict[str, Any]]:
        """Execute custom analytics query."""
        try:
            if params is None:
                params = []
            
            results = await self.timescale_client.execute_query(query, *params)
            return results
            
        except Exception as e:
            self.logger.error(f"Error executing custom analytics query: {e}")
            raise
    
    async def export_analytics_data(self, fleet_operator_id: str, 
                                  start_time: datetime, end_time: datetime,
                                  format: str = 'json') -> Dict[str, Any]:
        """Export analytics data in various formats."""
        try:
            # Get all analytics data
            energy_analytics = await self.get_energy_usage_analytics(fleet_operator_id, start_time, end_time)
            cost_analytics = await self.get_cost_analytics(fleet_operator_id, start_time, end_time)
            performance_analytics = await self.get_performance_analytics(fleet_operator_id, start_time, end_time)
            
            export_data = {
                'fleet_operator_id': fleet_operator_id,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'export_timestamp': datetime.now(timezone.utc).isoformat(),
                'energy_analytics': energy_analytics,
                'cost_analytics': cost_analytics,
                'performance_analytics': performance_analytics
            }
            
            if format == 'csv':
                # Convert to CSV format (simplified)
                return {
                    'format': 'csv',
                    'data': self._convert_to_csv_format(export_data)
                }
            else:
                return {
                    'format': 'json',
                    'data': export_data
                }
                
        except Exception as e:
            self.logger.error(f"Error exporting analytics data: {e}")
            raise
    
    def _convert_to_csv_format(self, data: Dict[str, Any]) -> str:
        """Convert analytics data to CSV format."""
        # This is a simplified CSV conversion
        # In a real implementation, you'd use pandas or similar
        csv_lines = []
        csv_lines.append("Metric,Value")
        
        # Add summary metrics
        if 'energy_analytics' in data and 'summary' in data['energy_analytics']:
            summary = data['energy_analytics']['summary']
            csv_lines.append(f"Total Sessions,{summary.get('total_sessions', 0)}")
            csv_lines.append(f"Total Energy Charged,{summary.get('total_energy_charged', 0)}")
            csv_lines.append(f"Total Energy Discharged,{summary.get('total_energy_discharged', 0)}")
        
        return '\n'.join(csv_lines)
