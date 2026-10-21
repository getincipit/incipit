"""Who reviewed a folder, by name, in a colour that is theirs everywhere."""

from pathlib import Path

import pytest

from incipit import accounts, db, review_server
from incipit.config import Settings
from conftest import insert_rows, seed_batch, signed_in

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8").replace("\r\n", "\n")
WORKSPACE = (STATIC / "workspace.js").read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture
def site(tmp_path, monkeypatch):
    db_path = str(tmp_path / "extraction.db")
    settings = Settings(bucket_uri=f"file://{(tmp_path / 'bucket').as_posix()}", db_path=db_path)
    application = review_server.create_app(settings, str(tmp_path / "export"))
    with db.connect(db_path) as conn:
        accounts.init_accounts(conn)
        accounts.create_user(conn, "paul", "paul-password", "Paul Israel", "admin")
        accounts.create_user(conn, "louie", "louie-password", "Louie Carlat", "admin")
        accounts.create_user(conn, "john", "john-password", "John Halloran", "reviewer")
        seed_batch(conn, "B", 8, folder="E2002", name="E2002_Advice")
        conn.execute("UPDATE new_batches SET status = 'confirmed' WHERE batch_id = 'B'")
        for letter in "ABCDEFGH":
            insert_rows(conn,"INSERT INTO new_documents (document_id, batch_id, status, review_status) "
                         "VALUES (?, 'B', 'drafted', 'unreviewed')", (f"E2002AA{letter}",))
        accounts.assign_folder(conn, "E2002", "new", accounts.get_user_by_name(conn, "john")["id"],
                               accounts.get_user_by_name(conn, "paul")["id"])
    return application, settings


def review(client, document_id):
    return client.post(f"/api/new/documents/{document_id}/review",
                       json={"review_status": "accepted", "reviewed_text": "t"})


def reviewers(client):
    return client.get("/api/new/folders").json()[0]["reviewers"]


# ---------- the folder names who did the work ----------


def test_one_reviewer_is_named_with_their_count(site):
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")
    for letter in "ABCDE":
        review(paul, f"E2002AA{letter}")

    assert [(r["name"], r["count"]) for r in reviewers(paul)] == [("Paul Israel", 5)]


def test_two_reviewers_are_each_named_with_their_own_count(site):
    """Reported: if Paul and Louie reviewed the folder, both names and both counts."""
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")
    for letter in "ABCDE":
        review(paul, f"E2002AA{letter}")
    louie = signed_in(application, "louie", "louie-password")
    for letter in "FGH":
        review(louie, f"E2002AA{letter}")

    listed = {r["name"]: r["count"] for r in reviewers(louie)}

    assert listed == {"Paul Israel": 5, "Louie Carlat": 3}


def test_every_admin_sees_the_same_names(site):
    """"You've reviewed" read the same to every admin, whoever had done the work."""
    application, _ = site
    review(signed_in(application, "paul", "paul-password"), "E2002AAA")

    seen_by_louie = reviewers(signed_in(application, "louie", "louie-password"))

    assert [r["name"] for r in seen_by_louie] == ["Paul Israel"]


def test_the_busiest_reviewer_comes_first(site):
    application, _ = site
    review(signed_in(application, "paul", "paul-password"), "E2002AAA")
    louie = signed_in(application, "louie", "louie-password")
    for letter in "BCD":
        review(louie, f"E2002AA{letter}")

    assert [r["name"] for r in reviewers(louie)] == ["Louie Carlat", "Paul Israel"]


def test_taking_a_review_back_takes_it_off_their_count(site):
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")
    review(paul, "E2002AAA")
    review(paul, "E2002AAB")
    paul.post("/api/new/documents/E2002AAB/review",
              json={"review_status": "unreviewed", "reviewed_text": "t"})

    assert reviewers(paul)[0]["count"] == 1


def test_a_reviewer_is_not_sent_who_reviewed(site):
    """Admin board, same rule as every other admin field: absent from the payload."""
    application, _ = site
    review(signed_in(application, "paul", "paul-password"), "E2002AAA")

    folder = signed_in(application, "john", "john-password").get("/api/new/folders").json()[0]

    assert "reviewers" not in folder


# ---------- a colour that is theirs everywhere ----------


def test_each_reviewer_gets_a_different_colour(site):
    application, settings = site
    with db.connect(settings.db_path) as conn:
        held = accounts.reviewer_colours(conn)

    assert len(held) == 2 and len(set(held.values())) == 2


def test_a_reviewers_colour_is_the_same_every_time(site):
    application, settings = site
    with db.connect(settings.db_path) as conn:
        first = accounts.reviewer_colours(conn)
    with db.connect(settings.db_path) as conn:
        again = accounts.reviewer_colours(conn)

    assert first == again


def test_the_pill_carries_the_reviewers_colour(site):
    application, settings = site
    paul = signed_in(application, "paul", "paul-password")
    review(paul, "E2002AAA")
    with db.connect(settings.db_path) as conn:
        pauls = accounts.reviewer_colours(conn)[accounts.get_user_by_name(conn, "paul")["id"]]

    pill = reviewers(paul)[0]

    assert pill["colour"] == pauls and pill["bg"].startswith("#") and pill["ink"].startswith("#")


def test_a_reviewer_can_choose_a_free_colour(site):
    application, settings = site
    paul = signed_in(application, "paul", "paul-password")
    free = next(c["key"] for c in paul.get("/api/reviewer-colours").json() if c["held_by"] is None)

    assert paul.post("/api/reviewer-colours/mine", json={"colour": free}).status_code == 200
    with db.connect(settings.db_path) as conn:
        assert accounts.reviewer_colours(conn)[accounts.get_user_by_name(conn, "paul")["id"]] == free


def test_a_colour_another_reviewer_wears_cannot_be_chosen(site):
    application, _ = site
    paul = signed_in(application, "paul", "paul-password")
    louies = next(c for c in paul.get("/api/reviewer-colours").json()
                  if c["held_by_name"] == "Louie Carlat")

    refused = paul.post("/api/reviewer-colours/mine", json={"colour": louies["key"]})

    assert refused.status_code == 409
    assert "Louie Carlat" in refused.json()["detail"]


def test_a_colour_that_is_not_in_the_palette_is_refused(site):
    application, _ = site

    refused = signed_in(application, "paul", "paul-password").post(
        "/api/reviewer-colours/mine", json={"colour": "#ff0000"})

    assert refused.status_code == 400


def test_a_reviewer_has_no_colour_to_choose(site):
    application, _ = site
    john = signed_in(application, "john", "john-password")

    assert john.get("/api/reviewer-colours").status_code == 403


def test_the_colours_stay_clear_of_the_verdict_colours():
    """Green, amber and teal already mean accepted, flagged and reviewed."""
    inks = {ink.lower() for _, _, ink in accounts.REVIEWER_COLOURS}

    assert not inks & {"#1d7a45", "#a1560a", "#0a6f7d"}


# ---------- the page ----------


def test_the_card_no_longer_speaks_for_every_admin_as_you():
    assert "You've reviewed" not in JS


def test_the_card_draws_a_pill_per_reviewer():
    assert "reviewers.map(adminPill)" in JS


def test_only_your_own_pill_opens_the_colour_choice():
    start = JS.index("function adminPill(")
    block = JS[start:JS.index("\n  }", start)]

    assert "r.user_id === state.myId" in block
    assert "data-colour-picker" in block


def test_clicking_your_pill_does_not_also_open_the_folder():
    guard = JS[JS.index("[data-claim], [data-release]"):]
    assert "[data-colour-picker]" in guard[:guard.index(")) return;")]


def test_the_colour_choice_is_in_your_account_settings_too():
    assert 'id="account-colour"' in WORKSPACE
    assert 'me.role === "admin"' in WORKSPACE


def test_a_taken_colour_is_shown_but_cannot_be_pressed():
    assert 'taken ? "disabled" : ""' in WORKSPACE


def test_the_pill_reads_as_a_sentence():
    assert "pillText(first, `reviewed ${r.count}`)" in JS


def test_an_unnamed_reviewer_is_not_cut_to_the():
    """The fallback was split on spaces, so it read "the did 3"."""
    assert 'p.other_name ? p.other_name.split(" ")[0] : `the ${term("reviewer", { lower: true })}`' in JS


def test_the_reviewers_accepts_are_their_own_pill():
    """A reviewer marks a document needing another look with a comment, so accepted is the only
    verdict of theirs a folder card draws."""
    assert 'folderReviewerPill(theirs.accepted, "accepted", "pill-reviewer"' in JS
    assert "pill-flagged" not in JS


def test_reviewers_and_reviewer_sit_on_separate_labelled_rows():
    assert 'markRow(term("admin", { plural: true }), adminMarks)' in JS
    assert 'markRow(term("reviewer"), reviewerMarks)' in JS


def test_a_verdict_the_reviewer_never_gave_draws_nothing():
    start = JS.index("function folderReviewerPill(")
    assert 'if (!count) return "";' in JS[start:start + 200]


def test_the_folder_pill_does_not_replace_the_document_row_pill():
    """Two functions of one name in one scope: the second silently wins."""
    assert JS.count("function adminPill(") == 1


def test_a_review_with_no_recorded_name_still_counts(site):
    """Reported: E2229 read 0 of 10 reviewed with all 10 reviewed, because older reviews carry no name."""
    application, settings = site
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'accepted', reviewer = '' "
                     "WHERE document_id IN ('E2002AAA', 'E2002AAB')")

    listed = reviewers(signed_in(application, "paul", "paul-password"))

    assert [(r["name"], r["count"]) for r in listed] == [("", 2)]


def test_the_pills_add_up_to_every_reviewed_document(site):
    application, settings = site
    paul = signed_in(application, "paul", "paul-password")
    review(paul, "E2002AAA")
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE new_documents SET review_status = 'accepted', reviewer = NULL "
                     "WHERE document_id = 'E2002AAB'")

    assert sum(r["count"] for r in reviewers(paul)) == 2


def test_an_unnamed_review_is_shown_plainly():
    assert 'pillText("Unnamed", `reviewed ${r.count}`)' in JS


def test_a_long_name_gives_way_before_the_count_does():
    """A pill ran past its card; now only the name truncates and the count always shows."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".mark-row-pills .pill { max-width: 100%; min-width: 0;" in css
    assert ".pill-name { min-width: 0; overflow: hidden; text-overflow: ellipsis;" in css
    assert ".pill-rest { flex-shrink: 0;" in css


def test_a_reviewer_saving_afterwards_does_not_take_the_reviewers_place(site):
    """Reported from the host: a reviewer's name appeared under Reviewers. Until 15e67f2 every save
    wrote the reviewer column from the form, so a reviewer saving after an admin had reviewed it
    overwrote the reviewer's name with their own."""
    application, _ = site
    review(signed_in(application, "paul", "paul-password"), "E2002AAA")

    signed_in(application, "john", "john-password").post(
        "/api/new/documents/E2002AAA/review",
        json={"review_status": "accepted", "reviewed_text": "t", "reviewer": "John Halloran"})

    assert [(r["name"], r["count"]) for r in
            reviewers(signed_in(application, "paul", "paul-password"))] == [("Paul Israel", 5 - 4)]


def test_a_reviewer_cannot_put_a_name_on_a_document_nobody_reviewed(site):
    application, _ = site

    signed_in(application, "john", "john-password").post(
        "/api/new/documents/E2002AAB/review",
        json={"review_status": "accepted", "reviewed_text": "t", "reviewer": "John Halloran"})

    assert reviewers(signed_in(application, "paul", "paul-password")) == []
