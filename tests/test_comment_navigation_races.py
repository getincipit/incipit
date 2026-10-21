"""Late comment requests must not replace another document's thread or typed draft."""
from pathlib import Path
import shutil
import subprocess

import pytest


def run(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_document_comment_requests_cannot_arrive_out_of_order():
    source = (Path(__file__).parents[1] / "app/static/workspace.js").read_text(encoding="utf-8")
    start = source.index("  async function showComments(")
    function = source[start:source.index("  /* One comment thread", start)]
    run(r'''
      const assert=require('node:assert/strict');
      let commentsFor=null;
      const nodes={}; const $=id=>nodes[id] || (nodes[id]={innerHTML:'',classList:{add(){},toggle(){}}});
      const panel={isConnected:true,classList:{remove(){},add(){throw Error('stale failure hid panel');}}};
      const ensureCommentPanel=()=>panel,ensureCommentButton=()=>{};
      const pending=[]; const fetch=()=>new Promise(resolve=>pending.push(resolve));
      const commentListHTML=comments=>comments[0].body;
    ''' + function + r'''
      (async()=>{
        const a=showComments('A','new'), b=showComments('B','new');
        pending[1]({ok:true,json:async()=>({comments:[{body:'B',resolved:false}]})});await b;
        pending[0]({ok:true,json:async()=>({comments:[{body:'A',resolved:false}]})});await a;
        assert.equal(nodes['doc-comments-list'].innerHTML,'B');
      })().catch(error=>{console.error(error);process.exitCode=1;});
    ''')
