"""StatsCollector: consecutive-miss counting and recovery."""
from eob_stats import StatsCollector, StatsStore
from eob_tariff import TariffSchedule


class FakeClient:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.i = 0

    def get_status(self):
        v = self.sequence[min(self.i, len(self.sequence) - 1)]
        self.i += 1
        return v


def test_miss_counting_and_recovery(tmp_path):
    store = StatsStore(str(tmp_path / "s.db"), TariffSchedule([], "Europe/Prague"), 60)
    good = {"measured_temp_c": 20.0, "relay_on": False}
    col = StatsCollector(FakeClient([{}, {}, {}, good]), store, 60, warn_after_misses=3)

    col.poll_once(); assert col._misses == 1       # miss
    col.poll_once(); assert col._misses == 2        # miss
    col.poll_once(); assert col._misses == 3        # miss -> this one warns
    col.poll_once(); assert col._misses == 0        # good -> streak reset

    assert store._query("SELECT COUNT(*) FROM readings")[0][0] == 1
