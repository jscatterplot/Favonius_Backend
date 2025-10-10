"""Database schema for OCPP 2.0.1 compliance features."""

from datetime import datetime, timezone
from typing import Dict, Any, List


def get_ocpp_schema_sql() -> str:
    """Get SQL schema for OCPP 2.0.1 compliance tables."""
    return """
-- ===== OCPP 2.0.1 COMPLIANCE SCHEMA =====

-- Device Components Table
CREATE TABLE IF NOT EXISTS device_components (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    instance VARCHAR(255) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, component_name, instance)
);

-- Device Variables Table
CREATE TABLE IF NOT EXISTS device_variables (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    component_instance VARCHAR(255) NOT NULL DEFAULT '',
    variable_name VARCHAR(255) NOT NULL,
    variable_instance VARCHAR(255) NOT NULL DEFAULT '',
    attribute_type VARCHAR(50) NOT NULL DEFAULT 'Actual',
    value TEXT,
    actual_value TEXT,
    target_value TEXT,
    default_value TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, component_name, component_instance, variable_name, variable_instance, attribute_type)
);

-- Device Reports Table
CREATE TABLE IF NOT EXISTS device_reports (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    tbc BOOLEAN NOT NULL DEFAULT FALSE,
    seq_no INTEGER NOT NULL,
    report_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Charging Profiles Table
CREATE TABLE IF NOT EXISTS charging_profiles (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    stack_level INTEGER NOT NULL,
    purpose VARCHAR(100) NOT NULL,
    kind VARCHAR(50) NOT NULL,
    schedule JSONB NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    transaction_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, evse_id, profile_id)
);

-- Reported Charging Profiles Table
CREATE TABLE IF NOT EXISTS reported_charging_profiles (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    request_id INTEGER NOT NULL,
    profile JSONB NOT NULL,
    reported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Transactions Table
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    transaction_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    connector_id INTEGER NOT NULL,
    id_token VARCHAR(255) NOT NULL,
    id_token_type VARCHAR(50) NOT NULL,
    charging_state VARCHAR(50),
    remote_start_id INTEGER,
    time_spent_charging INTEGER,
    stopped_reason VARCHAR(100),
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, transaction_id)
);

-- Transaction Events Table
CREATE TABLE IF NOT EXISTS transaction_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    transaction_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    charging_state VARCHAR(50),
    time_spent_charging INTEGER,
    stopped_reason VARCHAR(100),
    remote_start_id INTEGER,
    evse_id INTEGER,
    connector_id INTEGER,
    meter_value JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Authorization Cache Table
CREATE TABLE IF NOT EXISTS authorization_cache (
    id SERIAL PRIMARY KEY,
    id_token VARCHAR(255) NOT NULL,
    token_type VARCHAR(50) NOT NULL,
    cache_timeout INTEGER,
    charging_priority INTEGER,
    language1 VARCHAR(10),
    language2 VARCHAR(10),
    group_id_token JSONB,
    personal_message JSONB,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(id_token, token_type)
);

-- Tariffs Table
CREATE TABLE IF NOT EXISTS tariffs (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    tariff_id VARCHAR(255) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    tariff_element JSONB NOT NULL,
    start_date_time TIMESTAMPTZ,
    end_date_time TIMESTAMPTZ,
    min_price DECIMAL(10,4),
    max_price DECIMAL(10,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, tariff_id)
);

-- Transaction Costs Table
CREATE TABLE IF NOT EXISTS transaction_costs (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    transaction_id VARCHAR(255) NOT NULL,
    total_cost DECIMAL(10,4) NOT NULL,
    currency VARCHAR(3) NOT NULL,
    cost_breakdown JSONB NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, transaction_id)
);

-- Reset Requests Table
CREATE TABLE IF NOT EXISTS reset_requests (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    reset_type VARCHAR(50) NOT NULL,
    evse_id INTEGER,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Availability Changes Table
CREATE TABLE IF NOT EXISTS availability_changes (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER,
    operational_status VARCHAR(50) NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trigger Messages Table
CREATE TABLE IF NOT EXISTS trigger_messages (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER,
    requested_message VARCHAR(100) NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unlock Connector Requests Table
CREATE TABLE IF NOT EXISTS unlock_connector_requests (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    connector_id INTEGER NOT NULL,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Certificates Table
CREATE TABLE IF NOT EXISTS certificates (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    certificate_type VARCHAR(100) NOT NULL,
    certificate_data TEXT NOT NULL,
    certificate_chain JSONB,
    issuer_name TEXT,
    subject_name TEXT,
    serial_number VARCHAR(255),
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    status VARCHAR(50) NOT NULL DEFAULT 'Valid',
    installation_date TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, certificate_type, serial_number)
);

-- Authentication Tokens Table
CREATE TABLE IF NOT EXISTS auth_tokens (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    token TEXT NOT NULL,
    token_type VARCHAR(50) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used TIMESTAMPTZ,
    usage_count INTEGER NOT NULL DEFAULT 0,
    revoked_at TIMESTAMPTZ,
    UNIQUE(station_id, token_type)
);

-- Security Events Table
CREATE TABLE IF NOT EXISTS security_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    tech_info TEXT,
    additional_info JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- API Keys Table
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    api_key VARCHAR(255) NOT NULL,
    description TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    last_used TIMESTAMPTZ,
    usage_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(api_key)
);

-- Station Credentials Table
CREATE TABLE IF NOT EXISTS station_credentials (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    username VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used TIMESTAMPTZ,
    UNIQUE(station_id, username)
);

-- Contracts Table
CREATE TABLE IF NOT EXISTS contracts (
    id SERIAL PRIMARY KEY,
    contract_id VARCHAR(255) NOT NULL,
    ev_contract_id VARCHAR(255) NOT NULL,
    certificate_chain JSONB,
    contract_certificate TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'Valid',
    energy_contract_id VARCHAR(255),
    tariff_id VARCHAR(255),
    max_power DECIMAL(10,2),
    max_energy DECIMAL(10,2),
    revoked_at TIMESTAMPTZ,
    revoked_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(contract_id)
);

-- Energy Contracts Table
CREATE TABLE IF NOT EXISTS energy_contracts (
    id SERIAL PRIMARY KEY,
    energy_contract_id VARCHAR(255) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    max_power DECIMAL(10,2),
    max_energy DECIMAL(10,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(energy_contract_id)
);

-- EVSE Capabilities Table
CREATE TABLE IF NOT EXISTS evse_capabilities (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    max_power DECIMAL(10,2) NOT NULL,
    connector_types JSONB NOT NULL,
    supported_protocols JSONB NOT NULL,
    v2g_capable BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, evse_id)
);

-- Token Balances Table
CREATE TABLE IF NOT EXISTS token_balances (
    id SERIAL PRIMARY KEY,
    id_token VARCHAR(255) NOT NULL,
    balance DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    currency VARCHAR(3) NOT NULL DEFAULT 'USD',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(id_token)
);

-- Grid Constraints Table
CREATE TABLE IF NOT EXISTS grid_constraints (
    id SERIAL PRIMARY KEY,
    constraint_id VARCHAR(255) NOT NULL,
    constraint_type VARCHAR(50) NOT NULL,
    location VARCHAR(255) NOT NULL,
    max_power DECIMAL(10,2) NOT NULL,
    min_power DECIMAL(10,2) NOT NULL DEFAULT 0.0,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    priority INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(constraint_id)
);

-- Demand Response Events Table
CREATE TABLE IF NOT EXISTS demand_response_events (
    id SERIAL PRIMARY KEY,
    event_id VARCHAR(255) NOT NULL,
    signal VARCHAR(50) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    target_reduction DECIMAL(10,2),
    target_increase DECIMAL(10,2),
    affected_stations JSONB NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(event_id)
);

-- EVSE Priorities Table
CREATE TABLE IF NOT EXISTS evse_priorities (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    priority VARCHAR(20) NOT NULL DEFAULT 'medium',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, evse_id)
);

-- EVSE Configurations Table
CREATE TABLE IF NOT EXISTS evse_configurations (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    price_sensitivity DECIMAL(5,2) NOT NULL DEFAULT 1.0,
    current_power_limit DECIMAL(10,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, evse_id)
);

-- Electricity Prices Table
CREATE TABLE IF NOT EXISTS electricity_prices (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    current_price DECIMAL(10,4) NOT NULL,
    forecast_prices JSONB,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Signed Meter Values Table
CREATE TABLE IF NOT EXISTS signed_meter_values (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    sampled_value JSONB NOT NULL,
    reading_context VARCHAR(50) NOT NULL,
    format VARCHAR(20) NOT NULL,
    signature VARCHAR(255) NOT NULL,
    signature_method VARCHAR(50) NOT NULL,
    encoding_method VARCHAR(50) NOT NULL,
    public_key TEXT,
    signed_data TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Energy Accounting Table
CREATE TABLE IF NOT EXISTS energy_accounting (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    connector_id INTEGER NOT NULL,
    transaction_id VARCHAR(255),
    energy_import_kwh DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    energy_export_kwh DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    reactive_energy_import_kvarh DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    reactive_energy_export_kvarh DECIMAL(10,4) NOT NULL DEFAULT 0.0,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    billing_accuracy DECIMAL(5,3) NOT NULL DEFAULT 0.1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, evse_id, connector_id, transaction_id)
);

-- Meter Calibrations Table
CREATE TABLE IF NOT EXISTS meter_calibrations (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    calibration_data JSONB NOT NULL,
    calibrated_at TIMESTAMPTZ NOT NULL,
    accuracy DECIMAL(5,3) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id)
);

-- Power Quality Events Table
CREATE TABLE IF NOT EXISTS power_quality_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    events JSONB NOT NULL,
    voltage_l1 DECIMAL(8,2),
    voltage_l2 DECIMAL(8,2),
    voltage_l3 DECIMAL(8,2),
    current_l1 DECIMAL(8,2),
    current_l2 DECIMAL(8,2),
    current_l3 DECIMAL(8,2),
    frequency DECIMAL(6,3),
    power_factor DECIMAL(4,3),
    thd_voltage DECIMAL(5,2),
    thd_current DECIMAL(5,2),
    phase_imbalance DECIMAL(5,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ===== INDEXES =====

-- Device Components Indexes
CREATE INDEX IF NOT EXISTS idx_device_components_station_id ON device_components(station_id);
CREATE INDEX IF NOT EXISTS idx_device_components_name ON device_components(component_name);

-- Device Variables Indexes
CREATE INDEX IF NOT EXISTS idx_device_variables_station_id ON device_variables(station_id);
CREATE INDEX IF NOT EXISTS idx_device_variables_component ON device_variables(component_name, variable_name);

-- Device Reports Indexes
CREATE INDEX IF NOT EXISTS idx_device_reports_station_id ON device_reports(station_id);
CREATE INDEX IF NOT EXISTS idx_device_reports_request_id ON device_reports(request_id);

-- Charging Profiles Indexes
CREATE INDEX IF NOT EXISTS idx_charging_profiles_station_evse ON charging_profiles(station_id, evse_id);
CREATE INDEX IF NOT EXISTS idx_charging_profiles_purpose ON charging_profiles(purpose);
CREATE INDEX IF NOT EXISTS idx_charging_profiles_valid_time ON charging_profiles(valid_from, valid_to);

-- Transactions Indexes
CREATE INDEX IF NOT EXISTS idx_transactions_station_id ON transactions(station_id);
CREATE INDEX IF NOT EXISTS idx_transactions_transaction_id ON transactions(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transactions_started_at ON transactions(started_at);

-- Transaction Events Indexes
CREATE INDEX IF NOT EXISTS idx_transaction_events_station_transaction ON transaction_events(station_id, transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_events_timestamp ON transaction_events(timestamp);

-- Authorization Cache Indexes
CREATE INDEX IF NOT EXISTS idx_authorization_cache_token ON authorization_cache(id_token, token_type);
CREATE INDEX IF NOT EXISTS idx_authorization_cache_expires ON authorization_cache(expires_at);

-- Tariffs Indexes
CREATE INDEX IF NOT EXISTS idx_tariffs_station_id ON tariffs(station_id);
CREATE INDEX IF NOT EXISTS idx_tariffs_valid_time ON tariffs(start_date_time, end_date_time);

-- Transaction Costs Indexes
CREATE INDEX IF NOT EXISTS idx_transaction_costs_station_transaction ON transaction_costs(station_id, transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_costs_calculated_at ON transaction_costs(calculated_at);

-- Reset Requests Indexes
CREATE INDEX IF NOT EXISTS idx_reset_requests_station_id ON reset_requests(station_id);
CREATE INDEX IF NOT EXISTS idx_reset_requests_requested_at ON reset_requests(requested_at);

-- Availability Changes Indexes
CREATE INDEX IF NOT EXISTS idx_availability_changes_station_id ON availability_changes(station_id);
CREATE INDEX IF NOT EXISTS idx_availability_changes_changed_at ON availability_changes(changed_at);

-- Trigger Messages Indexes
CREATE INDEX IF NOT EXISTS idx_trigger_messages_station_id ON trigger_messages(station_id);
CREATE INDEX IF NOT EXISTS idx_trigger_messages_triggered_at ON trigger_messages(triggered_at);

-- Unlock Connector Requests Indexes
CREATE INDEX IF NOT EXISTS idx_unlock_connector_station_id ON unlock_connector_requests(station_id);
CREATE INDEX IF NOT EXISTS idx_unlock_connector_unlocked_at ON unlock_connector_requests(unlocked_at);

-- Certificates Indexes
CREATE INDEX IF NOT EXISTS idx_certificates_station_id ON certificates(station_id);
CREATE INDEX IF NOT EXISTS idx_certificates_type ON certificates(certificate_type);
CREATE INDEX IF NOT EXISTS idx_certificates_serial ON certificates(serial_number);
CREATE INDEX IF NOT EXISTS idx_certificates_valid_period ON certificates(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_certificates_status ON certificates(status);
CREATE INDEX IF NOT EXISTS idx_certificates_installation_date ON certificates(installation_date);

-- Authentication Tokens Indexes
CREATE INDEX IF NOT EXISTS idx_auth_tokens_station_id ON auth_tokens(station_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_token ON auth_tokens(token);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires_at ON auth_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_revoked_at ON auth_tokens(revoked_at);

-- Security Events Indexes
CREATE INDEX IF NOT EXISTS idx_security_events_station_id ON security_events(station_id);
CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type);
CREATE INDEX IF NOT EXISTS idx_security_events_timestamp ON security_events(timestamp);

-- API Keys Indexes
CREATE INDEX IF NOT EXISTS idx_api_keys_station_id ON api_keys(station_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys(api_key);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(active);
CREATE INDEX IF NOT EXISTS idx_api_keys_expires_at ON api_keys(expires_at);

-- Station Credentials Indexes
CREATE INDEX IF NOT EXISTS idx_station_credentials_station_id ON station_credentials(station_id);
CREATE INDEX IF NOT EXISTS idx_station_credentials_username ON station_credentials(username);
CREATE INDEX IF NOT EXISTS idx_station_credentials_active ON station_credentials(active);

-- Contracts Indexes
CREATE INDEX IF NOT EXISTS idx_contracts_contract_id ON contracts(contract_id);
CREATE INDEX IF NOT EXISTS idx_contracts_ev_contract_id ON contracts(ev_contract_id);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);
CREATE INDEX IF NOT EXISTS idx_contracts_valid_period ON contracts(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_contracts_energy_contract_id ON contracts(energy_contract_id);
CREATE INDEX IF NOT EXISTS idx_contracts_tariff_id ON contracts(tariff_id);

-- Energy Contracts Indexes
CREATE INDEX IF NOT EXISTS idx_energy_contracts_contract_id ON energy_contracts(energy_contract_id);
CREATE INDEX IF NOT EXISTS idx_energy_contracts_active ON energy_contracts(active);
CREATE INDEX IF NOT EXISTS idx_energy_contracts_valid_period ON energy_contracts(valid_from, valid_to);

-- EVSE Capabilities Indexes
CREATE INDEX IF NOT EXISTS idx_evse_capabilities_station_evse ON evse_capabilities(station_id, evse_id);
CREATE INDEX IF NOT EXISTS idx_evse_capabilities_v2g_capable ON evse_capabilities(v2g_capable);

-- Token Balances Indexes
CREATE INDEX IF NOT EXISTS idx_token_balances_id_token ON token_balances(id_token);
CREATE INDEX IF NOT EXISTS idx_token_balances_last_updated ON token_balances(last_updated);

-- Grid Constraints Indexes
CREATE INDEX IF NOT EXISTS idx_grid_constraints_location ON grid_constraints(location);
CREATE INDEX IF NOT EXISTS idx_grid_constraints_type ON grid_constraints(constraint_type);
CREATE INDEX IF NOT EXISTS idx_grid_constraints_active ON grid_constraints(active);
CREATE INDEX IF NOT EXISTS idx_grid_constraints_valid_period ON grid_constraints(valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_grid_constraints_priority ON grid_constraints(priority);

-- Demand Response Events Indexes
CREATE INDEX IF NOT EXISTS idx_demand_response_events_signal ON demand_response_events(signal);
CREATE INDEX IF NOT EXISTS idx_demand_response_events_time ON demand_response_events(start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_demand_response_events_priority ON demand_response_events(priority);

-- EVSE Priorities Indexes
CREATE INDEX IF NOT EXISTS idx_evse_priorities_station_evse ON evse_priorities(station_id, evse_id);
CREATE INDEX IF NOT EXISTS idx_evse_priorities_priority ON evse_priorities(priority);

-- EVSE Configurations Indexes
CREATE INDEX IF NOT EXISTS idx_evse_configurations_station_evse ON evse_configurations(station_id, evse_id);

-- Electricity Prices Indexes
CREATE INDEX IF NOT EXISTS idx_electricity_prices_station_id ON electricity_prices(station_id);
CREATE INDEX IF NOT EXISTS idx_electricity_prices_timestamp ON electricity_prices(timestamp);

-- Signed Meter Values Indexes
CREATE INDEX IF NOT EXISTS idx_signed_meter_values_station_evse ON signed_meter_values(station_id, evse_id);
CREATE INDEX IF NOT EXISTS idx_signed_meter_values_timestamp ON signed_meter_values(timestamp);
CREATE INDEX IF NOT EXISTS idx_signed_meter_values_reading_context ON signed_meter_values(reading_context);

-- Energy Accounting Indexes
CREATE INDEX IF NOT EXISTS idx_energy_accounting_station_evse ON energy_accounting(station_id, evse_id);
CREATE INDEX IF NOT EXISTS idx_energy_accounting_transaction ON energy_accounting(transaction_id);
CREATE INDEX IF NOT EXISTS idx_energy_accounting_start_time ON energy_accounting(start_time);

-- Meter Calibrations Indexes
CREATE INDEX IF NOT EXISTS idx_meter_calibrations_station_id ON meter_calibrations(station_id);
CREATE INDEX IF NOT EXISTS idx_meter_calibrations_calibrated_at ON meter_calibrations(calibrated_at);

-- Power Quality Events Indexes
CREATE INDEX IF NOT EXISTS idx_power_quality_events_station_id ON power_quality_events(station_id);
CREATE INDEX IF NOT EXISTS idx_power_quality_events_timestamp ON power_quality_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_power_quality_events_events ON power_quality_events USING GIN(events);

-- ===== TIMESCALE HYPERTABLES =====

-- Convert time-series tables to hypertables
SELECT create_hypertable('device_reports', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('transaction_events', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('transaction_costs', 'calculated_at', if_not_exists => TRUE);
SELECT create_hypertable('reset_requests', 'requested_at', if_not_exists => TRUE);
SELECT create_hypertable('availability_changes', 'changed_at', if_not_exists => TRUE);
SELECT create_hypertable('trigger_messages', 'triggered_at', if_not_exists => TRUE);
SELECT create_hypertable('unlock_connector_requests', 'unlocked_at', if_not_exists => TRUE);
SELECT create_hypertable('security_events', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('demand_response_events', 'start_time', if_not_exists => TRUE);
SELECT create_hypertable('electricity_prices', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('signed_meter_values', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('power_quality_events', 'timestamp', if_not_exists => TRUE);

-- ===== COMPRESSION POLICIES =====

-- Add compression policies for time-series data
SELECT add_compression_policy('device_reports', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('transaction_events', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('transaction_costs', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('reset_requests', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('availability_changes', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('trigger_messages', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('unlock_connector_requests', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('security_events', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('demand_response_events', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('electricity_prices', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('signed_meter_values', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('power_quality_events', INTERVAL '30 days', if_not_exists => TRUE);

-- ===== RETENTION POLICIES =====

-- Add retention policies for time-series data
SELECT add_retention_policy('device_reports', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('transaction_events', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('transaction_costs', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('reset_requests', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('availability_changes', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('trigger_messages', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('unlock_connector_requests', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('security_events', INTERVAL '5 years', if_not_exists => TRUE);
SELECT add_retention_policy('demand_response_events', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('electricity_prices', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('signed_meter_values', INTERVAL '7 years', if_not_exists => TRUE);
SELECT add_retention_policy('power_quality_events', INTERVAL '2 years', if_not_exists => TRUE);
"""


def get_ocpp_schema_migrations() -> List[Dict[str, Any]]:
    """Get migration data for OCPP schema."""
    return [
        {
            "version": "2024.01.001",
            "description": "Initial OCPP 2.0.1 compliance schema",
            "up": get_ocpp_schema_sql(),
            "down": """
                -- Drop tables in reverse order
                DROP TABLE IF EXISTS unlock_connector_requests CASCADE;
                DROP TABLE IF EXISTS trigger_messages CASCADE;
                DROP TABLE IF EXISTS availability_changes CASCADE;
                DROP TABLE IF EXISTS reset_requests CASCADE;
                DROP TABLE IF EXISTS transaction_costs CASCADE;
                DROP TABLE IF EXISTS tariffs CASCADE;
                DROP TABLE IF EXISTS authorization_cache CASCADE;
                DROP TABLE IF EXISTS transaction_events CASCADE;
                DROP TABLE IF EXISTS transactions CASCADE;
                DROP TABLE IF EXISTS reported_charging_profiles CASCADE;
                DROP TABLE IF EXISTS charging_profiles CASCADE;
                DROP TABLE IF EXISTS device_reports CASCADE;
                DROP TABLE IF EXISTS device_variables CASCADE;
                DROP TABLE IF EXISTS device_components CASCADE;
            """
        }
    ]


def get_standard_ocpp_variables() -> Dict[str, List[Dict[str, Any]]]:
    """Get standard OCPP variables by component."""
    return {
        "ChargingStation": [
            {"name": "Model", "type": "string", "mutability": "ReadOnly"},
            {"name": "VendorName", "type": "string", "mutability": "ReadOnly"},
            {"name": "SerialNumber", "type": "string", "mutability": "ReadOnly"},
            {"name": "FirmwareVersion", "type": "string", "mutability": "ReadOnly"},
            {"name": "Modem", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedFeatures", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedProtocols", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedChargingProfilePurposeTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedChargingProfileTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedMeasurands", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedCableTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedConnectorTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedDisplayMessageTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedIdTokenTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedMessageTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedOcppVersions", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedReservationTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedSecurityProfiles", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedUnitTypes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedV2GModes", "type": "string", "mutability": "ReadOnly"},
            {"name": "SupportedV2XChargingCtrlrTypes", "type": "string", "mutability": "ReadOnly"},
        ],
        "EVSE": [
            {"name": "AvailabilityState", "type": "enum", "mutability": "ReadWrite"},
            {"name": "AvailabilitySchedule", "type": "string", "mutability": "ReadWrite"},
            {"name": "Connector", "type": "string", "mutability": "ReadOnly"},
            {"name": "ConnectorType", "type": "enum", "mutability": "ReadOnly"},
            {"name": "ConnectorTypeId", "type": "integer", "mutability": "ReadOnly"},
            {"name": "MaxEnergy", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "MinEnergy", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "NominalVoltage", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "Power", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "PowerType", "type": "enum", "mutability": "ReadOnly"},
            {"name": "ReservationId", "type": "integer", "mutability": "ReadWrite"},
            {"name": "Status", "type": "enum", "mutability": "ReadOnly"},
            {"name": "TransactionId", "type": "integer", "mutability": "ReadWrite"},
        ],
        "Connector": [
            {"name": "AvailabilityState", "type": "enum", "mutability": "ReadWrite"},
            {"name": "AvailabilitySchedule", "type": "string", "mutability": "ReadWrite"},
            {"name": "Cable", "type": "string", "mutability": "ReadOnly"},
            {"name": "ConnectorType", "type": "enum", "mutability": "ReadOnly"},
            {"name": "ConnectorTypeId", "type": "integer", "mutability": "ReadOnly"},
            {"name": "MaxEnergy", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "MinEnergy", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "NominalVoltage", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "Power", "type": "decimal", "mutability": "ReadOnly"},
            {"name": "PowerType", "type": "enum", "mutability": "ReadOnly"},
            {"name": "ReservationId", "type": "integer", "mutability": "ReadWrite"},
            {"name": "Status", "type": "enum", "mutability": "ReadOnly"},
            {"name": "TransactionId", "type": "integer", "mutability": "ReadWrite"},
        ],
        "SmartCharging": [
            {"name": "ChargingProfileMaxStackLevel", "type": "integer", "mutability": "ReadOnly"},
            {"name": "ChargingScheduleAllowedChargingRateUnit", "type": "string", "mutability": "ReadOnly"},
            {"name": "ChargingScheduleMaxPeriods", "type": "integer", "mutability": "ReadOnly"},
            {"name": "ConnectorSwitch3to1PhaseSupported", "type": "boolean", "mutability": "ReadOnly"},
            {"name": "MaxChargingProfilesInstalled", "type": "integer", "mutability": "ReadOnly"},
            {"name": "MaxScheduledChargingProfiles", "type": "integer", "mutability": "ReadOnly"},
            {"name": "MaxScheduledChargingProfilesPerEVSE", "type": "integer", "mutability": "ReadOnly"},
            {"name": "MaxScheduledChargingProfilesPerEVSEConnector", "type": "integer", "mutability": "ReadOnly"},
        ],
        "V2XController": [
            {"name": "Enabled", "type": "boolean", "mutability": "ReadWrite"},
            {"name": "SupportedOperationModes", "type": "string", "mutability": "ReadOnly"},
            {"name": "TxUpdatedInterval", "type": "string", "mutability": "ReadWrite"},
        ],
        "Security": [
            {"name": "AdditionalRootCertificateCheck", "type": "boolean", "mutability": "ReadWrite"},
            {"name": "CertificateSignedMaxChainSize", "type": "integer", "mutability": "ReadOnly"},
            {"name": "CertificateStoreMaxLength", "type": "integer", "mutability": "ReadOnly"},
            {"name": "CpoName", "type": "string", "mutability": "ReadWrite"},
            {"name": "SecurityProfile", "type": "integer", "mutability": "ReadOnly"},
            {"name": "SupportedFileTransferProtocols", "type": "string", "mutability": "ReadOnly"},
            {"name": "TlsCipherSuite", "type": "string", "mutability": "ReadWrite"},
        ]
    }


def get_initial_device_data(station_id: str, station_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Get initial device data for a new station."""
    return [
        # Charging Station components
        {
            "station_id": station_id,
            "component_name": "ChargingStation",
            "instance": "",
            "variables": [
                {"name": "Model", "value": station_info.get("model", "Unknown")},
                {"name": "VendorName", "value": station_info.get("vendor_name", "Unknown")},
                {"name": "SerialNumber", "value": station_info.get("serial_number", "Unknown")},
                {"name": "FirmwareVersion", "value": station_info.get("firmware_version", "Unknown")},
                {"name": "Modem", "value": station_info.get("modem", "Unknown")},
                {"name": "SupportedFeatures", "value": "Core,SmartCharging,RemoteTrigger"},
                {"name": "SupportedProtocols", "value": "OCPP2.0.1"},
                {"name": "SupportedChargingProfilePurposeTypes", "value": "ChargingStationMaxProfile,TxDefaultProfile,TxProfile,V2XProfile"},
                {"name": "SupportedChargingProfileTypes", "value": "Absolute,Recurring,Relative"},
                {"name": "SupportedMeasurands", "value": "Energy.Active.Import.Register,Power.Active.Import,SoC,Voltage,Current.Import"},
                {"name": "SupportedCableTypes", "value": "IEC_62196_T2"},
                {"name": "SupportedConnectorTypes", "value": "IEC_62196_T2"},
                {"name": "SupportedDisplayMessageTypes", "value": "Normal"},
                {"name": "SupportedIdTokenTypes", "value": "ISO14443,KeyCode"},
                {"name": "SupportedMessageTypes", "value": "Core,SmartCharging,RemoteTrigger"},
                {"name": "SupportedOcppVersions", "value": "2.0.1"},
                {"name": "SupportedReservationTypes", "value": "Reservation"},
                {"name": "SupportedSecurityProfiles", "value": "1,2,3"},
                {"name": "SupportedUnitTypes", "value": "Wh,kWh"},
                {"name": "SupportedV2GModes", "value": "CentralSetpoint"},
                {"name": "SupportedV2XChargingCtrlrTypes", "value": "CentralSetpoint"},
            ]
        },
        # EVSE components
        {
            "station_id": station_id,
            "component_name": "EVSE",
            "instance": "1",
            "variables": [
                {"name": "AvailabilityState", "value": "Operative"},
                {"name": "Status", "value": "Available"},
                {"name": "Power", "value": "22.0"},
                {"name": "NominalVoltage", "value": "400.0"},
                {"name": "ConnectorType", "value": "IEC_62196_T2"},
                {"name": "ConnectorTypeId", "value": "1"},
                {"name": "MaxEnergy", "value": "100.0"},
                {"name": "MinEnergy", "value": "0.1"},
                {"name": "PowerType", "value": "AC_3_PHASE"},
            ]
        },
        # Connector components
        {
            "station_id": station_id,
            "component_name": "Connector",
            "instance": "1",
            "variables": [
                {"name": "AvailabilityState", "value": "Operative"},
                {"name": "Status", "value": "Available"},
                {"name": "ConnectorType", "value": "IEC_62196_T2"},
                {"name": "ConnectorTypeId", "value": "1"},
                {"name": "MaxEnergy", "value": "100.0"},
                {"name": "MinEnergy", "value": "0.1"},
                {"name": "NominalVoltage", "value": "400.0"},
                {"name": "Power", "value": "22.0"},
                {"name": "PowerType", "value": "AC_3_PHASE"},
            ]
        },
        # Smart Charging components
        {
            "station_id": station_id,
            "component_name": "SmartCharging",
            "instance": "",
            "variables": [
                {"name": "ChargingProfileMaxStackLevel", "value": "10"},
                {"name": "ChargingScheduleAllowedChargingRateUnit", "value": "W,A"},
                {"name": "ChargingScheduleMaxPeriods", "value": "1024"},
                {"name": "ConnectorSwitch3to1PhaseSupported", "value": "false"},
                {"name": "MaxChargingProfilesInstalled", "value": "4"},
                {"name": "MaxScheduledChargingProfiles", "value": "4"},
                {"name": "MaxScheduledChargingProfilesPerEVSE", "value": "4"},
                {"name": "MaxScheduledChargingProfilesPerEVSEConnector", "value": "4"},
            ]
        },
        # Security components
        {
            "station_id": station_id,
            "component_name": "Security",
            "instance": "",
            "variables": [
                {"name": "AdditionalRootCertificateCheck", "value": "false"},
                {"name": "CertificateSignedMaxChainSize", "value": "5"},
                {"name": "CertificateStoreMaxLength", "value": "20"},
                {"name": "CpoName", "value": "Favonius Energy"},
                {"name": "SecurityProfile", "value": "1"},
                {"name": "SupportedFileTransferProtocols", "value": "HTTPS"},
                {"name": "TlsCipherSuite", "value": "TLS_AES_256_GCM_SHA384"},
            ]
        },
        # V2X Controller components
        {
            "station_id": station_id,
            "component_name": "V2XController",
            "instance": "",
            "variables": [
                {"name": "Enabled", "value": "true"},
                {"name": "SupportedOperationModes", "value": "CentralSetpoint"},
                {"name": "TxUpdatedInterval", "value": "CentralSetpoint:30"},
            ]
        }
    ]
