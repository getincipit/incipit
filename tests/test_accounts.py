"""Accounts, sessions, assignment and the changelog."""


import pytest

from incipit import accounts, db


@pytest.fixture
def conn(tmp_path):
    """The whole schema, not just the accounts tables."""
    path = str(tmp_path / "accounts.db")
    db.init_db(path)
    c = db.open_connection(path)
    accounts.init_accounts(c)
    return c


@pytest.fixture
def team(conn, monkeypatch):
    """The worked example: two admins, two reviewers, one folder each."""
    ids = {
        "paul": accounts.create_user(conn, "paul", "paul-password", "Paul", "admin"),
        "louie": accounts.create_user(conn, "louie", "louie-password", "Louie", "admin"),
        "john": accounts.create_user(conn, "john", "john-password", "John", "reviewer"),
        "jack": accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer"),
    }
    accounts.assign_folder(conn, "E2002", "new", ids["john"], ids["paul"])
    accounts.assign_folder(conn, "E2003", "new", ids["jack"], ids["louie"])
    return ids


def user(conn, uid):
    return accounts.get_user(conn, uid)


def _legacy_assignment_table(conn):
    """The table as an older install created it, with a CHECK on the workflow column."""
    legacy = accounts.SCHEMA_ASSIGNMENTS.replace(
        "workflow    TEXT NOT NULL,", "workflow    TEXT NOT NULL CHECK (workflow IN ('new')),")
    assert "CHECK" in legacy
    conn.executescript("ALTER TABLE folder_assignments RENAME TO saved_assignments;" + legacy +
                      "INSERT INTO folder_assignments SELECT * FROM saved_assignments;"
                      "DROP TABLE saved_assignments;")


# ---------- passwords ----------


def test_a_password_is_never_stored(conn):
    accounts.create_user(conn, "john", "correct-horse", "John")
    stored = accounts.get_user_by_name(conn, "john")["password_hash"]

    assert "correct-horse" not in stored
    assert stored.startswith("scrypt$")
    assert accounts.verify_password("correct-horse", stored)
    assert not accounts.verify_password("Correct-Horse", stored)


def test_the_same_password_hashes_differently_for_two_people(conn):
    """Per-user salt."""
    accounts.create_user(conn, "john", "shared-password")
    accounts.create_user(conn, "jack", "shared-password")

    a = accounts.get_user_by_name(conn, "john")["password_hash"]
    b = accounts.get_user_by_name(conn, "jack")["password_hash"]
    assert a != b


def test_a_corrupt_hash_denies_access_rather_than_crashing(conn):
    """A damaged row must fail closed."""
    for junk in ("", "not-a-hash", "scrypt$bad", "bcrypt$1$2$3$4$5"):
        assert accounts.verify_password("anything", junk) is False


def test_a_short_password_is_refused(conn):
    with pytest.raises(accounts.AuthError, match="at least"):
        accounts.create_user(conn, "john", "short")


# ---------- who may become an admin ----------


def test_a_reviewer_can_enrol_without_any_secret(conn):
    """Self-enrolment is safe for reviewers because a new reviewer sees nothing at all until an admin
    assigns them something."""
    uid = accounts.create_user(conn, "jack", "jack-password", "Jack", "reviewer")
    assert accounts.get_user(conn, uid)["role"] == "reviewer"


def test_no_account_is_created_as_the_owner_except_by_create_owner(conn):
    """The owner is made once, through the bootstrap; create_user cannot make a second."""
    with pytest.raises(accounts.AuthError, match="cannot be created"):
        accounts.create_user(conn, "sneaky", "a-password", "Sneaky", "owner")


def test_usernames_are_unique_case_insensitively(conn):
    """"John" and "john" as two accounts is a quiet way to lose work -- someone signs in as the one
    with no assignments and finds an empty dashboard."""
    accounts.create_user(conn, "john", "john-password")
    with pytest.raises(accounts.AuthError, match="already taken"):
        accounts.create_user(conn, "John", "another-password")


# ---------- signing in ----------


def test_login_returns_a_token_that_identifies_the_user(conn, team):
    token = accounts.login(conn, "john", "john-password")
    assert accounts.user_for_token(conn, token)["username"] == "john"


def test_a_session_token_is_never_stored(conn, team):
    """The cookie holds the token; the database holds only its hash."""
    token = accounts.login(conn, "john", "john-password")
    stored = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]

    assert token not in stored
    assert all(len(h) == 64 for h in stored)


def test_a_wrong_username_and_a_wrong_password_are_indistinguishable(conn, team):
    """Saying which half was wrong turns one guess into two."""
    with pytest.raises(accounts.AuthError) as no_such:
        accounts.login(conn, "nobody", "whatever")
    with pytest.raises(accounts.AuthError) as bad_password:
        accounts.login(conn, "john", "wrong-password")

    assert str(no_such.value) == str(bad_password.value)


def test_a_deactivated_account_cannot_sign_in_and_its_sessions_end(conn, team):
    """How someone leaves the project."""
    token = accounts.login(conn, "john", "john-password")
    assert accounts.user_for_token(conn, token) is not None

    accounts.set_active(conn, team["john"], False)

    assert accounts.user_for_token(conn, token) is None
    with pytest.raises(accounts.AuthError, match="deactivated"):
        accounts.login(conn, "john", "john-password")


def test_changing_a_password_ends_every_existing_session(conn, team):
    """A password change is usually a response to "someone else may have this"."""
    old = accounts.login(conn, "john", "john-password")
    accounts.set_password(conn, team["john"], "a-brand-new-password")

    assert accounts.user_for_token(conn, old) is None
    assert accounts.user_for_token(conn, accounts.login(conn, "john", "a-brand-new-password"))


def test_an_expired_session_stops_working(conn, team):
    token = accounts.login(conn, "john", "john-password")
    conn.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00+00:00'")

    assert accounts.user_for_token(conn, token) is None
    assert accounts.purge_expired_sessions(conn) == 1


def test_authenticating_a_request_does_not_write_the_session_row(conn, team):
    """A loaded UI polls and fetches many images; reads must not churn SQLite and backups."""
    token = accounts.login(conn, "john", "john-password")
    conn.execute("UPDATE sessions SET last_seen_at = 'sentinel'")

    assert accounts.user_for_token(conn, token)["username"] == "john"
    assert conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0] == "sentinel"


def test_logout_invalidates_only_that_session(conn, team):
    laptop = accounts.login(conn, "john", "john-password")
    phone = accounts.login(conn, "john", "john-password")

    accounts.logout(conn, laptop)

    assert accounts.user_for_token(conn, laptop) is None
    assert accounts.user_for_token(conn, phone) is not None


def test_a_garbage_token_is_simply_nobody(conn, team):
    for token in (None, "", "not-a-real-token", "x" * 200):
        assert accounts.user_for_token(conn, token) is None


# ---------- isolation: the property the whole design rests on ----------


def test_a_reviewers_dashboard_lists_only_what_they_hold(conn, team):
    mine = accounts.folders_for_reviewer(conn, team["john"])
    assert [r["folder_id"] for r in mine] == ["E2002"]


# ---------- the workflow ----------


def test_the_round_trip_the_editorial_team_described(conn, team):
    """Paul assigns E2002 to John; John submits; Paul returns it with notes; John resubmits; Paul
    approves."""
    john, paul = user(conn, team["john"]), user(conn, team["paul"])

    accounts.set_folder_status(conn, "E2002", "new", "submitted", john, "first pass done")
    accounts.set_folder_status(conn, "E2002", "new", "returned", paul, "check the dates on pages 3-5")
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john, "dates corrected")
    accounts.set_folder_status(conn, "E2002", "new", "approved", paul, "looks good")

    assert accounts.get_assignment(conn, "E2002", "new")["status"] == "approved"
    story = [(e["action"], e["by"]) for e in accounts.folder_history(conn, "E2002", "new")]
    assert story == [
        ("assigned", "Paul"),
        ("submitted", "John"),
        ("returned", "Paul"),
        ("submitted", "John"),
        ("approved", "Paul"),
    ]
    assert "check the dates" in accounts.folder_history(conn, "E2002", "new")[2]["note"]


def test_a_reviewer_cannot_approve_their_own_work(conn, team):
    """Approval is what makes a folder exportable, so letting a reviewer set it would hand them the
    export gate through the back door."""
    john = user(conn, team["john"])

    with pytest.raises(accounts.PermissionDenied, match="only an admin"):
        accounts.set_folder_status(conn, "E2002", "new", "approved", john)
    with pytest.raises(accounts.PermissionDenied, match="only an admin"):
        accounts.set_folder_status(conn, "E2002", "new", "returned", john)


def test_a_reviewer_cannot_touch_a_folder_that_is_not_theirs(conn, team):
    jack = user(conn, team["jack"])
    with pytest.raises(accounts.PermissionDenied, match="not assigned to you"):
        accounts.set_folder_status(conn, "E2002", "new", "submitted", jack)


def test_reassigning_moves_a_folder_rather_than_duplicating_it(conn, team):
    """One folder, one owner."""
    accounts.assign_folder(conn, "E2002", "new", team["jack"], team["louie"], "John is away")

    assert accounts.get_assignment(conn, "E2002", "new")["assigned_to"] == team["jack"]
    assert len(conn.execute("SELECT * FROM folder_assignments WHERE folder_id='E2002'").fetchall()) == 1
    assert accounts.folders_for_reviewer(conn, team["john"]) == []
    # And the handover is visible rather than inferred from a changed name.
    assert [e["action"] for e in accounts.folder_history(conn, "E2002", "new")][-1] == "reassigned"


def test_reassignment_resets_the_status_so_the_new_reviewer_starts_clean(conn, team):
    john = user(conn, team["john"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)

    accounts.assign_folder(conn, "E2002", "new", team["jack"], team["paul"])

    assert accounts.get_assignment(conn, "E2002", "new")["status"] == "assigned"


def test_the_history_survives_the_person_leaving(conn, team):
    """"Completed by John" has to stay true after John's account is gone -- the changelog is a
    record of what happened, not a list of current staff."""
    john = user(conn, team["john"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)
    accounts.set_active(conn, team["john"], False)

    assert [e["by"] for e in accounts.folder_history(conn, "E2002", "new")][-1] == "John"


# ---------- the admin roster ----------


def test_the_roster_shows_who_is_doing_what(conn, team):
    john = user(conn, team["john"])
    accounts.set_folder_status(conn, "E2002", "new", "submitted", john)

    rows = {r["folder_id"]: r for r in accounts.roster(conn)}

    assert rows["E2002"]["reviewer"] == "John"
    assert rows["E2002"]["assigned_by"] == "Paul"
    assert rows["E2002"]["status"] == "submitted"
    assert rows["E2003"]["reviewer"] == "Jack"
    assert rows["E2003"]["assigned_by"] == "Louie"
    assert rows["E2002"]["last_activity"]


def test_the_roster_covers_every_reviewer_not_just_the_busy_ones(conn, team):
    assert {r["reviewer"] for r in accounts.roster(conn)} == {"John", "Jack"}
