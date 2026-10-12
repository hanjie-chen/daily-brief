"""The scheduled generate-and-publish operation, with one recovery allowance."""

from .generation import run_generate
from .network_recovery import NetworkRecovery
from .output import publish_brief
from .time_window import daily_window


class DailyRunError(RuntimeError):
    pass


def run_daily(*, output_dir, data_dir, model_backend, capture_model_inputs=False):
    # Keep publication on the same date even if recovery crosses a day boundary.
    date_label = daily_window().date_label
    with NetworkRecovery() as recovery:
        run_generate(
            output_dir=output_dir,
            data_dir=data_dir,
            date_label=date_label,
            model_backend=model_backend,
            capture_model_inputs=capture_model_inputs,
        )
        if recovery.exhausted:
            raise DailyRunError(
                "network recovery exhausted; local results retained, publication skipped"
            )
        return publish_brief(
            brief_dir=output_dir, data_dir=data_dir, date_label=date_label,
        )
