"""Progress, gates, comments, revisions, statistics and notifications."""


import pytest

from incipit import accounts, collab, db
from conftest import insert_rows


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "extraction.db")
    db.init_db(path)
    c = db.open_connection(path)
    accounts.init_accounts(c)
    collab.init_collab(c)
    return c


@pytest.fixture
def team(conn, monkeypatch):
    ids = {
        "paul": accounts.create_user(conn, "paul", "paul-password", "Paul", "admin"),
        "john": accounts.create_user(conn, "john", "john-password", "John", "reviewer"),
        "jack": accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer"),
    }
    accounts.assign_folder(conn, "E2002", "new", ids["john"], ids["paul"])
    return ids


def docs(conn, folder_id, statuses):
    """`statuses` is one review_status per document, e.g. ["accepted", "needs_work", None]."""
    insert_rows(conn,
        "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
        "VALUES (?, 'now', ?, ?)", (f"B-{folder_id}", len(statuses), folder_id))
    for i, status in enumerate(statuses):
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
            "VALUES (?, ?, 'drafted', ?)",
            (f"{folder_id}AA{chr(ord('A') + i)}", f"B-{folder_id}", status or "unreviewed"),
        )


def user(conn, uid):
    return accounts.get_user(conn, uid)


# ---------- progress is computed, never stored ----------


def test_progress_counts_the_documents_that_are_really_there(conn, team):
    docs(conn, "E2002", ["accepted", "accepted", "needs_work", None])

    p = collab.folder_progress(conn, "E2002", "new")

    assert p["total"] == 4
    assert p["accepted"] == 2
    assert p["needs_work"] == 1
    assert p["reviewed"] == 3          # accepted + flagged: both are decisions
    assert p["outstanding"] == 1
    assert p["label"] == "2 of 4 accepted"


def test_an_empty_folder_reports_itself_as_empty_not_as_complete(conn, team):
    """The bug in one line."""
    p = collab.folder_progress(conn, "E2002", "new")

    assert p["total"] == 0
    assert p["percent"] == 0
    assert p["label"] == "no documents yet"


def test_progress_does_not_count_a_neighbouring_folder(conn, team):
    """"E222" would prefix-match every E2226 document."""
    docs(conn, "E2226", ["accepted", "accepted"])
    docs(conn, "E222", ["accepted"])

    assert collab.folder_progress(conn, "E222", "new")["total"] == 1
    assert collab.folder_progress(conn, "E2226", "new")["total"] == 2


def test_progress_is_never_cached(conn, team):
    """Recomputed on every call."""
    docs(conn, "E2002", ["accepted", None])
    assert collab.folder_progress(conn, "E2002", "new")["accepted"] == 1

    conn.execute("UPDATE new_documents SET review_status = 'accepted'")

    assert collab.folder_progress(conn, "E2002", "new")["accepted"] == 2


# ---------- the gates ----------


def test_a_folder_with_unreviewed_documents_cannot_be_submitted(conn, team):
    """"Submitted" used to mean only that a reviewer had pressed a button."""
    docs(conn, "E2002", ["accepted", None, None])

    with pytest.raises(collab.NotReady, match="2 of 3"):
        collab.check_can_submit(conn, "E2002", "new")


def test_an_empty_folder_cannot_be_submitted(conn, team):
    with pytest.raises(collab.NotReady, match="nothing to send"):
        collab.check_can_submit(conn, "E2002", "new")


def test_a_fully_reviewed_folder_can_be_submitted_even_with_flags(conn, team):
    """Flagging is a decision, not an omission -- a reviewer who cannot read a word has done their
    job by saying so, and holding the folder hostage would make flagging useless."""
    docs(conn, "E2002", ["accepted", "needs_work"])

    assert collab.check_can_submit(conn, "E2002", "new")["total"] == 2


def test_a_folder_with_flags_cannot_be_approved(conn, team):
    """A flag is an unresolved question."""
    docs(conn, "E2002", ["accepted", "needs_work"])

    with pytest.raises(collab.NotReady, match="needing work"):
        collab.check_can_approve(conn, "E2002", "new")


def test_an_empty_folder_cannot_be_approved(conn, team):
    """The original defect, now unreachable."""
    with pytest.raises(collab.NotReady, match="nothing to approve"):
        collab.check_can_approve(conn, "E2002", "new")


def test_a_fully_accepted_folder_can_be_approved(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    assert collab.check_can_approve(conn, "E2002", "new")["accepted"] == 2


# ---------- comments ----------


def test_a_comment_records_who_said_it_and_when(conn, team):
    collab.add_comment(conn, "E2002AAA", "new", team["paul"], "check the date on this one")

    [comment] = collab.comments_for(conn, "E2002AAA", "new")
    assert comment["body"] == "check the date on this one"
    assert comment["by"] == "Paul"
    assert comment["by_role"] == "admin"
    assert comment["resolved"] is False


def test_an_empty_comment_is_refused(conn, team):
    for body in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="needs something"):
            collab.add_comment(conn, "E2002AAA", "new", team["paul"], body)


def test_an_enormous_comment_is_refused(conn, team):
    """Unbounded text in a field rendered on every card is a denial-of-service by paste."""
    with pytest.raises(ValueError, match="too long"):
        collab.add_comment(conn, "E2002AAA", "new", team["paul"], "x" * 4001)


def test_resolving_keeps_the_comment(conn, team):
    """A comment is a request for a change; resolved records that it happened."""
    cid = collab.add_comment(conn, "E2002AAA", "new", team["paul"], "fix the date")

    collab.resolve_comment(conn, cid, team["john"])

    [comment] = collab.comments_for(conn, "E2002AAA", "new")
    assert comment["resolved"] is True
    assert comment["body"] == "fix the date"


def test_a_comment_survives_its_author_leaving(conn, team):
    collab.add_comment(conn, "E2002AAA", "new", team["paul"], "still relevant")
    conn.execute("DELETE FROM users WHERE id = ?", (team["paul"],))

    [comment] = collab.comments_for(conn, "E2002AAA", "new")
    assert comment["body"] == "still relevant"
    assert comment["by"] == "(deleted account)"


# ---------- version history ----------


def test_a_revision_keeps_what_was_replaced(conn, team):
    """Without this, a correction silently replaced the previous one and the earlier text was gone:
    no way to see what changed, who changed it, or to undo a bad edit."""
    collab.record_revision(conn, "E2002AAA", "new", "reviewed_text",
                           "the original reading", "the second reading", team["john"])
    collab.record_revision(conn, "E2002AAA", "new", "reviewed_text",
                           "the second reading", "the final reading", team["paul"])

    history = collab.revisions_for(conn, "E2002AAA", "new")

    assert [r["previous"] for r in history] == ["the second reading", "the original reading"]
    assert [r["current"] for r in history] == ["the final reading", "the second reading"]
    assert all(r["has_current"] for r in history)
    assert history[0]["by"] == "Paul"      # newest first: the last thing that happened


def test_a_revision_can_record_a_field_that_was_previously_empty(conn, team):
    """The first edit replaces nothing, and storing NULL is how "there was nothing here" is told
    apart from "it was blank"."""
    collab.record_revision(conn, "E2002AAA", "new", "reviewed_title", None, "A title", team["john"])
    [revision] = collab.revisions_for(conn, "E2002AAA", "new")
    assert revision["previous"] is None
    assert revision["current"] == "A title"


# ---------- contributor statistics ----------


def test_statistics_count_accepted_documents_per_person(conn, team):
    docs(conn, "E2002", ["accepted", "accepted", None])

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["folders_held"] == 1
    assert john["documents_accepted"] == 2


def test_waiting_to_be_reviewed_is_not_counted_as_having_been_sent_back(conn, team):
    """It used to be submissions minus approvals, so a folder handed in and simply waiting read as
    work that had come back -- on the screen a supervisor uses to judge exactly that."""
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["submissions"] == 1
    assert john["returns"] == 0, "waiting for review was counted as a return"


def test_a_return_is_counted_when_one_actually_happens(conn, team):
    docs(conn, "E2002", ["accepted"])
    john, paul = user(conn, team["john"]), user(conn, team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "not yet")

    [row] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert row["returns"] == 1


def test_another_reviewers_returns_are_not_counted_against_this_one(conn, team):
    docs(conn, "E2002", ["accepted"])
    docs(conn, "E2226", ["accepted"])
    accounts.assign_folder(conn, "E2226", "new", team["jack"], team["paul"])
    accounts.set_folder_status(conn, "E2226", "new", "submitted", user(conn, team["jack"]))
    accounts.set_folder_status(conn, "E2226", "new", "returned", user(conn, team["paul"]), "no")

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]
    [jack] = [r for r in collab.contributor_stats(conn) if r["name"] == "Jack"]

    assert john["returns"] == 0
    assert jack["returns"] == 1


def test_statistics_show_how_often_work_came_back(conn, team):
    """More submissions than approvals means work was returned -- the single most useful quality
    signal a supervisor has, and it costs nothing to compute."""
    docs(conn, "E2002", ["accepted", "accepted"])
    john, paul = user(conn, team["john"]), user(conn, team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "not yet")
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "approved", paul)

    [row] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert row["submissions"] == 2
    assert row["folders_approved"] == 1
    assert row["returns"] == 1


def test_statistics_include_a_reviewer_who_has_done_nothing(conn, team):
    """A supervisor needs to see the person with no activity most of all -- omitting them turns a
    roster into a leaderboard."""
    assert {r["name"] for r in collab.contributor_stats(conn)} == {"John", "Jack"}
    [jack] = [r for r in collab.contributor_stats(conn) if r["name"] == "Jack"]
    assert jack["documents_accepted"] == 0
    assert jack["last_active"] is None


def test_statistics_survive_a_malformed_timestamp(conn, team):
    """Turnaround is arithmetic on stored strings."""
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))
    conn.execute("UPDATE folder_assignments SET assigned_at = 'not-a-date'")

    rows = collab.contributor_stats(conn)   # must not raise

    assert [r["name"] for r in rows]


def test_a_reviewers_accept_moves_the_progress_bar(conn, team):
    """Reported: a reviewer accepted documents and the bar stayed at nought, on their screen and on
    the administrator's."""
    docs(conn, "E2002", [None, None])
    db.set_document_review(conn, "E2002AAA", "new", "reviewer", "accepted", "John")

    progress = collab.folder_progress(conn, "E2002", "new")

    assert progress["accepted"] == 1
    assert progress["percent"] == 50


def test_a_flag_outranks_someone_elses_accept(conn, team):
    """An unresolved question is the thing worth showing."""
    docs(conn, "E2002", [None])
    db.set_document_review(conn, "E2002AAA", "new", "reviewer", "needs_work", "John")
    db.set_document_review(conn, "E2002AAA", "new", "admin", "accepted", "Paul")

    assert collab.folder_progress(conn, "E2002", "new")["needs_work"] == 1


def test_the_admins_own_column_still_wins_where_it_has_ruled(conn, team):
    docs(conn, "E2002", ["accepted"])
    db.set_document_review(conn, "E2002AAA", "new", "reviewer", "needs_work", "John")

    assert collab.folder_progress(conn, "E2002", "new")["accepted"] == 1


def test_a_reviewer_can_submit_a_folder_they_have_accepted_throughout(conn, team):
    """The other half of the same fault: the submit gate counted the admin's column, so a reviewer
    who had accepted every document was told every document still needed reviewing."""
    docs(conn, "E2002", [None, None])
    for doc in ("E2002AAA", "E2002AAB"):
        db.set_document_review(conn, doc, "new", "reviewer", "accepted", "John")

    assert collab.check_can_submit(conn, "E2002", "new")["total"] == 2


def test_a_reviewer_still_cannot_submit_with_a_document_untouched(conn, team):
    docs(conn, "E2002", [None, None])
    db.set_document_review(conn, "E2002AAA", "new", "reviewer", "accepted", "John")

    with pytest.raises(collab.NotReady):
        collab.check_can_submit(conn, "E2002", "new")


def test_a_reviewers_accepts_do_not_make_a_folder_approvable(conn, team):
    """Approval releases work for export."""
    docs(conn, "E2002", [None, None])
    for doc in ("E2002AAA", "E2002AAB"):
        db.set_document_review(conn, doc, "new", "reviewer", "accepted", "John")

    with pytest.raises(collab.NotReady):
        collab.check_can_approve(conn, "E2002", "new")


def test_an_admin_accepting_everything_can_still_approve(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])

    assert collab.check_can_approve(conn, "E2002", "new")["total"] == 2


def test_turnaround_is_the_hours_from_being_given_a_folder_to_submitting_it(conn, team):
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))
    conn.execute("UPDATE folder_assignments SET assigned_at = '2026-09-01T09:00:00'")
    conn.execute("UPDATE folder_events SET at = '2026-09-01T15:30:00' WHERE action = 'submitted'")

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["avg_turnaround_hours"] == 6.5


def test_turnaround_measures_the_first_submission_not_the_last(conn, team):
    """Work sent back and resubmitted is what `returns` is for."""
    docs(conn, "E2002", ["accepted"])
    john, paul = user(conn, team["john"]), user(conn, team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "not yet")
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    conn.execute("UPDATE folder_assignments SET assigned_at = '2026-09-01T09:00:00'")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM folder_events WHERE action = 'submitted' ORDER BY id")]
    conn.execute("UPDATE folder_events SET at = '2026-09-01T10:00:00' WHERE id = ?", (ids[0],))
    conn.execute("UPDATE folder_events SET at = '2026-09-03T10:00:00' WHERE id = ?", (ids[1],))

    [john_row] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john_row["avg_turnaround_hours"] == 1.0


def test_turnaround_averages_across_the_folders_someone_holds(conn, team):
    docs(conn, "E2002", ["accepted"])
    docs(conn, "E2226", ["accepted"])
    accounts.assign_folder(conn, "E2226", "new", team["john"], team["paul"])
    for folder in ("E2002", "E2226"):
        accounts.set_folder_status(conn, folder, "new", "submitted", user(conn, team["john"]))
    conn.execute("UPDATE folder_assignments SET assigned_at = '2026-09-01T09:00:00'")
    conn.execute("UPDATE folder_events SET at = '2026-09-01T10:00:00' "
                 "WHERE action = 'submitted' AND folder_id = 'E2002'")
    conn.execute("UPDATE folder_events SET at = '2026-09-01T12:00:00' "
                 "WHERE action = 'submitted' AND folder_id = 'E2226'")

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["avg_turnaround_hours"] == 2.0


def test_turnaround_is_blank_rather_than_zero_before_anything_is_submitted(conn, team):
    """Nothing submitted is not a turnaround of nought hours, and a table that says 0h reads as the
    fastest person on it."""
    docs(conn, "E2002", ["accepted"])

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["avg_turnaround_hours"] is None


def test_last_active_counts_editing_a_document(conn, team):
    """The reported gap: someone part way through a folder has produced no folder event, so the
    supervision table said "never" while they were typing."""
    docs(conn, "E2002", ["unreviewed"])
    collab.record_revision(conn, "E2002AAA", "new", "reviewed_text", "before", "after", team["john"])

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["last_active"], "an edit left no trace in the supervision view"


def test_last_active_counts_accepting_a_document(conn, team):
    """Accepting without changing a word records no revision, only reviewed_at."""
    docs(conn, "E2002", ["accepted"])
    conn.execute("UPDATE new_documents SET reviewer = 'John', reviewed_at = '2026-09-10T12:00:00'")

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["last_active"] == "2026-09-10T12:00:00"


def test_last_active_reports_the_most_recent_of_the_two(conn, team):
    docs(conn, "E2002", ["accepted"])
    conn.execute("UPDATE new_documents SET reviewer = 'John', reviewed_at = '2026-01-01T00:00:00'")
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))

    [john] = [r for r in collab.contributor_stats(conn) if r["name"] == "John"]

    assert john["last_active"] > "2026-01-01T00:00:00"


def test_one_persons_edits_are_not_anothers(conn, team):
    docs(conn, "E2002", ["unreviewed"])
    collab.record_revision(conn, "E2002AAA", "new", "reviewed_text", "before", "after", team["john"])

    [jack] = [r for r in collab.contributor_stats(conn) if r["name"] == "Jack"]

    assert jack["last_active"] is None


# ---------- notifications ----------


def test_a_reviewer_is_told_when_their_folder_comes_back(conn, team):
    docs(conn, "E2002", ["accepted"])
    john, paul = user(conn, team["john"]), user(conn, team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "check the dates")

    news = collab.notifications_for(conn, john)

    assert news["unread"] >= 1
    assert news["items"][0]["action"] == "returned"
    assert news["items"][0]["note"] == "check the dates"
    assert news["items"][0]["by"] == "Paul"


def test_a_reviewer_is_not_notified_of_their_own_actions(conn, team):
    """Being told what you just did yourself is noise, and noise is how a badge stops being read."""
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))

    actions = [i["action"] for i in collab.notifications_for(conn, user(conn, team["john"]))["items"]]

    assert "submitted" not in actions


def test_a_reviewer_is_not_notified_about_someone_elses_folder(conn, team):
    """The isolation rule again, from a third angle: a notification naming a folder Jack cannot see
    would leak both its existence and its state."""
    accounts.assign_folder(conn, "E2003", "new", team["jack"], team["paul"])
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))

    folders = {i["folder_id"] for i in collab.notifications_for(conn, user(conn, team["jack"]))["items"]}

    assert "E2002" not in folders


def test_an_admin_is_told_when_work_arrives(conn, team):
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))

    news = collab.notifications_for(conn, user(conn, team["paul"]))

    assert [i["action"] for i in news["items"]] == ["submitted"]
    assert news["unread"] == 1


def test_marking_read_clears_the_badge_without_deleting_the_history(conn, team):
    docs(conn, "E2002", ["accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]))
    paul = user(conn, team["paul"])
    assert collab.notifications_for(conn, paul)["unread"] == 1

    collab.mark_notifications_read(conn, team["paul"])

    after = collab.notifications_for(conn, paul)
    assert after["unread"] == 0
    assert len(after["items"]) == 1      # the entries stay; only the watermark moved


def test_a_reviewer_with_nothing_assigned_has_no_notifications(conn, team):
    assert collab.notifications_for(conn, user(conn, team["jack"])) == {"unread": 0, "items": []}


def test_notifications_of_nobody_are_empty_rather_than_an_error(conn, team):
    """Called from a route where the user should always exist; failing soft here means a bug
    upstream shows as an empty badge, not a 500 on every page load."""
    assert collab.notifications_for(conn, None) == {"unread": 0, "items": []}


# ---------- assigning a folder claims it ----------


def test_assigning_a_folder_claims_it_for_the_admin_who_did(conn, team):
    """Handing a folder out is taking responsibility for it."""
    claims = db.get_folder_claims(conn)

    assert claims["E2002"]["admin_id"] == team["paul"]
    assert claims["E2002"]["by"] == "Paul"


def test_an_admin_can_release_the_claim_without_disturbing_the_assignment(conn, team):
    """The point of the claim being a courtesy rather than a lock: another editor can pick the
    folder up while the reviewer on it stays exactly where they are."""
    db.release_folder(conn, "E2002", "new")

    assert "E2002" not in db.get_folder_claims(conn)
    assignment = accounts.get_assignment(conn, "E2002", "new")
    assert assignment["assigned_to"] == team["john"]
    assert assignment["status"] == "assigned"


def test_reassigning_moves_the_claim_to_whoever_made_that_call(conn, team):
    """A second admin switching the reviewer is now the one working on it."""
    monkey = accounts.create_user(conn, "ruth", "ruth-password", "Ruth", "admin")
    accounts.assign_folder(conn, "E2002", "new", team["jack"], monkey)

    claims = db.get_folder_claims(conn)
    assert claims["E2002"]["by"] == "Ruth"
    assert accounts.get_assignment(conn, "E2002", "new")["assigned_to"] == team["jack"]


def test_a_released_folder_is_claimed_again_by_the_next_assignment(conn, team):
    db.release_folder(conn, "E2002", "new")

    accounts.assign_folder(conn, "E2002", "new", team["jack"], team["paul"])

    assert db.get_folder_claims(conn)["E2002"]["by"] == "Paul"


def test_assigning_records_no_note_when_the_admin_typed_none(conn, team):
    """It used to manufacture "to <the reviewer's name>", which the folder card then displayed as
    though somebody had written it."""
    [event] = [e for e in accounts.folder_history(conn, "E2002", "new") if e["action"] == "assigned"]

    assert not (event.get("note") or "").strip()


def test_a_note_the_admin_did_type_is_still_recorded(conn, team):
    accounts.assign_folder(conn, "E2226", "new", team["john"], team["paul"], "start with page 3")

    [event] = [e for e in accounts.folder_history(conn, "E2226", "new") if e["action"] == "assigned"]
    assert event["note"] == "start with page 3"


# ---------- sending a folder back names the documents that need work ----------


def test_sending_back_flags_the_documents_the_admin_picked(conn, team):
    docs(conn, "E2002", ["accepted", "accepted", "accepted"])

    flagged = collab.mark_documents_for_rework(
        conn, "E2002", "new", ["E2002AAA", "E2002AAC"], team["paul"])

    assert flagged == 2
    statuses = dict(conn.execute(
        "SELECT document_id, review_status FROM new_documents").fetchall())
    assert statuses["E2002AAA"] == "needs_work"
    assert statuses["E2002AAB"] == "accepted"
    assert statuses["E2002AAC"] == "needs_work"


def test_a_flagged_document_carries_the_admins_own_verdict(conn, team):
    docs(conn, "E2002", ["accepted"])

    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAA"], team["paul"])

    assert db.get_document_review(conn, "E2002AAA", "new", "admin") == "needs_work"


def test_sending_back_cannot_reach_a_document_in_another_folder(conn, team):
    docs(conn, "E2002", ["accepted"])
    docs(conn, "E2003", ["accepted"])

    flagged = collab.mark_documents_for_rework(conn, "E2002", "new", ["E2003AAA"], team["paul"])

    assert flagged == 0
    assert conn.execute("SELECT review_status FROM new_documents WHERE document_id = 'E2003AAA'"
                        ).fetchone()[0] == "accepted"


def test_sending_back_ignores_a_document_that_does_not_exist(conn, team):
    docs(conn, "E2002", ["accepted"])

    assert collab.mark_documents_for_rework(
        conn, "E2002", "new", ["E2002ZZZ"], team["paul"]) == 0


def test_sending_back_with_nothing_picked_flags_nothing(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])

    assert collab.mark_documents_for_rework(conn, "E2002", "new", [], team["paul"]) == 0
    assert collab.folder_progress(conn, "E2002", "new")["needs_work"] == 0


def test_a_flagged_folder_can_no_longer_be_approved(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAB"], team["paul"])

    with pytest.raises(collab.NotReady):
        collab.check_can_approve(conn, "E2002", "new")


# ---------- un-accepting a document takes back the folder's approval ----------


def test_un_accepting_a_document_sends_an_approved_folder_back_to_the_reviewer(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))

    conn.execute("UPDATE new_documents SET review_status = 'unreviewed' "
                 "WHERE document_id = 'E2002AAB'")
    changed = collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"])

    assert changed is True
    assert accounts.get_assignment(conn, "E2002", "new")["status"] == "submitted"


def test_an_approval_stands_while_every_document_is_still_accepted(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))

    assert collab.revoke_approval_if_incomplete(conn, "E2002AAA", "new", team["paul"]) is False
    assert accounts.get_assignment(conn, "E2002", "new")["status"] == "approved"


def test_a_folder_that_was_never_approved_is_left_alone(conn, team):
    docs(conn, "E2002", ["accepted", None])

    assert collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"]) is False
    assert accounts.get_assignment(conn, "E2002", "new")["status"] == "assigned"


def test_withdrawing_an_approval_says_why_in_the_folders_history(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))
    conn.execute("UPDATE new_documents SET review_status = 'flagged' "
                 "WHERE document_id = 'E2002AAB'")

    collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"])

    history = accounts.folder_history(conn, "E2002", "new")
    assert any("approval withdrawn" in (e["note"] or "") for e in history)


def test_an_automatic_note_does_not_pose_as_something_a_reviewer_wrote(conn, team):
    """The folder card shows the latest note somebody actually wrote."""
    from incipit import scoping
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", user(conn, team["john"]),
                               "here is the folder")
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))
    conn.execute("UPDATE new_documents SET review_status = 'unreviewed' "
                 "WHERE document_id = 'E2002AAB'")
    collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"])

    assert scoping.assignment_summary(conn, "E2002", "new")["latest_note"] == "here is the folder"


def test_the_history_still_records_why_an_approval_was_withdrawn(conn, team):
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))
    conn.execute("UPDATE new_documents SET review_status = 'unreviewed' "
                 "WHERE document_id = 'E2002AAB'")
    collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"])

    history = accounts.folder_history(conn, "E2002", "new")
    assert any(accounts.is_system_note(e["note"]) and "approval withdrawn" in e["note"]
               for e in history)


def test_an_automatic_event_is_not_delivered_as_a_notification(conn, team):
    """It would arrive in the bell as though the reviewer had written it to them."""
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))
    conn.execute("UPDATE new_documents SET review_status = 'unreviewed' "
                 "WHERE document_id = 'E2002AAB'")
    collab.revoke_approval_if_incomplete(conn, "E2002AAB", "new", team["paul"])

    items = collab.notifications_for(conn, user(conn, team["john"]))["items"]

    assert not any("approval withdrawn" in (i["note"] or "") for i in items)
    assert any(i["action"] == "assigned" for i in items), "real events still arrive"


def test_the_statistics_name_the_folders_as_well_as_counting_them(conn, team):
    """"How many" is the first question a supervisor asks and "which ones" is the second."""
    docs(conn, "E2002", ["accepted", "accepted"])
    accounts.assign_folder(conn, "E2003", "new", team["john"], team["paul"])
    accounts.set_folder_status(conn, "E2002", "new", "approved", user(conn, team["paul"]))

    mine = [r for r in collab.contributor_stats(conn) if r["name"] == "John"][0]

    assert mine["folders_held"] == len(mine["held"]) == 2
    assert {f["folder_id"] for f in mine["held"]} == {"E2002", "E2003"}
    assert [f["folder_id"] for f in mine["approved"]] == ["E2002"]
    assert mine["folders_approved"] == 1


def test_a_held_folder_carries_the_state_it_is_in(conn, team):
    """The list is read beside the count, so each row has to say where that folder stands."""
    mine = [r for r in collab.contributor_stats(conn) if r["name"] == "John"][0]

    assert mine["held"][0]["status"] == "assigned"
    assert mine["held"][0]["workflow"] == "new"


def test_someone_holding_nothing_reports_empty_lists_rather_than_none(conn, team):
    jack = [r for r in collab.contributor_stats(conn) if r["name"] == "Jack"][0]

    assert jack["held"] == [] and jack["approved"] == []
    assert jack["folders_held"] == 0
