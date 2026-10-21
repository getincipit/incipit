"""The folder card: whose progress the bar measures, and what each role is allowed to see."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from incipit import accounts, collab, db, scoping
from conftest import insert_rows, terms_js

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


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
    }
    accounts.assign_folder(conn, "E2002", "new", ids["john"], ids["paul"])
    return ids


def docs(conn, folder_id, n):
    insert_rows(conn,
        "INSERT OR IGNORE INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
        "VALUES (?, 'now', ?, ?)", (f"B-{folder_id}", n, folder_id))
    for i in range(n):
        insert_rows(conn,
            "INSERT INTO new_documents (document_id, batch_id, status, review_status) "
            "VALUES (?, ?, 'drafted', 'unreviewed')",
            (f"{folder_id}AA{chr(ord('A') + i)}", f"B-{folder_id}"))


def reviewer_accepts(conn, *document_ids):
    for document_id in document_ids:
        db.set_document_review(conn, document_id, "new", "reviewer", "accepted", "John")


def admin_accepts(conn, *document_ids):
    for document_id in document_ids:
        conn.execute("UPDATE new_documents SET review_status = 'accepted' WHERE document_id = ?",
                     (document_id,))
        db.set_document_review(conn, document_id, "new", "admin", "accepted", "Paul")


def card(conn, team, who):
    folders = scoping.filter_folders(
        conn, accounts.get_user(conn, team[who]), [{"folder_id": "E2002"}])
    return folders[0]["progress"]


# ---------- the counts are kept per role ----------


def test_a_reviewers_accepts_do_not_fill_the_reviewers_track(conn, team):
    """The reported bug, at its source."""
    docs(conn, "E2002", 4)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB", "E2002AAC", "E2002AAD")

    p = card(conn, team, "paul")

    assert p["viewer"]["reviewed"] == 0
    assert p["other"]["reviewed"] == 4


def test_the_reviewers_own_accepts_fill_their_track(conn, team):
    docs(conn, "E2002", 4)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB", "E2002AAC", "E2002AAD")
    admin_accepts(conn, "E2002AAA", "E2002AAB")

    p = card(conn, team, "paul")

    assert p["viewer"]["reviewed"] == 2
    assert p["viewer"]["percent"] == 50
    assert p["other"]["reviewed"] == 4


def test_a_flag_counts_as_reviewed_not_as_accepted(conn, team):
    docs(conn, "E2002", 2)
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAA"], team["paul"])

    p = card(conn, team, "paul")

    assert p["viewer"] == {**p["viewer"], "reviewed": 1, "accepted": 0, "needs_work": 1}


def test_an_untouched_folder_leaves_both_tracks_at_nothing(conn, team):
    docs(conn, "E2002", 3)

    p = card(conn, team, "paul")

    assert p["viewer"]["reviewed"] == 0 and p["other"]["reviewed"] == 0
    assert p["viewer"]["percent"] == 0


def test_the_reviewer_sees_their_own_track(conn, team):
    docs(conn, "E2002", 4)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB")

    p = card(conn, team, "john")

    assert p["viewer"]["reviewed"] == 2
    assert p["viewer"]["percent"] == 50


def test_an_empty_folder_reports_no_percentage_rather_than_a_full_one(conn, team):
    p = card(conn, team, "paul")

    assert p["total"] == 0 and p["viewer"]["percent"] == 0


# ---------- and the reviewer's verdicts stay on the reviewer's side of the wall ----------


def test_a_reviewer_is_never_sent_the_reviewers_counts(conn, team):
    """Reported: "the reviewer can see the number of unaccepted documents that the admin chose"."""
    docs(conn, "E2002", 4)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB", "E2002AAC", "E2002AAD")
    admin_accepts(conn, "E2002AAA")

    p = card(conn, team, "john")

    assert "other" not in p and "other_name" not in p
    assert "by_role" not in p, "the whole breakdown was riding along under another name"
    assert json.dumps(p).count("admin") == 0


def test_a_reviewers_own_numbers_do_not_carry_the_reviewers_flags(conn, team):
    """The merged counts were the subtler leak: a reviewer's flag showed up in the reviewer's own
    total as "1 flagged", telling them a document had failed before any note reached them."""
    docs(conn, "E2002", 3)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB", "E2002AAC")
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAC"], team["paul"])

    p = card(conn, team, "john")

    assert p["needs_work"] == 0, "the reviewer's flag is not the reviewer's"
    # Their own accept on the returned document was withdrawn, so it is back in their queue -- 2 of
    # 3, and nothing on the card says who asked for that or why.
    assert p["accepted"] == 2
    assert p["label"] == "2 of 3 accepted"


def test_the_reviewer_still_sees_the_flag_they_raised(conn, team):
    docs(conn, "E2002", 3)
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAC"], team["paul"])

    assert card(conn, team, "paul")["viewer"]["needs_work"] == 1


def test_the_shared_gates_still_read_the_merged_counts(conn, team):
    """Approval and export key on the reviewer's own column, and none of this changes that."""
    docs(conn, "E2002", 2)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB")

    with pytest.raises(collab.NotReady):
        collab.check_can_approve(conn, "E2002", "new")

    admin_accepts(conn, "E2002AAA", "E2002AAB")
    collab.check_can_approve(conn, "E2002", "new")


# ---------- what the card draws ----------


def run(function_names, call, role="admin", extra=""):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    blocks = []
    for name in function_names:
        start = source.index(f"  function {name}(")
        blocks.append(source[start:source.index("\n  }", start) + 4])
    harness = (
        terms_js() +
        "const escapeHtml = (s) => String(s == null ? '' : s);"
        f"const classes = new Set({json.dumps(['role-' + role])});"
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + extra + "".join(blocks) + f"console.log({call});"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return " ".join(done.stdout.split())


CARD = ["isReviewerView", "fullyExported", "readyToExport", "renderProgress", "markRow",
        "folderReviewerPill", "adminPill", "pillText"]

ADMIN_VIEW = {"progress": {
    "total": 4, "viewer": {"total": 4, "reviewed": 1, "accepted": 1, "needs_work": 0, "percent": 25},
    "other": {"total": 4, "reviewed": 4, "accepted": 4, "needs_work": 0, "percent": 100},
    "other_name": "John Smith", "viewer_role": "admin"}}


def test_the_bar_draws_both_tracks_for_a_reviewer():
    markup = run(CARD, f"renderProgress({json.dumps(ADMIN_VIEW)})")

    assert "is-theirs" in markup and "width:100%" in markup
    assert "is-mine" in markup and "width:25%" in markup
    assert "1 of 4 reviewed" in markup
    assert markup.count('pill-name">John</span> <span class="pill-rest">accepted 4</span>') == 1,         "only the first name, and only once"


def test_a_full_reviewer_track_is_not_by_itself_complete():
    """Green is the folder's state, not one reviewer's tally -- see
    test_green_means_nothing_is_owed.py for what it is allowed to mean."""
    part = run(CARD, f"renderProgress({json.dumps(ADMIN_VIEW)})")
    assert "is-complete" not in part

    full = json.loads(json.dumps(ADMIN_VIEW))
    full["progress"]["viewer"] = {"total": 4, "reviewed": 4, "accepted": 4, "needs_work": 0,
                                  "percent": 100}
    assert "is-complete" not in run(CARD, f"renderProgress({json.dumps(full)})")

    ready = {**full, "assignment_status": "submitted", "all_approved": True}
    assert "is-complete" in run(CARD, f"renderProgress({json.dumps(ready)})")


def test_a_reviewer_gets_one_track_and_no_mention_of_the_reviewer():
    view = {"progress": {"total": 4, "viewer_role": "reviewer",
                         "viewer": {"total": 4, "reviewed": 2, "accepted": 2, "needs_work": 0,
                                    "percent": 50}}}
    markup = run(CARD, f"renderProgress({json.dumps(view)})", role="reviewer")

    assert "is-theirs" not in markup
    assert "2 of 4 reviewed" in markup
    assert "Paul" not in markup and "did" not in markup


def test_a_folder_with_no_documents_draws_no_bar_at_all():
    assert run(CARD, "renderProgress({progress: {total: 0}})") == ""


# ---------- a document sent back lands in the reviewer's queue as work to do ----------


def test_flagging_for_rework_withdraws_the_reviewers_own_accept(conn, team):
    """Otherwise the document still reads Accepted on their screen, and the folder can go straight
    back without the one document it was returned for ever being opened."""
    docs(conn, "E2002", 3)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB", "E2002AAC")

    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAB"], team["paul"])

    assert db.get_document_review(conn, "E2002AAB", "new", "reviewer") == "unreviewed"
    assert db.get_document_review(conn, "E2002AAA", "new", "reviewer") == "accepted"


def test_a_folder_cannot_go_back_until_the_picked_documents_are_looked_at(conn, team):
    docs(conn, "E2002", 2)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB")
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAB"], team["paul"])
    # The send-back route returns the folder in the same request that marks its documents.
    conn.execute("UPDATE folder_assignments SET status = 'returned' WHERE folder_id = 'E2002'")

    with pytest.raises(collab.NotReady) as raised:
        collab.check_can_submit(conn, "E2002", "new")
    assert "E2002AAB" in str(raised.value)


def test_once_the_reviewer_has_looked_again_the_folder_can_go_back(conn, team):
    docs(conn, "E2002", 2)
    reviewer_accepts(conn, "E2002AAA", "E2002AAB")
    collab.mark_documents_for_rework(conn, "E2002", "new", ["E2002AAB"], team["paul"])

    reviewer_accepts(conn, "E2002AAB")

    assert collab.check_can_submit(conn, "E2002", "new")["total"] == 2
    assert collab.documents_sent_back(conn, "E2002", "new") == set()


def test_a_reviewers_own_flag_is_not_mistaken_for_one_the_reviewer_sent_back(conn, team):
    """A reviewer flagging a word they cannot read is a decision of theirs, not a returned document
    -- it must not hold their folder hostage."""
    docs(conn, "E2002", 2)
    reviewer_accepts(conn, "E2002AAA")
    db.set_document_review(conn, "E2002AAB", "new", "reviewer", "needs_work", "John")

    assert collab.documents_sent_back(conn, "E2002", "new") == set()
    assert collab.check_can_submit(conn, "E2002", "new")["total"] == 2


def test_the_sent_back_set_does_not_reach_into_another_folder(conn, team):
    docs(conn, "E2002", 1)
    docs(conn, "E2003", 1)
    collab.mark_documents_for_rework(conn, "E2003", "new", ["E2003AAA"], team["paul"])

    assert collab.documents_sent_back(conn, "E2002", "new") == set()
    assert collab.documents_sent_back(conn, "E2003", "new") == {"E2003AAA"}
    assert collab.documents_sent_back(conn, None, "new") == {"E2003AAA"}


# ---------- an editor's sidebar shows the folders that are theirs ----------


def nav_filter(name, folder, my_id, role="admin"):
    """Runs one FOLDER_FILTERS entry against a folder, as the given viewer."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
    start = source.index("  const FOLDER_FILTERS = {")
    block = source[start:source.index("\n  };", start) + 5]
    harness = (
        terms_js() +
        f"const state = {{ myId: {json.dumps(my_id)} }};"
        f"const classes = new Set({json.dumps(['role-' + role])});"
        "global.document = { body: { classList: { contains: (c) => classes.has(c) } } };"
        + source[source.index("  function fullyExported("):source.index("\n  }", source.index("  function fullyExported(")) + 4] + block +
        f"console.log(FOLDER_FILTERS[{json.dumps(name)}]({json.dumps(folder)}));"
    )
    done = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip() == "true"


PAUL, OTHER_EDITOR = 1, 7

SUBMITTED = {"folder_id": "E2002", "assignment_status": "submitted", "assigned_by": PAUL,
             "progress": {"total": 4, "viewer": {"total": 4, "reviewed": 0, "percent": 0}}}


def test_a_folder_another_editor_gave_out_is_in_the_shared_review_queue():
    """Every admin sees submitted work, regardless of which admin assigned the reviewer."""
    theirs = dict(SUBMITTED, assigned_by=OTHER_EDITOR)

    assert nav_filter("needs-review", SUBMITTED, PAUL) is True
    assert nav_filter("needs-review", theirs, PAUL) is True


def test_an_admin_claim_does_not_change_the_shared_review_queue():
    claimed = dict(SUBMITTED, assigned_by=OTHER_EDITOR, claim={"admin_id": PAUL})

    assert nav_filter("needs-review", claimed, PAUL) is True
    assert nav_filter("needs-review", claimed, OTHER_EDITOR) is True


def test_the_admin_lifecycle_tabs_are_global_not_per_admin():
    for name, status in (("with-reviewer", "assigned"), ("needs-review", "submitted")):
        mine = dict(SUBMITTED, assignment_status=status, assigned_by=PAUL)
        theirs = dict(SUBMITTED, assignment_status=status, assigned_by=OTHER_EDITOR)
        assert nav_filter(name, mine, PAUL) is True, name
        assert nav_filter(name, theirs, PAUL) is True, name


def test_completed_is_driven_by_export_not_assignment_ownership_or_status():
    # Complete, as Completed now requires: approved, and every document it holds went out.
    exported = dict(SUBMITTED, assigned_by=OTHER_EDITOR, all_approved=True, document_count=4,
                    export={"exported_at": "2026-09-24T10:00:00+00:00", "document_count": 4})

    assert nav_filter("completed", exported, PAUL) is True
    assert nav_filter("needs-review", exported, PAUL) is False
    assert nav_filter("with-reviewer", dict(exported, assignment_status="assigned"), PAUL) is False
    assert nav_filter("completed", dict(SUBMITTED, assignment_status="approved"), PAUL) is False


def test_all_still_shows_the_whole_board():
    """Asked for explicitly: the one tab where an editor sees every folder and every reviewer."""
    theirs = dict(SUBMITTED, assigned_by=OTHER_EDITOR)

    assert nav_filter("all", theirs, PAUL) is True


def test_a_reviewer_is_not_narrowed_by_this_at_all():
    """They are only ever sent their own folders, so there is nothing to narrow."""
    assigned = {"folder_id": "E2002", "assignment_status": "assigned",
                "progress": {"total": 4, "viewer": {"total": 4, "reviewed": 0, "percent": 0}}}

    assert nav_filter("mine", assigned, 2, role="reviewer") is True
