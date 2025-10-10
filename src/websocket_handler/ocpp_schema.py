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

-- Log Requests Table
CREATE TABLE IF NOT EXISTS log_requests (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    log_type VARCHAR(50) NOT NULL,
    request_id INTEGER NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 3,
    retry_interval INTEGER NOT NULL DEFAULT 60,
    status VARCHAR(50) NOT NULL DEFAULT 'Accepted',
    additional_info TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, request_id)
);

-- Log Files Table
CREATE TABLE IF NOT EXISTS log_files (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER NOT NULL,
    log_type VARCHAR(50) NOT NULL,
    file_path VARCHAR(500) NOT NULL,
    file_size BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Notify Events Table
CREATE TABLE IF NOT EXISTS notify_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    tech_info TEXT,
    additional_info JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Diagnostic Logs Table
CREATE TABLE IF NOT EXISTS diagnostic_logs (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    level VARCHAR(20) NOT NULL,
    message TEXT NOT NULL,
    component VARCHAR(100),
    event_type VARCHAR(100),
    additional_info JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Firmware Status Logs Table
CREATE TABLE IF NOT EXISTS firmware_status_logs (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) NOT NULL,
    additional_info TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Local List Logs Table
CREATE TABLE IF NOT EXISTS local_list_logs (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    action VARCHAR(50) NOT NULL,
    id_token VARCHAR(255) NOT NULL,
    additional_info JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Station Capabilities Table
CREATE TABLE IF NOT EXISTS station_capabilities (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    supported_log_types VARCHAR(500),
    supported_firmware_types VARCHAR(500),
    max_firmware_size BIGINT,
    supported_checksum_algorithms VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id)
);

-- Firmware Requests Table
CREATE TABLE IF NOT EXISTS firmware_requests (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER NOT NULL,
    location VARCHAR(500) NOT NULL,
    retrieve_date_time TIMESTAMPTZ NOT NULL,
    retry_interval INTEGER,
    retries INTEGER,
    retry_back_off_random_range INTEGER,
    checksum VARCHAR(255),
    checksum_algorithm VARCHAR(50),
    signing_certificate TEXT,
    signature TEXT,
    signing_certificate_chain JSONB,
    request_start_time TIMESTAMPTZ,
    request_stop_time TIMESTAMPTZ,
    status VARCHAR(50) NOT NULL DEFAULT 'Accepted',
    additional_info TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, request_id)
);

-- Firmware Update Events Table
CREATE TABLE IF NOT EXISTS firmware_update_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    tech_info TEXT,
    additional_info JSONB,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Firmware Failure Events Table
CREATE TABLE IF NOT EXISTS firmware_failure_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER,
    failure_type VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Reset Events Table
CREATE TABLE IF NOT EXISTS reset_events (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    reset_type VARCHAR(50) NOT NULL,
    tech_info TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Monitoring Reports Table
CREATE TABLE IF NOT EXISTS monitoring_reports (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER NOT NULL,
    monitoring_base VARCHAR(50) NOT NULL,
    monitoring_criteria JSONB,
    component_variable JSONB,
    generated_at TIMESTAMPTZ NOT NULL,
    tbc BOOLEAN NOT NULL DEFAULT false,
    seq_no INTEGER NOT NULL DEFAULT 1,
    report_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Notified Monitoring Reports Table
CREATE TABLE IF NOT EXISTS notified_monitoring_reports (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    request_id INTEGER NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    tbc BOOLEAN NOT NULL DEFAULT false,
    seq_no INTEGER NOT NULL DEFAULT 1,
    report_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Variable Monitoring Table
CREATE TABLE IF NOT EXISTS variable_monitoring (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    component_instance VARCHAR(255) NOT NULL DEFAULT '',
    variable_name VARCHAR(255) NOT NULL,
    variable_instance VARCHAR(255) NOT NULL DEFAULT '',
    monitoring_criterion VARCHAR(50) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    threshold DECIMAL(10,4),
    delta DECIMAL(10,4),
    period INTEGER,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(station_id, component_name, component_instance, variable_name, variable_instance)
);

-- Periodic Monitoring Data Table
CREATE TABLE IF NOT EXISTS periodic_monitoring_data (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    variable_name VARCHAR(255) NOT NULL,
    value TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Alert Rules Table
CREATE TABLE IF NOT EXISTS alert_rules (
    id SERIAL PRIMARY KEY,
    rule_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    component_name VARCHAR(255) NOT NULL,
    variable_name VARCHAR(255) NOT NULL,
    condition VARCHAR(10) NOT NULL,
    threshold DECIMAL(10,4) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    cooldown_minutes INTEGER NOT NULL DEFAULT 5,
    notification_channels JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(rule_id)
);

-- Alerts Table
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    alert_id VARCHAR(255) NOT NULL,
    rule_id VARCHAR(255),
    station_id VARCHAR(255) NOT NULL,
    component_name VARCHAR(255) NOT NULL,
    variable_name VARCHAR(255) NOT NULL,
    current_value TEXT NOT NULL,
    threshold_value DECIMAL(10,4) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'Active',
    message TEXT NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    acknowledged_by VARCHAR(255),
    resolved_by VARCHAR(255),
    additional_info JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(alert_id)
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

-- Log Requests Indexes
CREATE INDEX IF NOT EXISTS idx_log_requests_station_id ON log_requests(station_id);
CREATE INDEX IF NOT EXISTS idx_log_requests_log_type ON log_requests(log_type);
CREATE INDEX IF NOT EXISTS idx_log_requests_status ON log_requests(status);
CREATE INDEX IF NOT EXISTS idx_log_requests_created_at ON log_requests(created_at);

-- Log Files Indexes
CREATE INDEX IF NOT EXISTS idx_log_files_station_id ON log_files(station_id);
CREATE INDEX IF NOT EXISTS idx_log_files_log_type ON log_files(log_type);
CREATE INDEX IF NOT EXISTS idx_log_files_created_at ON log_files(created_at);

-- Notify Events Indexes
CREATE INDEX IF NOT EXISTS idx_notify_events_station_id ON notify_events(station_id);
CREATE INDEX IF NOT EXISTS idx_notify_events_event_type ON notify_events(event_type);
CREATE INDEX IF NOT EXISTS idx_notify_events_timestamp ON notify_events(timestamp);

-- Diagnostic Logs Indexes
CREATE INDEX IF NOT EXISTS idx_diagnostic_logs_station_id ON diagnostic_logs(station_id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_logs_timestamp ON diagnostic_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_diagnostic_logs_level ON diagnostic_logs(level);

-- Firmware Status Logs Indexes
CREATE INDEX IF NOT EXISTS idx_firmware_status_logs_station_id ON firmware_status_logs(station_id);
CREATE INDEX IF NOT EXISTS idx_firmware_status_logs_timestamp ON firmware_status_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_firmware_status_logs_status ON firmware_status_logs(status);

-- Local List Logs Indexes
CREATE INDEX IF NOT EXISTS idx_local_list_logs_station_id ON local_list_logs(station_id);
CREATE INDEX IF NOT EXISTS idx_local_list_logs_timestamp ON local_list_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_local_list_logs_action ON local_list_logs(action);

-- Station Capabilities Indexes
CREATE INDEX IF NOT EXISTS idx_station_capabilities_station_id ON station_capabilities(station_id);

-- Firmware Requests Indexes
CREATE INDEX IF NOT EXISTS idx_firmware_requests_station_id ON firmware_requests(station_id);
CREATE INDEX IF NOT EXISTS idx_firmware_requests_status ON firmware_requests(status);
CREATE INDEX IF NOT EXISTS idx_firmware_requests_created_at ON firmware_requests(created_at);

-- Firmware Update Events Indexes
CREATE INDEX IF NOT EXISTS idx_firmware_update_events_station_id ON firmware_update_events(station_id);
CREATE INDEX IF NOT EXISTS idx_firmware_update_events_timestamp ON firmware_update_events(timestamp);

-- Firmware Failure Events Indexes
CREATE INDEX IF NOT EXISTS idx_firmware_failure_events_station_id ON firmware_failure_events(station_id);
CREATE INDEX IF NOT EXISTS idx_firmware_failure_events_timestamp ON firmware_failure_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_firmware_failure_events_failure_type ON firmware_failure_events(failure_type);

-- Reset Events Indexes
CREATE INDEX IF NOT EXISTS idx_reset_events_station_id ON reset_events(station_id);
CREATE INDEX IF NOT EXISTS idx_reset_events_timestamp ON reset_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_reset_events_reset_type ON reset_events(reset_type);

-- Monitoring Reports Indexes
CREATE INDEX IF NOT EXISTS idx_monitoring_reports_station_id ON monitoring_reports(station_id);
CREATE INDEX IF NOT EXISTS idx_monitoring_reports_request_id ON monitoring_reports(request_id);
CREATE INDEX IF NOT EXISTS idx_monitoring_reports_monitoring_base ON monitoring_reports(monitoring_base);
CREATE INDEX IF NOT EXISTS idx_monitoring_reports_created_at ON monitoring_reports(created_at);

-- Notified Monitoring Reports Indexes
CREATE INDEX IF NOT EXISTS idx_notified_monitoring_reports_station_id ON notified_monitoring_reports(station_id);
CREATE INDEX IF NOT EXISTS idx_notified_monitoring_reports_request_id ON notified_monitoring_reports(request_id);
CREATE INDEX IF NOT EXISTS idx_notified_monitoring_reports_created_at ON notified_monitoring_reports(created_at);

-- Variable Monitoring Indexes
CREATE INDEX IF NOT EXISTS idx_variable_monitoring_station_id ON variable_monitoring(station_id);
CREATE INDEX IF NOT EXISTS idx_variable_monitoring_component_variable ON variable_monitoring(component_name, variable_name);
CREATE INDEX IF NOT EXISTS idx_variable_monitoring_criterion ON variable_monitoring(monitoring_criterion);
CREATE INDEX IF NOT EXISTS idx_variable_monitoring_enabled ON variable_monitoring(enabled);

-- Periodic Monitoring Data Indexes
CREATE INDEX IF NOT EXISTS idx_periodic_monitoring_data_station_id ON periodic_monitoring_data(station_id);
CREATE INDEX IF NOT EXISTS idx_periodic_monitoring_data_timestamp ON periodic_monitoring_data(timestamp);
CREATE INDEX IF NOT EXISTS idx_periodic_monitoring_data_component_variable ON periodic_monitoring_data(component_name, variable_name);

-- Alert Rules Indexes
CREATE INDEX IF NOT EXISTS idx_alert_rules_rule_id ON alert_rules(rule_id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_component_variable ON alert_rules(component_name, variable_name);
CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled ON alert_rules(enabled);
CREATE INDEX IF NOT EXISTS idx_alert_rules_severity ON alert_rules(severity);

-- Alerts Indexes
CREATE INDEX IF NOT EXISTS idx_alerts_alert_id ON alerts(alert_id);
CREATE INDEX IF NOT EXISTS idx_alerts_station_id ON alerts(station_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_triggered_at ON alerts(triggered_at);
CREATE INDEX IF NOT EXISTS idx_alerts_component_variable ON alerts(component_name, variable_name);

-- ===== DISPLAY MESSAGE MANAGEMENT =====

-- Display Messages Table
CREATE TABLE IF NOT EXISTS display_messages (
    message_id VARCHAR(255) PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER,
    connector_id INTEGER,
    message_type VARCHAR(50) NOT NULL, -- Normal, Info, Warning, Error
    message_content TEXT NOT NULL,
    language VARCHAR(10) DEFAULT 'en',
    priority INTEGER DEFAULT 0,
    state VARCHAR(50) DEFAULT 'active', -- active, inactive, expired
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_to TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Display Message History Table
CREATE TABLE IF NOT EXISTS display_message_history (
    id SERIAL PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL,
    station_id VARCHAR(255) NOT NULL,
    action VARCHAR(50) NOT NULL, -- created, updated, cleared, expired
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    details JSONB
);

-- Display Message Indexes
CREATE INDEX IF NOT EXISTS idx_display_messages_station_id ON display_messages(station_id);
CREATE INDEX IF NOT EXISTS idx_display_messages_evse_id ON display_messages(evse_id);
CREATE INDEX IF NOT EXISTS idx_display_messages_connector_id ON display_messages(connector_id);
CREATE INDEX IF NOT EXISTS idx_display_messages_state ON display_messages(state);
CREATE INDEX IF NOT EXISTS idx_display_messages_priority ON display_messages(priority);
CREATE INDEX IF NOT EXISTS idx_display_messages_valid_from ON display_messages(valid_from);
CREATE INDEX IF NOT EXISTS idx_display_messages_valid_to ON display_messages(valid_to);
CREATE INDEX IF NOT EXISTS idx_display_messages_created_at ON display_messages(created_at);

-- Display Message History Indexes
CREATE INDEX IF NOT EXISTS idx_display_message_history_message_id ON display_message_history(message_id);
CREATE INDEX IF NOT EXISTS idx_display_message_history_station_id ON display_message_history(station_id);
CREATE INDEX IF NOT EXISTS idx_display_message_history_timestamp ON display_message_history(timestamp);

-- ===== TARIFF AND COST MANAGEMENT =====

-- Tariffs Table
CREATE TABLE IF NOT EXISTS tariffs (
    tariff_id VARCHAR(255) PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    tariff_description VARCHAR(500),
    tariff_currency VARCHAR(3) DEFAULT 'USD',
    tariff_priority INTEGER DEFAULT 0,
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_to TIMESTAMP WITH TIME ZONE,
    tariff_data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Tariff Elements Table
CREATE TABLE IF NOT EXISTS tariff_elements (
    element_id VARCHAR(255) PRIMARY KEY,
    tariff_id VARCHAR(255) NOT NULL REFERENCES tariffs(tariff_id) ON DELETE CASCADE,
    element_type VARCHAR(50) NOT NULL, -- Energy, Time, Parking, Power
    price_per_unit DECIMAL(10,4) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    unit VARCHAR(20) NOT NULL, -- kWh, hour, minute, kW
    valid_from TIMESTAMP WITH TIME ZONE,
    valid_to TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Time-of-Use Periods Table
CREATE TABLE IF NOT EXISTS tou_periods (
    period_id VARCHAR(255) PRIMARY KEY,
    tariff_id VARCHAR(255) NOT NULL REFERENCES tariffs(tariff_id) ON DELETE CASCADE,
    period_name VARCHAR(100) NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    day_of_week INTEGER, -- 0=Sunday, 1=Monday, etc.
    month INTEGER, -- 1-12
    day_of_month INTEGER, -- 1-31
    price_multiplier DECIMAL(5,2) DEFAULT 1.0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Cost Updates Table
CREATE TABLE IF NOT EXISTS cost_updates (
    update_id VARCHAR(255) PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    transaction_id VARCHAR(255),
    evse_id INTEGER,
    connector_id INTEGER,
    total_cost DECIMAL(10,4) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    cost_breakdown JSONB,
    calculated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Tariff Indexes
CREATE INDEX IF NOT EXISTS idx_tariffs_station_id ON tariffs(station_id);
CREATE INDEX IF NOT EXISTS idx_tariffs_valid_from ON tariffs(valid_from);
CREATE INDEX IF NOT EXISTS idx_tariffs_valid_to ON tariffs(valid_to);
CREATE INDEX IF NOT EXISTS idx_tariffs_priority ON tariffs(tariff_priority);

-- Tariff Elements Indexes
CREATE INDEX IF NOT EXISTS idx_tariff_elements_tariff_id ON tariff_elements(tariff_id);
CREATE INDEX IF NOT EXISTS idx_tariff_elements_type ON tariff_elements(element_type);
CREATE INDEX IF NOT EXISTS idx_tariff_elements_valid_from ON tariff_elements(valid_from);
CREATE INDEX IF NOT EXISTS idx_tariff_elements_valid_to ON tariff_elements(valid_to);

-- TOU Periods Indexes
CREATE INDEX IF NOT EXISTS idx_tou_periods_tariff_id ON tou_periods(tariff_id);
CREATE INDEX IF NOT EXISTS idx_tou_periods_day_of_week ON tou_periods(day_of_week);
CREATE INDEX IF NOT EXISTS idx_tou_periods_start_time ON tou_periods(start_time);
CREATE INDEX IF NOT EXISTS idx_tou_periods_end_time ON tou_periods(end_time);

-- Cost Updates Indexes
CREATE INDEX IF NOT EXISTS idx_cost_updates_station_id ON cost_updates(station_id);
CREATE INDEX IF NOT EXISTS idx_cost_updates_transaction_id ON cost_updates(transaction_id);
CREATE INDEX IF NOT EXISTS idx_cost_updates_evse_id ON cost_updates(evse_id);
CREATE INDEX IF NOT EXISTS idx_cost_updates_connector_id ON cost_updates(connector_id);
CREATE INDEX IF NOT EXISTS idx_cost_updates_calculated_at ON cost_updates(calculated_at);

-- ===== GDPR COMPLIANCE =====

-- Customer Information Table
CREATE TABLE IF NOT EXISTS customer_information (
    request_id VARCHAR(255) PRIMARY KEY,
    station_id VARCHAR(255) NOT NULL,
    customer_certificate_id VARCHAR(255),
    id_token VARCHAR(255),
    customer_identifier VARCHAR(255),
    request_type VARCHAR(50) NOT NULL, -- CustomerInformation, DeleteCustomerInformation
    status VARCHAR(50) DEFAULT 'pending', -- pending, processing, completed, failed
    requested_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Data Retention Policies Table
CREATE TABLE IF NOT EXISTS data_retention_policies (
    policy_id VARCHAR(255) PRIMARY KEY,
    data_type VARCHAR(100) NOT NULL, -- transaction_data, meter_values, logs, certificates
    retention_period_days INTEGER NOT NULL,
    anonymization_required BOOLEAN DEFAULT false,
    deletion_method VARCHAR(50) DEFAULT 'soft', -- soft, hard, anonymize
    policy_description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Consent Management Table
CREATE TABLE IF NOT EXISTS consent_records (
    consent_id VARCHAR(255) PRIMARY KEY,
    customer_identifier VARCHAR(255) NOT NULL,
    consent_type VARCHAR(100) NOT NULL, -- data_processing, marketing, analytics, third_party
    consent_status VARCHAR(50) NOT NULL, -- granted, revoked, expired
    consent_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    revocation_date TIMESTAMP WITH TIME ZONE,
    expiry_date TIMESTAMP WITH TIME ZONE,
    consent_method VARCHAR(50), -- explicit, implicit, opt_in, opt_out
    consent_source VARCHAR(100), -- web_portal, mobile_app, charging_station, email
    legal_basis VARCHAR(100), -- consent, legitimate_interest, contract, legal_obligation
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- PII Anonymization Log Table
CREATE TABLE IF NOT EXISTS pii_anonymization_log (
    log_id VARCHAR(255) PRIMARY KEY,
    customer_identifier VARCHAR(255) NOT NULL,
    data_type VARCHAR(100) NOT NULL,
    anonymization_method VARCHAR(50) NOT NULL, -- hash, mask, delete, pseudonymize
    original_value_hash VARCHAR(255), -- Hash of original value for audit
    anonymized_value VARCHAR(255),
    anonymization_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    retention_policy_id VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Data Subject Rights Requests Table
CREATE TABLE IF NOT EXISTS data_subject_requests (
    request_id VARCHAR(255) PRIMARY KEY,
    customer_identifier VARCHAR(255) NOT NULL,
    request_type VARCHAR(50) NOT NULL, -- access, rectification, erasure, portability, restriction
    request_status VARCHAR(50) DEFAULT 'pending', -- pending, in_progress, completed, rejected
    request_date TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completion_date TIMESTAMP WITH TIME ZONE,
    verification_method VARCHAR(100), -- email, phone, id_document, certificate
    verification_status VARCHAR(50) DEFAULT 'pending',
    request_details JSONB,
    response_data JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Customer Information Indexes
CREATE INDEX IF NOT EXISTS idx_customer_information_station_id ON customer_information(station_id);
CREATE INDEX IF NOT EXISTS idx_customer_information_customer_id ON customer_information(customer_identifier);
CREATE INDEX IF NOT EXISTS idx_customer_information_status ON customer_information(status);
CREATE INDEX IF NOT EXISTS idx_customer_information_requested_at ON customer_information(requested_at);

-- Data Retention Policies Indexes
CREATE INDEX IF NOT EXISTS idx_data_retention_policies_data_type ON data_retention_policies(data_type);
CREATE INDEX IF NOT EXISTS idx_data_retention_policies_retention_period ON data_retention_policies(retention_period_days);

-- Consent Records Indexes
CREATE INDEX IF NOT EXISTS idx_consent_records_customer_id ON consent_records(customer_identifier);
CREATE INDEX IF NOT EXISTS idx_consent_records_consent_type ON consent_records(consent_type);
CREATE INDEX IF NOT EXISTS idx_consent_records_status ON consent_records(consent_status);
CREATE INDEX IF NOT EXISTS idx_consent_records_consent_date ON consent_records(consent_date);

-- PII Anonymization Log Indexes
CREATE INDEX IF NOT EXISTS idx_pii_anonymization_customer_id ON pii_anonymization_log(customer_identifier);
CREATE INDEX IF NOT EXISTS idx_pii_anonymization_data_type ON pii_anonymization_log(data_type);
CREATE INDEX IF NOT EXISTS idx_pii_anonymization_date ON pii_anonymization_log(anonymization_date);

-- Data Subject Requests Indexes
CREATE INDEX IF NOT EXISTS idx_data_subject_requests_customer_id ON data_subject_requests(customer_identifier);
CREATE INDEX IF NOT EXISTS idx_data_subject_requests_type ON data_subject_requests(request_type);
CREATE INDEX IF NOT EXISTS idx_data_subject_requests_status ON data_subject_requests(request_status);
CREATE INDEX IF NOT EXISTS idx_data_subject_requests_date ON data_subject_requests(request_date);

-- ===== ERROR HANDLING AND RESILIENCE =====

-- Circuit Breaker States Table
CREATE TABLE IF NOT EXISTS circuit_breaker_states (
    service_name VARCHAR(100) PRIMARY KEY,
    state VARCHAR(20) NOT NULL, -- closed, open, half_open
    failure_count INTEGER DEFAULT 0,
    last_failure_time TIMESTAMP WITH TIME ZONE,
    last_success_time TIMESTAMP WITH TIME ZONE,
    failure_threshold INTEGER DEFAULT 5,
    timeout_seconds INTEGER DEFAULT 60,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Dead Letter Queue Table
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    message_id VARCHAR(255) PRIMARY KEY,
    original_message JSONB NOT NULL,
    error_message TEXT NOT NULL,
    error_type VARCHAR(100) NOT NULL,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    next_retry_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed_at TIMESTAMP WITH TIME ZONE
);

-- Retry Attempts Table
CREATE TABLE IF NOT EXISTS retry_attempts (
    attempt_id VARCHAR(255) PRIMARY KEY,
    message_id VARCHAR(255) NOT NULL,
    attempt_number INTEGER NOT NULL,
    error_message TEXT,
    attempt_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    success BOOLEAN DEFAULT false,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Health Check Results Table
CREATE TABLE IF NOT EXISTS health_check_results (
    check_id VARCHAR(255) PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    check_type VARCHAR(50) NOT NULL, -- database, external_api, websocket, system
    status VARCHAR(20) NOT NULL, -- healthy, unhealthy, degraded
    response_time_ms INTEGER,
    error_message TEXT,
    check_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Graceful Degradation Rules Table
CREATE TABLE IF NOT EXISTS degradation_rules (
    rule_id VARCHAR(255) PRIMARY KEY,
    service_name VARCHAR(100) NOT NULL,
    trigger_condition VARCHAR(200) NOT NULL,
    degradation_action VARCHAR(100) NOT NULL, -- disable_feature, use_fallback, reduce_functionality
    fallback_config JSONB,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Circuit Breaker Indexes
CREATE INDEX IF NOT EXISTS idx_circuit_breaker_states_state ON circuit_breaker_states(state);
CREATE INDEX IF NOT EXISTS idx_circuit_breaker_states_last_failure ON circuit_breaker_states(last_failure_time);

-- Dead Letter Queue Indexes
CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_error_type ON dead_letter_queue(error_type);
CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_next_retry ON dead_letter_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_created_at ON dead_letter_queue(created_at);

-- Retry Attempts Indexes
CREATE INDEX IF NOT EXISTS idx_retry_attempts_message_id ON retry_attempts(message_id);
CREATE INDEX IF NOT EXISTS idx_retry_attempts_attempt_time ON retry_attempts(attempt_time);

-- Health Check Results Indexes
CREATE INDEX IF NOT EXISTS idx_health_check_results_service ON health_check_results(service_name);
CREATE INDEX IF NOT EXISTS idx_health_check_results_status ON health_check_results(status);
CREATE INDEX IF NOT EXISTS idx_health_check_results_check_time ON health_check_results(check_time);

-- Degradation Rules Indexes
CREATE INDEX IF NOT EXISTS idx_degradation_rules_service ON degradation_rules(service_name);
CREATE INDEX IF NOT EXISTS idx_degradation_rules_enabled ON degradation_rules(enabled);

-- ===== TIMESCALE HYPERTABLES =====

-- Convert time-series tables to hypertables
SELECT create_hypertable('device_reports', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('monitoring_reports', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('notified_monitoring_reports', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('periodic_monitoring_data', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('alerts', 'triggered_at', if_not_exists => TRUE);
SELECT create_hypertable('display_messages', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('display_message_history', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('cost_updates', 'calculated_at', if_not_exists => TRUE);
SELECT create_hypertable('customer_information', 'requested_at', if_not_exists => TRUE);
SELECT create_hypertable('consent_records', 'consent_date', if_not_exists => TRUE);
SELECT create_hypertable('pii_anonymization_log', 'anonymization_date', if_not_exists => TRUE);
SELECT create_hypertable('data_subject_requests', 'request_date', if_not_exists => TRUE);
SELECT create_hypertable('dead_letter_queue', 'created_at', if_not_exists => TRUE);
SELECT create_hypertable('retry_attempts', 'attempt_time', if_not_exists => TRUE);
SELECT create_hypertable('health_check_results', 'check_time', if_not_exists => TRUE);
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
SELECT create_hypertable('notify_events', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('diagnostic_logs', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('firmware_status_logs', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('local_list_logs', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('firmware_update_events', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('firmware_failure_events', 'timestamp', if_not_exists => TRUE);
SELECT create_hypertable('reset_events', 'timestamp', if_not_exists => TRUE);

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
SELECT add_compression_policy('notify_events', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('diagnostic_logs', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_compression_policy('firmware_status_logs', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('local_list_logs', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('firmware_update_events', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('firmware_failure_events', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_compression_policy('reset_events', INTERVAL '30 days', if_not_exists => TRUE);

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
SELECT add_retention_policy('notify_events', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('diagnostic_logs', INTERVAL '1 year', if_not_exists => TRUE);
SELECT add_retention_policy('firmware_status_logs', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('local_list_logs', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('firmware_update_events', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('firmware_failure_events', INTERVAL '2 years', if_not_exists => TRUE);
SELECT add_retention_policy('reset_events', INTERVAL '2 years', if_not_exists => TRUE);
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
