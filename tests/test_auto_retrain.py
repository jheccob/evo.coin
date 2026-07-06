import unittest

from ia.auto_retrain import evaluate_candidate


def _candidate_metadata(val_accuracy: float, val_loss: float) -> dict:
    return {
        "label_names": ["short", "hold", "long"],
        "metrics": {"val_accuracy": val_accuracy, "val_loss": val_loss},
        "validation": {
            "label_distribution": {"short": 60, "hold": 200, "long": 70},
            "prediction_distribution": {"short": 80, "hold": 180, "long": 70},
            "confusion_matrix": [
                [45, 10, 5],
                [25, 160, 15],
                [10, 10, 50],
            ],
        },
    }


class AutoRetrainTests(unittest.TestCase):
    def test_quality_gate_approves_candidate_above_thresholds(self):
        result = evaluate_candidate(
            candidate_metadata=_candidate_metadata(0.52, 0.95),
            current_metadata={"metrics": {"val_accuracy": 0.50, "val_loss": 1.01}},
            min_val_accuracy=0.38,
            max_val_loss=1.35,
            min_accuracy_delta=-0.03,
        )

        self.assertTrue(result["approved"])
        self.assertGreater(result["accuracy_delta"], 0.0)

    def test_quality_gate_rejects_candidate_with_large_regression(self):
        result = evaluate_candidate(
            candidate_metadata=_candidate_metadata(0.42, 1.00),
            current_metadata={"metrics": {"val_accuracy": 0.50, "val_loss": 1.01}},
            min_val_accuracy=0.38,
            max_val_loss=1.35,
            min_accuracy_delta=-0.03,
        )

        self.assertFalse(result["approved"])
        self.assertTrue(any("accuracy_delta_below_floor" in reason for reason in result["reasons"]))

    def test_quality_gate_rejects_candidate_with_high_loss(self):
        result = evaluate_candidate(
            candidate_metadata=_candidate_metadata(0.60, 1.80),
            current_metadata={},
            min_val_accuracy=0.38,
            max_val_loss=1.35,
            min_accuracy_delta=-0.03,
        )

        self.assertFalse(result["approved"])
        self.assertTrue(any("val_loss_above_ceiling" in reason for reason in result["reasons"]))

    def test_quality_gate_rejects_low_action_precision(self):
        metadata = _candidate_metadata(0.70, 0.80)
        metadata["validation"]["prediction_distribution"] = {"short": 900, "hold": 100, "long": 900}

        result = evaluate_candidate(
            candidate_metadata=metadata,
            current_metadata={},
            min_val_accuracy=0.38,
            max_val_loss=1.35,
            min_accuracy_delta=-0.03,
        )

        self.assertFalse(result["approved"])
        self.assertTrue(any("action_precision_below_floor" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
