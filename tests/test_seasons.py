import config
import seasons


class FakeEsparven:
    def __init__(self, active=None, fail=False):
        self.active, self.fail = active, fail

    def get_active_season_id(self, competition):
        if self.fail:
            raise RuntimeError("down")
        return self.active


def test_upcoming_matches_decide_the_season():
    assert seasons.current_season_id(FakeEsparven(active=77), [{"seasonID": 87}]) == 87


def test_falls_back_to_active_season():
    assert seasons.current_season_id(FakeEsparven(active=77), []) == 77
    assert seasons.current_season_id(FakeEsparven(fail=True), []) is None


def test_override(monkeypatch):
    monkeypatch.setattr(config, "SEASON_OVERRIDE", 60)
    assert seasons.current_season_id(FakeEsparven(active=77), [{"seasonID": 87}]) == 60


def test_cups_dont_count_for_stats():
    assert seasons.counts_for_stats(77)
    assert not seasons.counts_for_stats(48)
    assert not seasons.counts_for_stats(None)
