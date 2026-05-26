-- Migration 046: add acknowledged_by_email to notification_alerts.
-- Stored by the alerts.acknowledge / alerts.resolve commands so the
-- full Alert shape can be returned without a cross-pool join.

ALTER TABLE notification_alerts
    ADD COLUMN IF NOT EXISTS acknowledged_by_email TEXT;
