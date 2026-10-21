"""Regression coverage for queue races and work that grows with the archive."""
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from incipit import db

STATIC = Path(__file__).parents[1] / 'app/static'


def js_function(name, file='app.js'):
    source = (STATIC / file).read_text(encoding='utf-8')
    start = source.index(f'  async function {name}(') if f'  async function {name}(' in source else source.index(f'  function {name}(')
    return source[start:source.index('\n  }', start) + 4]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_pending_count_preserves_scope_with_one_query_for_many_documents():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE new_documents (folder_id TEXT, status TEXT)')
    conn.executemany('INSERT INTO new_documents VALUES (?, ?)',
                     [('mine', 'split_confirmed')] * 1000 + [('mine', 'error'), ('mine', 'drafted'), ('other', 'error')])
    queries = []
    conn.set_trace_callback(queries.append)
    assert db.count_new_pending_documents(conn, {'mine'}) == 1001
    assert len(queries) == 1
    assert db.count_new_pending_documents(conn, set()) == 0
    assert db.count_new_pending_documents(conn) == 1002
    conn.close()


def test_queue_ignores_late_responses_and_preserves_data_on_http_errors():
    run_js(r'''
const assert = require('node:assert/strict');
const state = {}; const pending = []; const errors = [];
const fetch = () => new Promise(resolve => pending.push(resolve));
const apiBase = () => '/api/new', tabQuery = () => '';
const renderQueue = () => {}, loadPendingBatches = () => {}, refreshPendingCounts = () => {};
const toast = message => errors.push(message);
const response = value => ({ok:true, json:async()=>value});
''' + js_function('loadQueue') + r'''
(async()=>{
  const first = loadQueue(), second = loadQueue();
  pending[2](response(['new doc'])); pending[3](response(['new folder'])); await second;
  pending[0](response(['old doc'])); pending[1](response(['old folder'])); await first;
  assert.deepEqual(state.documents, ['new doc']);
  assert.deepEqual(state.folders, ['new folder']);
  const failed = loadQueue();
  pending[4]({ok:false,status:500}); pending[5](response([])); await failed;
  assert.deepEqual(state.documents, ['new doc']);
  assert.equal(errors.length, 1);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_returning_to_queue_cancels_a_pending_document_open():
    run_js(r'''
const assert = require('node:assert/strict');
const state = {}; let resolve;
const fetch = () => new Promise(done => resolve = done);
const apiBase = () => '/api/new';
const clearRedrafted = () => {}, syncAnnouncements = () => {}, updateBackButton = () => {};
const loadQueue = async () => {};
const view = () => ({classList:{add(){},remove(){}}});
const detailView=view(), splitReviewView=view(), queueView=view();
const renderDetail = () => { throw Error('a cancelled document was rendered'); };
''' + js_function('openDocument') + js_function('showQueue') + r'''
(async()=>{
  const opening = openDocument('A');
  await showQueue();
  resolve({ok:true,json:async()=>({document_id:'A'})}); await opening;
  assert.equal(state.currentDoc, null);
  assert.equal(state.openingId, null);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


def test_split_order_inserts_new_boundaries_without_quadratic_array_searches():
    run_js(r'''
const assert = require('node:assert/strict');
let scanned = [0,1,2,3,4].map(firstSeq=>({firstSeq}));
const scanSplitGroups = () => scanned;
let splitState = {groupOrder:[4,0,2]};
''' + js_function('splitGroups') + r'''
assert.deepEqual(splitGroups().map(g=>g.firstSeq), [4,0,1,2,3]);
scanned = Array.from({length:20000}, (_,firstSeq)=>({firstSeq}));
splitState = {groupOrder:[]};
const originals = ['includes','indexOf','findIndex','splice'].map(key=>[key,Array.prototype[key]]);
try {
  for (const [key] of originals) Array.prototype[key] = () => {throw Error('linear array search in ordering');};
  const ordered = splitGroups();
  assert.equal(ordered.length, 20000);
  assert.equal(ordered[19999].firstSeq, 19999);
} finally { for (const [key,fn] of originals) Array.prototype[key] = fn; }
''')


def test_stats_modal_releases_keyboard_listener_on_close_and_replacement():
    run_js(r'''
const assert = require('node:assert/strict');
let dismissReviewerStats = null;
const contributorStats = new Map(); const esc = x => x;
const listeners = new Set(); let button;
const document = {
  createElement:()=>({querySelectorAll:()=>[],addEventListener(){},remove(){}}),
  body:{appendChild(){}},
  addEventListener:(_,fn)=>listeners.add(fn), removeEventListener:(_,fn)=>listeners.delete(fn)
};
const $ = () => ({addEventListener:(_,fn)=>button=fn});
''' + js_function('showReviewerStats', 'workspace.js') + r'''
showReviewerStats('A'); assert.equal(listeners.size,1);
showReviewerStats('B'); assert.equal(listeners.size,1);
button(); assert.equal(listeners.size,0);
showReviewerStats('C'); [...listeners][0]({key:'Escape'}); assert.equal(listeners.size,0);
''')


def test_pending_uploads_do_not_land_in_a_different_tab():
    run_js(r'''
const assert = require('node:assert/strict');
const state = {activeWorkflow:'new',activeTab:{id:1}};
let resolve; const fetch = () => new Promise(done=>resolve=done);
const pendingBatches = {classList:{add(){},remove(){throw Error('stale uploads shown');}}};
const showFolderGrid=()=>true, tabQuery=()=>''; const activityRunning=false;
const splitReviewView={classList:{contains:()=>true}};
''' + js_function('loadPendingBatches') + r'''
(async()=>{
  const loading=loadPendingBatches(); state.activeTab={id:2};
  resolve({ok:true,json:async()=>[{batch_id:'old'}]}); await loading;
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
