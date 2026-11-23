"""Ready check rules and storage (ready_check.py), on an in-memory database."""

import pytest

import ready_check as rc

KICKOFF = 1_800_000_000
HOUR = 3600


@pytest.fixture
def conn():
    c = rc.connect(":memory:")
    rc.register_meeting(c, 1, "Opponent", KICKOFF, season_id=77)
    rc.mark_posted(c, 1, channel_id=10, message_id=20)
    yield c
    c.close()


def vote(conn, user, state, now):
    return rc.apply_vote(conn, 1, user, f"P{user}", state, now)


def test_clicking_current_answer_clears_it():
    assert rc.next_state(rc.IN, rc.IN) == rc.NONE
    assert rc.next_state(rc.NONE, rc.IN) == rc.IN
    assert rc.next_state(rc.IN, rc.OUT) == rc.OUT


def test_late_window():
    assert rc.is_late(KICKOFF, KICKOFF - 30 * HOUR)
    assert not rc.is_late(KICKOFF, KICKOFF - 30 * HOUR - 1)
    assert not rc.is_late(KICKOFF, KICKOFF)


def test_short_roster_thresholds():
    assert rc.due_short_roster(KICKOFF, KICKOFF - 72 * HOUR) is None
    assert rc.due_short_roster(KICKOFF, KICKOFF - 47 * HOUR) == 48
    assert rc.due_short_roster(KICKOFF, KICKOFF - 23 * HOUR) == 24
    assert rc.due_short_roster(KICKOFF, KICKOFF + 1) is None


def test_voice_check_passes_with_five_and_ignores_sixth():
    check = rc.evaluate_voice([1, 2, 3, 4, 5, 6], {1, 2, 3, 4, 5})
    assert check.passed and check.missing == [6]
    assert not rc.evaluate_voice([1, 2, 3, 4, 5], {1, 2, 3, 4}).passed


def test_early_drop_has_no_alert(conn):
    vote(conn, 1, rc.IN, KICKOFF - 5 * 24 * HOUR)
    result = vote(conn, 1, rc.OUT, KICKOFF - 4 * 24 * HOUR)
    assert result.changed and result.notify_kind is None


def test_late_drop_alerts_and_pings_when_short(conn):
    now = KICKOFF - 10 * HOUR
    for user in range(1, 6):
        vote(conn, user, rc.IN, now - 100 * HOUR)
    result = vote(conn, 3, rc.OUT, now)
    assert result.notify_kind == "withdrawal"
    assert result.ping  # 5 were in, so the drop leaves the team short
    assert rc.pending_notifications(conn, now) == []  # grace period
    assert len(rc.pending_notifications(conn, now + rc.WITHDRAWAL_GRACE_SECONDS)) == 1


def test_late_drop_with_spare_player_does_not_ping(conn):
    now = KICKOFF - 10 * HOUR
    for user in range(1, 7):
        vote(conn, user, rc.IN, now - 100 * HOUR)
    assert not vote(conn, 6, rc.OUT, now).ping


def test_clicking_back_in_cancels_pending_alert(conn):
    now = KICKOFF - 10 * HOUR
    vote(conn, 1, rc.IN, now - 100 * HOUR)
    vote(conn, 1, rc.OUT, now)
    result = vote(conn, 1, rc.IN, now + 10)
    assert result.alert_cancelled
    assert rc.pending_notifications(conn, now + rc.WITHDRAWAL_GRACE_SECONDS) == []


def test_back_in_after_sent_alert(conn):
    now = KICKOFF - 10 * HOUR
    vote(conn, 1, rc.IN, now - 100 * HOUR)
    vote(conn, 1, rc.OUT, now)
    (alert,) = rc.pending_notifications(conn, now + rc.WITHDRAWAL_GRACE_SECONDS)
    rc.mark_notification(conn, alert["id"], "sent")
    assert vote(conn, 1, rc.IN, now + 120).notify_kind == "back_in"


def test_voting_closes_at_kickoff(conn):
    with pytest.raises(rc.VotingClosed):
        vote(conn, 1, rc.IN, KICKOFF)


def test_reschedule_resets_answers_and_announces(conn):
    now = KICKOFF - 5 * 24 * HOUR
    vote(conn, 1, rc.IN, now)
    vote(conn, 2, rc.OUT, now)
    assert rc.reschedule(conn, 1, KICKOFF + 24 * HOUR, now + 10)
    assert rc.roster(conn, 1) == {rc.IN: [], rc.OUT: []}
    (announcement,) = rc.pending_announcements(conn)
    assert announcement["kind"] == "reschedule" and announcement["ping"] == 1
    assert not rc.reschedule(conn, 1, KICKOFF + 24 * HOUR, now + 20)  # same time: no-op


def test_attendance_and_season_stats(conn):
    for user in range(1, 7):
        vote(conn, user, rc.IN, KICKOFF - 100 * HOUR)
    meeting = rc.get_meeting(conn, 1)
    # Only four of six are in voice: the two missing count as no-shows.
    rc.record_final_check(conn, meeting, {1, 2, 3, 4}, KICKOFF)
    stats = {row["user_id"]: row for row in rc.season_stats(conn, 77)}
    assert stats[5]["no_shows"] == 1 and stats[1]["no_shows"] == 0
    assert stats[1]["answered"] == 1
