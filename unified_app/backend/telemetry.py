"""
backend/telemetry.py
Alias module exposing LiveTelemetry and telemetry from batch_scheduler.
"""

from backend.batch_scheduler import telemetry, LiveTelemetry

__all__ = ["telemetry", "LiveTelemetry"]
