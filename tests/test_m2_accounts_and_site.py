"""Covers M2 (docs/specs/M2-accounts-roles-and-site.md): roles, bootstrap, accounts, join codes, the
org id, site settings and appearance, and the setup wizard."""

import io
import json
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image
from starlette.requests import Request

from incipit import accounts, appearance, bootstrap, cli, db, scoping, site_assets, site_settings
from conftest import make_app, signed_in

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


@pytest.fixture
def site(tmp_path):
    """Olive the owner, Paul an admin, John a reviewer."""
    app, settings = make_app(tmp_path, owner="olive", admins=("paul",), reviewers=("john",))
    return app, settings


def ids(settings):
    with db.connect(settings.db_path) as conn:
        return {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users")}


def png(size=(40, 30), colour=(120, 90, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, kind, name, content, content_type="image/png"):
    return client.post("/api/site/assets", data={"kind": kind}, files={"file": (name, content, content_type)})


# ---------- AC-1: owner, admin and reviewer ----------


def test_ac1_the_database_refuses_the_old_student_role(site):
    _, settings = site
    with pytest.raises(db.IntegrityError), db.connect(settings.db_path) as conn:
        conn.execute("INSERT INTO users (username, display_name, role, password_hash, created_at) "
                     "VALUES ('old', 'Old', 'student', 'x', 'now')")


def test_ac1_no_page_or_script_names_the_student_role():
    found = [p.name for p in STATIC.glob("*.*") if p.suffix in (".js", ".html", ".css")
             and re.search(r"student", p.read_text(encoding="utf-8"), re.I)]
    assert found == []


def test_ac1_the_page_names_roles_by_the_sites_own_labels(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE site_settings SET role_label_reviewer = 'Volunteer'")

    shell = signed_in(app, "john").get("/").text

    assert '"reviewer": "Volunteer"' in shell


# ---------- AC-2: the permission matrix, over HTTP ----------

MATRIX = [
    # (who, method, path, body, expected)
    ("olive", "PUT", "/api/site/settings", {"product_name": "Ours"}, 200),
    ("paul", "PUT", "/api/site/settings", {"product_name": "Ours"}, 200),
    ("john", "PUT", "/api/site/settings", {"product_name": "Ours"}, 403),
    (None, "PUT", "/api/site/settings", {"product_name": "Ours"}, 401),
    ("olive", "PUT", "/api/site/settings", {"appearance": {"custom_css": "a{}"}}, 200),
    ("paul", "PUT", "/api/site/settings", {"appearance": {"custom_css": "a{}"}}, 403),
    ("paul", "PUT", "/api/site/settings", {"join_enabled": False}, 403),
    ("olive", "POST", "/api/team/users", {"username": "rita", "password": "rita-password", "role": "reviewer"}, 200),
    ("paul", "POST", "/api/team/users", {"username": "rita", "password": "rita-password", "role": "reviewer"}, 200),
    ("john", "POST", "/api/team/users", {"username": "rita", "password": "rita-password", "role": "reviewer"}, 403),
    ("olive", "POST", "/api/team/users", {"username": "ada", "password": "ada-password", "role": "admin"}, 200),
    ("paul", "POST", "/api/team/users", {"username": "ada", "password": "ada-password", "role": "admin"}, 403),
    ("olive", "POST", "/api/team/join-codes", {"role": "admin"}, 200),
    ("paul", "POST", "/api/team/join-codes", {"role": "admin"}, 403),
    ("paul", "POST", "/api/team/join-codes", {"role": "reviewer"}, 200),
    ("olive", "POST", "/api/team/users/{john}/role", {"role": "admin"}, 200),
    ("paul", "POST", "/api/team/users/{john}/role", {"role": "admin"}, 403),
    ("paul", "POST", "/api/team/owner/transfer", {"user_id": "{paul}"}, 403),
    ("olive", "POST", "/api/team/users/{paul}/deactivate", None, 200),
    ("paul", "POST", "/api/team/users/{john}/deactivate", None, 200),
    ("paul", "POST", "/api/team/users/{olive}/reset-password", {"password": "taken-over"}, 403),
    ("olive", "POST", "/api/site/org-id", None, 200),
    ("paul", "POST", "/api/site/org-id", None, 403),
    ("john", "GET", "/api/team/roster", None, 403),
    ("john", "DELETE", "/api/new/documents/X", None, 403),
]


@pytest.mark.parametrize("who,method,path,body,expected", MATRIX,
                         ids=[f"{m[0]}-{m[1]}-{m[2]}-{m[4]}" for m in MATRIX])
def test_ac2_the_permission_matrix_holds(site, who, method, path, body, expected):
    app, settings = site
    people = ids(settings)
    path = path.format(**people)
    if isinstance(body, dict):
        body = {k: (people[v[1:-1]] if isinstance(v, str) and v[:1] == "{" else v) for k, v in body.items()}
    client = signed_in(app, who, raise_server_exceptions=False)

    assert client.request(method, path, json=body).status_code == expected


# ---------- AC-3: failing closed ----------


def test_ac3_nobody_in_particular_sees_no_folder(site):
    _, settings = site
    with db.connect(settings.db_path) as conn:
        assert scoping.visible_folder_ids(conn, None) == set()


def test_ac3_a_review_route_reached_without_a_viewer_answers_401(site):
    from fastapi import HTTPException
    app, _ = site
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/new/documents"
                 and "GET" in r.methods)
    request = Request({"type": "http", "method": "GET", "path": "/api/new/documents", "headers": []})

    with pytest.raises(HTTPException) as refused:
        route.endpoint(request)
    assert refused.value.status_code == 401


# ---------- AC-4: the bootstrap token ----------


@pytest.fixture
def unowned(tmp_path):
    app, settings = make_app(tmp_path, admins=(), reviewers=())
    token = site_settings.write_bootstrap_token(settings.db_path)
    return app, settings, token


OWNER = {"username": "olive", "password": "olive-password", "display_name": "Olive"}


def test_ac4_setup_without_the_token_is_not_found(unowned):
    app, _, _ = unowned
    assert signed_in(app, None).get("/setup").status_code == 404


def test_ac4_owner_creation_without_the_token_is_not_found(unowned):
    app, _, _ = unowned
    assert signed_in(app, None).post("/api/setup/owner", json={**OWNER, "token": "guess"}).status_code == 404


def test_ac4_the_token_opens_the_setup_page(unowned):
    app, _, token = unowned
    assert signed_in(app, None).get(f"/setup?token={token}").status_code == 200


def test_ac4_the_token_creates_the_owner(unowned):
    app, settings, token = unowned
    signed_in(app, None).post("/api/setup/owner", json={**OWNER, "token": token})
    with db.connect(settings.db_path) as conn:
        assert accounts.owner(conn)["username"] == "olive"


def test_ac4_the_token_works_once(unowned):
    app, _, token = unowned
    signed_in(app, None).post("/api/setup/owner", json={**OWNER, "token": token})

    again = signed_in(app, None).post("/api/setup/owner", json={**OWNER, "username": "other", "token": token})
    assert again.status_code == 404


def test_ac4_the_token_file_is_gone_once_used(unowned):
    app, settings, token = unowned
    signed_in(app, None).post("/api/setup/owner", json={**OWNER, "token": token})
    assert not site_settings.bootstrap_path(settings.db_path).exists()


def test_ac4_after_setup_a_stranger_is_sent_to_sign_in(unowned):
    app, _, token = unowned
    signed_in(app, None).post("/api/setup/owner", json={**OWNER, "token": token})

    response = signed_in(app, None).get(f"/setup?token={token}", follow_redirects=False)
    assert response.headers["location"].startswith("/login")


# ---------- AC-5: one owner ----------


def test_ac5_two_simultaneous_owner_creations_make_one_owner(tmp_path):
    path = str(tmp_path / "x.db")
    bootstrap.initialise(path)
    barrier, outcomes = threading.Barrier(2), []

    def create(name):
        barrier.wait()
        try:
            with db.connect(path) as conn:
                accounts.create_owner(conn, name, f"{name}-password")
            outcomes.append("made")
        except (accounts.AuthError, db.DatabaseError):
            outcomes.append("refused")

    threads = [threading.Thread(target=create, args=(n,)) for n in ("olive", "oscar")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with db.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'").fetchone()[0] == 1


def test_ac5_the_database_refuses_a_second_owner_row(site):
    _, settings = site
    with pytest.raises(db.IntegrityError), db.connect(settings.db_path) as conn:
        conn.execute("UPDATE users SET role = 'owner' WHERE username = 'paul'")


# ---------- AC-6: the command line ----------


def test_ac6_the_server_will_not_start_without_an_owner_or_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "run_server", lambda *a, **k: pytest.fail("the server started"))
    assert cli.main(["review", "--db", str(tmp_path / "x.db"), "--bucket-uri", "file://./b"]) == 2


def test_ac6_init_with_an_owner_reads_the_password_from_a_file(tmp_path, monkeypatch):
    secret = tmp_path / "owner-password"
    secret.write_text("a-long-owner-password\n", encoding="utf-8")
    monkeypatch.setenv("INCIPIT_OWNER_PASSWORD_FILE", str(secret))

    cli.main(["init", "--db", str(tmp_path / "x.db"), "--owner", "olive"])

    with db.connect(str(tmp_path / "x.db")) as conn:
        assert accounts.login(conn, "olive", "a-long-owner-password")


def test_ac6_recovery_resets_the_owners_password(site, tmp_path, monkeypatch):
    _, settings = site
    secret = tmp_path / "recovered"
    secret.write_text("a-recovered-password", encoding="utf-8")
    monkeypatch.setenv("INCIPIT_OWNER_PASSWORD_FILE", str(secret))

    cli.main(["create-owner", "--db", settings.db_path, "--recover"])

    with db.connect(settings.db_path) as conn:
        assert accounts.login(conn, "olive", "a-recovered-password")


# ---------- AC-7: sessions end when an account is deactivated or reset ----------


def test_ac7_a_deactivated_reviewers_open_session_stops_working(site):
    app, settings = site
    john = signed_in(app, "john")
    signed_in(app, "olive").post(f"/api/team/users/{ids(settings)['john']}/deactivate")

    assert john.get("/api/me").status_code == 401


def test_ac7_a_reset_ends_the_open_session(site):
    app, settings = site
    john = signed_in(app, "john")
    signed_in(app, "paul").post(f"/api/team/users/{ids(settings)['john']}/reset-password",
                                json={"password": "temporary-one"})

    assert john.get("/api/me").status_code == 401


def test_ac7_a_reset_account_can_only_change_its_password(site):
    app, settings = site
    signed_in(app, "paul").post(f"/api/team/users/{ids(settings)['john']}/reset-password",
                                json={"password": "temporary-one"})
    john = signed_in(app, "john", "temporary-one", raise_server_exceptions=False)

    assert john.get("/api/new/folders").json()["code"] == "must_change_password"


def test_ac7_changing_the_password_lifts_the_restriction(site):
    app, settings = site
    signed_in(app, "paul").post(f"/api/team/users/{ids(settings)['john']}/reset-password",
                                json={"password": "temporary-one"})
    signed_in(app, "john", "temporary-one").post("/api/me/password",
                                                 json={"current": "temporary-one", "new": "johns-own-one"})

    assert signed_in(app, "john", "johns-own-one").get("/api/new/folders").status_code == 200


# ---------- AC-8: who may add, promote and hand over ----------


def test_ac8_the_owner_can_transfer_ownership(site):
    app, settings = site
    signed_in(app, "olive").post("/api/team/owner/transfer", json={"user_id": ids(settings)["paul"]})
    with db.connect(settings.db_path) as conn:
        roles = {r["username"]: r["role"] for r in conn.execute("SELECT username, role FROM users")}
    assert (roles["olive"], roles["paul"]) == ("admin", "owner")


def test_ac8_the_owner_cannot_change_their_own_role(site):
    app, settings = site
    response = signed_in(app, "olive").post(f"/api/team/users/{ids(settings)['olive']}/role", json={"role": "reviewer"})
    assert response.status_code == 403


def test_ac8_the_owner_cannot_deactivate_themself(site):
    app, settings = site
    response = signed_in(app, "olive").post(f"/api/team/users/{ids(settings)['olive']}/deactivate")
    assert response.status_code == 403


def test_ac8_an_admin_cannot_deactivate_another_admin(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        accounts.create_user(conn, "ada", "ada-password", "Ada", "admin")
    response = signed_in(app, "paul").post(f"/api/team/users/{ids(settings)['ada']}/deactivate")
    assert response.status_code == 403


def test_ac8_an_account_made_for_someone_must_choose_its_own_password(site):
    app, settings = site
    signed_in(app, "paul").post("/api/team/users", json={"username": "rita", "password": "rita-password",
                                                         "role": "reviewer"})
    with db.connect(settings.db_path) as conn:
        assert accounts.get_user_by_name(conn, "rita")["must_change_password"] == 1


# ---------- AC-9: joining ----------


def make_code(settings, role="reviewer", **options):
    with db.connect(settings.db_path) as conn:
        return accounts.create_join_code(conn, accounts.owner(conn), role, **options)


def join(app, username, code=""):
    return signed_in(app, None).post("/signup", data={
        "username": username, "password": f"{username}-password", "display_name": username.title(),
        "join_code": code}, follow_redirects=False)


def test_ac9_a_valid_code_creates_an_account_with_its_role(site):
    app, settings = site
    join(app, "ada", make_code(settings, "admin"))
    with db.connect(settings.db_path) as conn:
        assert accounts.get_user_by_name(conn, "ada")["role"] == "admin"


def test_ac9_a_used_up_code_is_refused(site):
    app, settings = site
    code = make_code(settings, max_uses=1)
    join(app, "first", code)
    assert join(app, "second", code).status_code == 400


def test_ac9_an_expired_code_is_refused(site):
    app, settings = site
    code = make_code(settings)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE join_codes SET expires_at = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
    assert join(app, "late", code).status_code == 400


def test_ac9_a_revoked_code_is_refused(site):
    app, settings = site
    code = make_code(settings)
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE join_codes SET active = 0")
    assert join(app, "late", code).status_code == 400


def test_ac9_without_a_code_nobody_joins_while_self_signup_is_off(site):
    app, _ = site
    assert join(app, "walkin").status_code == 400


def test_ac9_with_self_signup_on_a_walk_in_joins_as_a_reviewer(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE site_settings SET reviewer_self_signup = 1")
    join(app, "walkin")
    with db.connect(settings.db_path) as conn:
        assert accounts.get_user_by_name(conn, "walkin")["role"] == "reviewer"


def test_ac9_only_a_codes_hash_is_stored(site):
    _, settings = site
    code = make_code(settings)
    with db.connect(settings.db_path) as conn:
        stored = [tuple(r) for r in conn.execute("SELECT * FROM join_codes")]
    assert code not in json.dumps(stored) and code.replace("-", "") not in json.dumps(stored)


# ---------- AC-10: the organisation id and the join link ----------


def test_ac10_the_site_has_an_org_id_of_the_documented_form(site):
    _, settings = site
    with db.connect(settings.db_path) as conn:
        assert re.fullmatch(r"ORG-[A-Z2-9]{4}-[A-Z2-9]{4}", site_settings.settings(conn)["org_id"])


def test_ac10_the_owner_can_regenerate_it(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        before = site_settings.settings(conn)["org_id"]
    after = signed_in(app, "olive").post("/api/site/org-id").json()["org_id"]
    assert after != before


def test_ac10_the_join_link_opens_the_create_account_form(site):
    app, _ = site
    assert 'show("signup");' in signed_in(app, None).get("/login?join=1").text


def test_ac10_the_join_link_says_joining_is_closed_when_it_is(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE site_settings SET join_enabled = 0")
    assert "Joining this site is closed" in signed_in(app, None).get("/login?join=1").text


# ---------- AC-11: the public payload ----------


def test_ac11_the_public_site_payload_carries_only_what_the_pages_draw(site):
    app, _ = site
    public = signed_in(app, None).get("/api/site").json()
    assert set(public) == {"product_name", "company_name", "labels", "join_enabled", "self_signup",
                           "appearance", "album"}


def test_ac11_the_public_site_payload_carries_no_org_id_or_custom_css(site):
    app, settings = site
    with db.connect(settings.db_path) as conn:
        org_id = site_settings.settings(conn)["org_id"]
    signed_in(app, "olive").put("/api/site/settings", json={"appearance": {"custom_css": "body{color:red}"}})
    text = signed_in(app, None).get("/api/site").text
    assert org_id not in text and "color:red" not in text


# ---------- AC-12: what an upload may be ----------


def test_ac12_a_text_file_is_not_accepted_as_a_logo(site):
    app, _ = site
    assert upload(signed_in(app, "paul"), "logo", "logo.png", b"not an image").status_code == 400


def test_ac12_a_refused_upload_writes_nothing(site):
    app, settings = site
    upload(signed_in(app, "paul"), "logo", "logo.png", b"not an image")
    bucket = Path(settings.bucket_uri[len("file://"):]) / "site"
    assert not bucket.exists() or not any(bucket.rglob("*.*"))


def test_ac12_an_oversized_file_is_refused(site):
    app, _ = site
    big = b"\x00" * (site_assets.LIMITS["favicon"] + 10)
    assert upload(signed_in(app, "paul"), "favicon", "icon.png", big).status_code == 400


def test_ac12_an_svgs_script_and_handlers_are_removed():
    clean, removed = site_assets.sanitize_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script><rect/></svg>')
    assert b"script" not in clean and b"onload" not in clean and removed


def test_ac12_an_svg_with_a_doctype_is_refused():
    with pytest.raises(site_assets.AssetError):
        site_assets.sanitize_svg(b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>')


def test_ac12_custom_css_loses_imports_and_foreign_files(site):
    app, _ = site
    saved = signed_in(app, "olive").put("/api/site/settings", json={"appearance": {
        "custom_css": "@import url(http://x.test/a.css); body{background:url(http://x.test/b.png)}"}}).json()
    assert "x.test" not in saved["appearance"]["custom_css"]


def test_ac12_an_accent_without_contrast_is_refused(site):
    app, _ = site
    response = signed_in(app, "paul").put("/api/site/settings", json={"appearance": {"accent_light": "#f4f4f4"}})
    assert response.status_code == 400


def test_ac12_an_unregistered_asset_is_not_served(site):
    app, _ = site
    assert signed_in(app, None).get("/site/assets/999").status_code == 404


def test_ac12_a_served_svg_cannot_run_anything(site):
    app, _ = site
    added = upload(signed_in(app, "paul"), "logo", "logo.svg",
                   b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="4" height="4"/></svg>',
                   "image/svg+xml").json()
    served = signed_in(app, None).get(added["url"])
    assert "sandbox" in served.headers["content-security-policy"]


# ---------- AC-13: the look reaches the pages ----------


def test_ac13_site_css_carries_the_chosen_accent_for_both_themes(site):
    app, _ = site
    signed_in(app, "paul").put("/api/site/settings", json={"appearance": {
        "accent_light": "#7a3b12", "accent_dark": "#e7a46a"}})
    css = signed_in(app, None).get("/site.css").text
    assert "--accent: #7a3b12;" in css and "--accent: #e7a46a;" in css


def test_ac13_the_sign_in_page_uses_the_sites_name_and_logo(site):
    app, _ = site
    paul = signed_in(app, "paul")
    logo = upload(paul, "logo", "logo.png", png()).json()
    paul.put("/api/site/settings", json={"product_name": "Marlowe Letters", "appearance": {"logo_asset": logo["id"]}})

    page = signed_in(app, None).get("/login").text
    assert "Marlowe Letters" in page and f'src="/site/assets/{logo["id"]}"' in page


def test_ac13_the_shell_uses_the_sites_name(site):
    app, _ = site
    signed_in(app, "paul").put("/api/site/settings", json={"product_name": "Marlowe Letters"})
    assert "<title>Marlowe Letters — Review</title>" in signed_in(app, "john").get("/").text


BACKDROP_HARNESS = r"""
const made = [];
function element(tag) {
  const el = { tagName: tag, style: {}, dataset: {}, children: [], className: "",
    classList: { toggle(name, on) { el.shown = on; } },
    appendChild(child) { el.children.push(child); made.push(child); return child; },
    replaceChildren() { el.children = []; }, setAttribute() {}, getContext: () => ({
      clearRect() {}, fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
      setTransform() {}, createRadialGradient: () => ({ addColorStop() {} }) }),
    play: () => Promise.resolve(), pause() {}, clientWidth: 800, clientHeight: 600 };
  return el;
}
let timers = 0, frames = 0;
global.window = { matchMedia: (q) => ({ matches: q.includes("reduce") ? REDUCED : false }),
  addEventListener() {}, removeEventListener() {}, devicePixelRatio: 1 };
global.document = { createElement: element, getElementById: () => null, hidden: false,
  documentElement: { getAttribute: () => "light" }, addEventListener() {}, removeEventListener() {} };
global.setInterval = () => ++timers; global.clearInterval = () => {};
global.requestAnimationFrame = () => ++frames; global.cancelAnimationFrame = () => {};
global.fetch = () => new Promise(() => {});
"""


def draw(backdrop, reduced=False, album=()):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (BACKDROP_HARNESS.replace("REDUCED", "true" if reduced else "false")
              + (STATIC / "login-backdrop.js").read_text(encoding="utf-8")
              + "\nconst host = element('div');"
              + f"\nwindow.drawBackdrop({{ appearance: {{ backdrop: {json.dumps(backdrop)} }}, album: {json.dumps(list(album))} }}, host);"
              + "\nconsole.log(JSON.stringify({ tags: host.children.map((c) => c.tagName), background: host.style.background, timers, frames }));")
    done = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


ALBUM = [{"url": "/site/assets/1", "caption": "", "credit": ""}, {"url": "/site/assets/2", "caption": "", "credit": ""}]
FULL = appearance.DEFAULTS["backdrop"]


def test_ac13_a_colour_backdrop_paints_the_colour():
    assert draw({**FULL, "kind": "colour", "colour_light": "#123456"})["background"] == "#123456"


def test_ac13_an_album_draws_one_layer_per_photograph_and_moves_on():
    shown = draw({**FULL, "kind": "album"}, album=ALBUM)
    assert (shown["tags"].count("div"), shown["timers"]) == (3, 1)


def test_ac13_a_video_backdrop_plays_a_video():
    assert draw({**FULL, "kind": "video", "video": {"asset": 4, "poster": None}})["tags"] == ["video"]


def test_ac13_an_animation_backdrop_shows_the_svg_as_an_image():
    assert draw({**FULL, "kind": "animation", "animation": {"asset": 5}})["tags"] == ["img"]


def test_ac13_a_preset_draws_on_a_canvas_and_animates():
    shown = draw({**FULL, "kind": "preset"})
    assert (shown["tags"], shown["frames"]) == (["canvas"], 1)


def test_ac13_reduced_motion_holds_the_album_still():
    assert draw({**FULL, "kind": "album"}, reduced=True, album=ALBUM)["timers"] == 0


def test_ac13_reduced_motion_draws_a_preset_once():
    assert draw({**FULL, "kind": "preset"}, reduced=True)["frames"] == 0


# ---------- AC-14: the setup wizard ----------


def test_ac14_the_wizard_lists_exactly_the_registered_steps(site):
    app, _ = site
    steps = signed_in(app, "olive").get("/api/setup").json()["steps"]
    assert [s["id"] for s in steps] == [key for key, _ in site_settings.SETUP_STEPS]


def test_ac14_setup_cannot_finish_with_a_step_left(site):
    app, _ = site
    assert signed_in(app, "olive").post("/api/setup/finish").status_code == 409


def test_ac14_setup_finishes_once_every_step_is_done(site):
    app, _ = site
    olive = signed_in(app, "olive")
    for step in ("site", "prompt", "model", "tabs", "people", "access"):
        olive.post(f"/api/setup/steps/{step}", json={"done": True})
    assert olive.post("/api/setup/finish").json()["completed_at"]


def test_ac14_a_step_done_is_remembered(site):
    app, _ = site
    signed_in(app, "olive").post("/api/setup/steps/site", json={"done": True})
    steps = {s["id"]: s["done"] for s in signed_in(app, "olive").get("/api/setup").json()["steps"]}
    assert steps["site"] is True


def test_ac14_an_owner_who_has_not_finished_is_sent_to_the_wizard(site):
    app, _ = site
    response = signed_in(app, "olive").get("/", follow_redirects=False)
    assert response.headers["location"] == "/setup"


def test_ac14_an_admin_is_not_sent_to_the_wizard(site):
    app, _ = site
    assert signed_in(app, "paul").get("/", follow_redirects=False).status_code == 200


# ---------- AC-15 ----------


def test_ac15_the_m2_smoke_script_compiles():
    compile((ROOT / "scripts" / "smoke_m2.py").read_text(encoding="utf-8"), "smoke_m2.py", "exec")



def test_a_browser_asking_for_favicon_ico_is_sent_to_the_sites_mark(site):
    """Every page links the site's favicon; the bare /favicon.ico an old browser asks for goes there too."""
    app, _ = site
    answer = signed_in(app, None).get("/favicon.ico", follow_redirects=False)
    assert (answer.status_code, answer.headers["location"]) == (302, "/favicon.svg")
