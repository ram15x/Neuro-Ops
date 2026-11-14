import pytest
import sys
import os

# Add parent directory to path to import backend modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.services.failure_predictor import predict_failure_from_status


class TestFailurePredictor:
    """Unit tests for failure prediction service"""
    
    def test_normal_cpu_no_prediction(self):
        """CPU < 45% should return normal status"""
        result = predict_failure_from_status(
            cpu_usage=30.0,
            status_check_ok=True,
            recent_reboots=0,
            instance_age_days=30
        )
        
        assert result["will_fail_soon"] is False
        assert result["failure_probability"] == 0.0
        assert result["risk_level"] == "NORMAL"
    
    def test_status_check_failing(self):
        """Status check failing should trigger critical"""
        result = predict_failure_from_status(
            cpu_usage=50.0,
            status_check_ok=False,
            recent_reboots=0,
            instance_age_days=30
        )
        
        assert result["will_fail_soon"] is True
        assert result["failure_probability"] == 95.0
        assert result["risk_level"] == "CRITICAL"
    
    def test_high_cpu_with_reboots(self):
        """without a trained model, 90% CPU falls to the rule-based warning band"""
        result = predict_failure_from_status(
            cpu_usage=90.0,
            status_check_ok=True,
            recent_reboots=3,
            instance_age_days=30
        )

        assert result["will_fail_soon"] is False
        assert result["risk_level"] == "WARNING"
        assert result["model_used"] == "rule_based"

    def test_very_high_cpu(self):
        """CPU above 95% is critical no matter what the model says"""
        result = predict_failure_from_status(
            cpu_usage=96.0,
            status_check_ok=True,
            recent_reboots=0,
            instance_age_days=30
        )

        assert result["will_fail_soon"] is True
        assert result["failure_probability"] == 85.0
        assert result["risk_level"] == "CRITICAL"

    def test_high_cpu_warning(self):
        """CPU 80-90% should be warning"""
        result = predict_failure_from_status(
            cpu_usage=85.0,
            status_check_ok=True,
            recent_reboots=0,
            instance_age_days=30
        )
        
        assert result["will_fail_soon"] is False
        assert result["failure_probability"] == 50.0
        assert result["risk_level"] == "WARNING"