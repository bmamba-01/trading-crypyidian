import subprocess
import sys
from pathlib import Path


def test_learning_evaluation_imports_from_a_clean_process() -> None:
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import trading_bot.application.learning_evaluation; "
            "from trading_bot.adapters.database.repositories "
            "import LearningScorerTestOpportunityProofRepository",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_replay_training_rights_cold_imports_with_scorer_access_exports() -> None:
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from trading_bot.adapters.database.replay_training_rights "
            "import DatabaseReplayTrainingRightsGate, training_scope_digest; "
            "from trading_bot.adapters.database.repositories import "
            "LearningScorerTestAccessRepository, "
            "LearningScorerTestEvaluationReportRepository; "
            "assert DatabaseReplayTrainingRightsGate and training_scope_digest; "
            "assert LearningScorerTestAccessRepository and "
            "LearningScorerTestEvaluationReportRepository",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
