"""Host deliverables replace browser downloads; all three workflows use one completion gate."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from incipit import accounts, collab, db, export_archive, folder_exports, review_server
from incipit.config import Settings
from incipit.storage import BucketStorage
from conftest import insert_rows


@pytest.fixture(params=["new"])
def completed(tmp_path, request, monkeypatch):
    workflow = request.param
    folder = "E2002"
    document = f"{folder}AAA"
    settings = Settings(db_path=str(tmp_path / "extraction.db"), bucket_uri=(tmp_path / "bucket").as_uri())
    db.init_db(settings.db_path)
    with db.connect(settings.db_path) as conn:
        accounts.init_accounts(conn)
        collab.init_collab(conn)
        insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                          "VALUES ('B', 'now', 1, ?)", (folder,))
        insert_rows(conn,"INSERT INTO new_documents (document_id,batch_id,status,text,reviewed_text,review_status) "
                     "VALUES (?, 'B', 'drafted','original','latest correction','accepted')", (document,))
        conn.execute("INSERT INTO new_document_pages (document_id,sequence_index,bucket_key) VALUES (?,0,'image.jpg')", (document,))
        conn.execute("INSERT INTO folder_assignments (folder_id,workflow,status,assigned_at,updated_at) "
                     "VALUES (?,?,'approved','now','now')", (folder, workflow))
        db.set_document_review(conn, document, workflow, "reviewer", "accepted", "Reviewer")
    BucketStorage(settings.bucket_uri).write_bytes("image.jpg", b"image content")
    return settings, workflow, folder, document


def wait(jobs, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = jobs.status(job_id)
        if status["state"] not in ("queued", "running"):
            return status
        time.sleep(0.01)
    pytest.fail("export did not finish")


TABLES = {"new": "new_documents"}


def test_a_reviewers_missing_accept_does_not_hold_back_an_export(completed, tmp_path):
    """From 5 October: only the admin needs to have reviewed every document."""
    settings, workflow, folder, document = completed
    with db.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM document_reviews WHERE document_id=?", (document,))
        conn.execute("DELETE FROM folder_assignments WHERE folder_id=? AND workflow=?", (folder, workflow))
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    assert wait(jobs, jobs.start(workflow, folder, "Admin"))["state"] == "completed"


def test_a_document_the_admin_has_not_reviewed_holds_back_the_export(completed, tmp_path):
    settings, workflow, folder, document = completed
    with db.connect(settings.db_path) as conn:
        conn.execute(f"UPDATE {TABLES[workflow]} SET review_status='unreviewed' WHERE document_id=?", (document,))
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    with pytest.raises(collab.NotReady):
        jobs.start(workflow, folder, "Admin")


def test_the_prompt_follows_only_the_review_that_completed_the_folder(completed):
    settings, workflow, folder, document = completed
    with db.connect(settings.db_path) as conn:
        assert folder_exports.completion(conn, workflow, document, False) == {"workflow": workflow, "folder_id": folder}


def test_a_folder_that_was_already_complete_does_not_prompt_again(completed):
    settings, workflow, folder, document = completed
    with db.connect(settings.db_path) as conn:
        assert folder_exports.completion(conn, workflow, document, True) is None


def test_concurrent_change_cannot_publish_or_mark_completed(completed, tmp_path, monkeypatch):
    settings, workflow, folder, _ = completed
    original = folder_exports.build
    def changed(*args):
        result = original(*args)
        # An admin takes back a verdict while the export is being built.
        with db.connect(settings.db_path) as conn:
            conn.execute(f"UPDATE {TABLES[workflow]} SET review_status='unreviewed'")
        return result
    monkeypatch.setattr(folder_exports, "build", changed)
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    assert wait(jobs, jobs.start(workflow, folder, "Admin"))["state"] == "failed"
    assert not list((tmp_path / "exports").rglob("*.zip"))
    with db.connect(settings.db_path) as conn:
        assert not db.get_folder_exports(conn, workflow)


def test_entire_deliverable_replicates_and_reexport_removes_stale_files(completed, tmp_path):
    settings, workflow, folder, _ = completed
    root = tmp_path / "exports"
    jobs = folder_exports.Jobs(settings, root)
    result = wait(jobs, jobs.start(workflow, folder, "Admin"))
    assert result["state"] == "completed", result
    directory = next((root / "documents").iterdir())
    (directory / "obsolete.json").write_text("stale")
    result = wait(jobs, jobs.start(workflow, folder, "Admin"))
    assert result["state"] == "completed", result
    assert not (directory / "obsolete.json").exists()
    for destination in (tmp_path / "backup" / "exports", tmp_path / "mirror" / "exports"):
        export_archive.sync_tree(root, destination)
        left = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
                and not any(part.startswith(".") for part in p.relative_to(root).parts)}
        right = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
        assert left == right


def test_api_returns_job_not_attachment_and_reviewers_cannot_export(completed, tmp_path, monkeypatch):
    settings, workflow, folder, _ = completed
    with db.connect(settings.db_path) as conn:
        accounts.create_user(conn, "admin", "admin-password", "Admin", "admin")
        accounts.create_user(conn, "reviewer", "reviewer-password", "Reviewer", "reviewer")
    app = review_server.create_app(settings)
    admin, reviewer = TestClient(app), TestClient(app)
    for client, username in ((admin, "admin"), (reviewer, "reviewer")):
        assert client.post("/login", data={"username": username, "password": f"{username}-password"}, follow_redirects=False).status_code == 303
    path = f"/api/folder-exports/{workflow}/{folder}"
    assert reviewer.post(path).status_code == 403
    response = admin.post(path)
    assert response.status_code == 202 and "content-disposition" not in response.headers
    assert "job_id" in response.json()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = admin.get(f"/api/folder-exports/jobs/{response.json()['job_id']}").json()
        if result["state"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    assert result["state"] == "completed", result
    assert admin.post("/api/export").status_code in (404, 405)
    assert admin.post("/api/new/export").status_code in (404, 405)


def test_backup_refuses_overlapping_paths(tmp_path):
    with pytest.raises(ValueError, match="non-overlapping"):
        export_archive.sync_tree(tmp_path, tmp_path / "backup")


def test_snapshot_fingerprint_reuses_records_without_mutating_them(completed):
    settings, workflow, folder, _ = completed
    with db.connect(settings.db_path) as conn:
        items = folder_exports.records(conn, workflow, folder)
        before = json.dumps(items, sort_keys=True)
        assert folder_exports.fingerprint(conn, workflow, folder, items) == folder_exports.fingerprint(conn, workflow, folder)
        assert json.dumps(items, sort_keys=True) == before


def test_new_export_range_does_not_include_a_similarly_named_folder(completed):
    settings, workflow, folder, document = completed
    if workflow != "new":
        return
    with db.connect(settings.db_path) as conn:
        insert_rows(conn, "INSERT INTO new_batches (batch_id, uploaded_at, image_count, folder_id) "
                          "VALUES ('B1', 'now', 1, ?)", (folder + "1",))
        insert_rows(conn,"INSERT INTO new_documents (document_id,batch_id,status) VALUES (?, 'B1', 'drafted')",
                    (folder + "1AAA",))
        assert [item["document"]["document_id"] for item in folder_exports.records(conn, workflow, folder)] == [document]


def test_job_replicates_to_both_configured_backups(completed, tmp_path):
    settings, workflow, folder, _ = completed
    root = tmp_path / "exports"
    jobs = folder_exports.Jobs(settings, root, tmp_path / "backup", tmp_path / "mirror")
    assert wait(jobs, jobs.start(workflow, folder, "Admin"))["state"] == "completed"
    expected = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for destination in (tmp_path / "backup/exports", tmp_path / "mirror/exports"):
        assert {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()} == expected


def test_legacy_migration_preserves_both_trees_without_overwriting(tmp_path):
    old = tmp_path / "export/new/E2002/old.zip"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old generation")
    root = tmp_path / "exports"
    export_archive.migrate_legacy(root)
    assert (root / "new/E2002/old.zip").read_bytes() == b"old generation"
    old.write_bytes(b"old changed after migration")
    export_archive.migrate_legacy(root)
    assert (root / "new/E2002/old.zip").read_bytes() == b"old generation"
    assert old.read_bytes() == b"old changed after migration"


def test_disconnected_backup_warns_without_losing_host_export(completed, tmp_path):
    settings, workflow, folder, _ = completed
    blocked = tmp_path / "offline"
    blocked.write_text("not a directory")
    jobs = folder_exports.Jobs(settings, tmp_path / "exports", blocked, tmp_path / "mirror")
    result = wait(jobs, jobs.start(workflow, folder, "Admin"))
    assert result["state"] == "completed" and "unavailable" in result["detail"]
    assert list((tmp_path / "exports").rglob("*.zip"))
    assert list((tmp_path / "mirror").rglob("*.zip"))


def test_same_folder_clicks_return_one_pending_job(completed, tmp_path, monkeypatch):
    import threading
    settings, workflow, folder, _ = completed
    entered, release = threading.Event(), threading.Event()
    original = folder_exports.build
    def delayed(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(folder_exports, "build", delayed)
    jobs = folder_exports.Jobs(settings, tmp_path / "exports")
    job = jobs.start(workflow, folder, "Admin")
    try:
        assert entered.wait(10)
        assert jobs.start(workflow, folder, "Admin") == job
    finally:
        release.set()
    assert wait(jobs, job)["state"] == "completed"


def test_completion_ui_not_yet_exports_nothing_and_export_never_downloads():
    from pathlib import Path
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = (Path(__file__).parents[1] / "app/static/folder_exports.js").read_text(encoding="utf-8")
    harness = r'''
    const assert = require('node:assert/strict');
    class Element {
      constructor() {this.children=[]; this.parts={}; this.events={}; this.classList={contains:()=>false,remove(){},toggle(){}};}
      appendChild(c){this.children.push(c);c.parent=this;return c;}
      setAttribute(){}
      querySelector(s){return this.parts[s] ??= new Element();}
      querySelectorAll(){return [this.querySelector('[data-later]'),this.querySelector('[data-export]')];}
      addEventListener(k,f){this.events[k]=f;}
      remove(){this.parent.children=this.parent.children.filter(c=>c!==this);}
      focus(){document.activeElement=this;}
    }
    const document={body:new Element(),createElement:()=>new Element(),activeElement:null};
    let requests=[], events=[];
    const window={dispatchEvent:e=>events.push(e.type)};
    class CustomEvent {constructor(type,options){this.type=type;this.detail=options.detail;}}
    let setTimeout=(f,ms)=>{if(ms===1500)queueMicrotask(f);return 1},clearTimeout=()=>{};
    const fetch=async (url,options)=>{requests.push(url);return {ok:true,json:async()=>url.includes('/jobs/')?
      {state:'completed',detail:'Saved on host'}:{job_id:'job'}}};
    ''' + source + r'''
    (async()=>{
      const folder={workflow:'new',folder_id:'E2002'};
      window.__folderExports__.prompt(folder);
      let dialog=document.body.children[1];
      assert.ok(dialog.innerHTML.includes('Every document in this folder has now been reviewed.'));
      assert.ok(!dialog.innerHTML.includes('both Reviewer and Admin'));
      assert.ok(dialog.innerHTML.includes('Export now?'));
      assert.ok(dialog.innerHTML.includes('class="btn btn-accept" data-export'));
      assert.ok(dialog.innerHTML.includes('class="btn btn-ghost" data-later'));
      assert.ok(!dialog.innerHTML.includes('btn-primary'),'Use the shared modal button styles');
      dialog.querySelector('[data-later]').onclick();
      assert.deepEqual(requests,[]);
      assert.equal(document.body.children.length,1);
      window.__folderExports__.prompt(folder);
      dialog=document.body.children[1];
      dialog.querySelector('[data-export]').onclick();
      for(let i=0;i<20;i++)await Promise.resolve();
      assert.equal(requests[0],'/api/folder-exports/new/E2002');
      assert.deepEqual(events,['folder-export-completed']);
      assert.equal(document.body.children.length,1);
      document.body.classList.contains=()=>true;
      window.__folderExports__.prompt(folder);
      assert.equal(document.body.children.length,1);
    })().catch(e=>{console.error(e);process.exitCode=1});
    '''
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "createObjectURL" not in source and ".download" not in source


def test_export_success_fades_and_old_timers_never_hide_new_progress_or_errors():
    from pathlib import Path
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    source = (Path(__file__).parents[1] / "app/static/folder_exports.js").read_text(encoding="utf-8")
    harness = r'''
    const assert = require('node:assert/strict');
    class Element {
      constructor() {
        this.classes = new Set(['hidden']); this.textContent = '';
        this.classList = {add:(...xs)=>xs.forEach(x=>this.classes.add(x)),
          remove:(...xs)=>xs.forEach(x=>this.classes.delete(x)),
          toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x),
          contains:x=>this.classes.has(x)};
      }
      appendChild(c){this.child=c;}
      setAttribute(){}
    }
    const document={body:new Element(),createElement:()=>new Element()};
    const window={dispatchEvent:()=>{}};
    class CustomEvent {}
    let nextTimer=0, timers=new Map(), mode='completed';
    const setTimeout=(f,ms)=>{const id=++nextTimer;if(ms===1500)queueMicrotask(f);else timers.set(id,{f,ms});return id;};
    const clearTimeout=id=>timers.delete(id);
    const fire=ms=>{for(const [id,t] of [...timers])if(t.ms===ms){timers.delete(id);t.f();}};
    const fetch=async url=>({ok:true,json:async()=>{
      if(!url.includes('/jobs/'))return {job_id:'job'};
      if(mode==='pending')return new Promise(()=>{});
      return {state:mode,detail:'Saved on host'};
    }});
    ''' + source + r'''
    (async()=>{
      const status=document.body.child;
      const folder={workflow:'new',folder_id:'E2002'};
      await window.__folderExports__.start(folder);
      assert.ok(status.textContent.startsWith('Export completed.'));
      assert.ok(!status.classList.contains('hidden'));
      fire(5000);
      assert.ok(status.classList.contains('is-fading'));
      fire(250);
      assert.ok(status.classList.contains('hidden'));
      assert.equal(status.textContent,'');
      await window.__folderExports__.start(folder);
      fire(5000); // An earlier success is already fading when an error replaces it.
      mode='failed';
      await window.__folderExports__.start(folder);
      fire(250); fire(5000);
      assert.ok(status.classList.contains('is-error'));
      assert.ok(!status.classList.contains('hidden')&&!status.classList.contains('is-fading'));
      assert.ok(status.textContent.startsWith('Export could not be completed:'));
      mode='completed';
      await window.__folderExports__.start(folder);
      mode='pending';
      const progress=window.__folderExports__.start(folder);
      for(let i=0;i<10;i++)await Promise.resolve();
      fire(5000); fire(250);
      assert.ok(status.textContent.startsWith('Exporting E2002'));
      assert.ok(!status.classList.contains('hidden')&&!status.classList.contains('is-fading'));
    })().catch(e=>{console.error(e);process.exitCode=1});
    '''
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


