#!/usr/bin/env python3
"""Generate a self-contained data browser (index.html) for the watcher's output.

Why a generator instead of a live page: opening a plain HTML file via file://
blocks fetch() of local JSON, so we can't read data/ at runtime in the browser.
Instead we embed the (small) metadata inline as JSON and reference the (large)
screenshots by relative path — <img src="data/..."> loads fine over both file://
and http://. Re-run this after a scrape to refresh:

    python3 build_browser.py            # writes ./index.html
    python3 build_browser.py --data DIR --out FILE
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def collect_posts(data_dir: Path) -> list[dict]:
    """Walk data/posts/* and build one record per post, with its capture events."""
    posts_dir = data_dir / "posts"
    posts: list[dict] = []
    if not posts_dir.is_dir():
        return posts

    for post_dir in sorted(posts_dir.iterdir()):
        if not post_dir.is_dir():
            continue
        meta = _read_json(post_dir / "post-meta.json")
        seen = _read_json(post_dir / "seen.json")

        captures: list[dict] = []
        cap_dir = post_dir / "captures"
        if cap_dir.is_dir():
            for jf in sorted(cap_dir.glob("*.json")):
                cap = _read_json(jf)
                if not cap:
                    continue
                # Resolve the screenshot to a path relative to the data dir's parent
                # (the repo root, where index.html lives) so the browser can load it.
                shot = cap.get("screenshot_path")
                rel_png = None
                if shot:
                    png = (cap_dir / Path(shot).name)
                    if png.exists():
                        rel_png = png.relative_to(data_dir.parent).as_posix()
                captures.append({
                    "comment_id": cap.get("comment_id"),
                    "author": cap.get("author_display_name"),
                    "klass": cap.get("class"),
                    "run_id": cap.get("run_id"),
                    "observed_at": cap.get("observed_at"),
                    "is_reply": bool(cap.get("is_reply")),
                    "parent_comment_id": cap.get("parent_comment_id"),
                    "timestamp_text": cap.get("timestamp_text"),
                    "edited_marker_text": cap.get("edited_marker_text"),
                    "body_text": cap.get("body_text") or "",
                    "screenshot": rel_png,
                })

        post_body = None
        if isinstance(meta.get("post"), dict):
            post_body = meta["post"].get("current_body_text")

        posts.append({
            "slug": meta.get("post_slug") or post_dir.name,
            "url": meta.get("post_url"),
            "first_seen_at": meta.get("first_seen_at"),
            "last_scrape_at": meta.get("last_scrape_at"),
            "last_scrape_result": meta.get("last_scrape_result"),
            "totals": {
                "seen": meta.get("total_seen"),
                "live": meta.get("total_currently_live"),
                "edited": meta.get("total_edited_ever"),
                "deleted": meta.get("total_deleted_ever"),
            },
            "post_body": post_body,
            "comment_count": len(seen) if isinstance(seen, dict) else 0,
            "captures": captures,
        })
    return posts


# --- HTML template ---------------------------------------------------------- #
# {DATA} is replaced with the embedded JSON. The page is pure vanilla JS so it
# works by double-clicking with no server or build step.
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nextdoor Watcher — Data Browser</title>
<style>
  :root {
    --bg: #0f1117; --panel: #181b24; --panel2: #1f2330; --line: #2a2f3e;
    --fg: #e6e8ee; --muted: #9aa3b2; --accent: #6ea8fe;
    --new: #3fb950; --edited: #d29922; --deleted: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 5; background: var(--panel);
    border-bottom: 1px solid var(--line); padding: 14px 20px;
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .grow { flex: 1; }
  select, .filterbtn {
    background: var(--panel2); color: var(--fg); border: 1px solid var(--line);
    border-radius: 8px; padding: 8px 12px; font-size: 14px; cursor: pointer;
  }
  select { min-width: 360px; max-width: 60vw; }
  .filterbtn { user-select: none; }
  .filterbtn.off { opacity: 0.4; }
  main { padding: 20px; max-width: 1100px; margin: 0 auto; }
  .postmeta {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 16px 18px; margin-bottom: 20px;
  }
  .postmeta a { color: var(--accent); text-decoration: none; word-break: break-all; }
  .postmeta a:hover { text-decoration: underline; }
  .badges { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .badge {
    font-size: 12px; padding: 3px 9px; border-radius: 999px;
    background: var(--panel2); border: 1px solid var(--line); color: var(--muted);
  }
  .postbody {
    margin-top: 12px; padding: 12px; background: var(--panel2);
    border-radius: 8px; white-space: pre-wrap; color: var(--fg); font-size: 14px;
    max-height: 220px; overflow: auto;
  }
  .group { margin-bottom: 26px; }
  .group > h3 {
    font-size: 13px; color: var(--muted); font-weight: 600; margin: 0 0 10px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 14px 16px; margin-bottom: 12px; display: grid;
    grid-template-columns: 180px 1fr; gap: 16px;
  }
  .card.reply { margin-left: 36px; border-left: 3px solid var(--accent); }
  .shot { width: 100%; border-radius: 8px; border: 1px solid var(--line);
          cursor: zoom-in; background: #fff; }
  .noshot { color: var(--muted); font-size: 12px; font-style: italic;
            display: flex; align-items: center; justify-content: center;
            border: 1px dashed var(--line); border-radius: 8px; min-height: 90px; }
  .meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
          font-size: 13px; color: var(--muted); margin-bottom: 8px; }
  .meta .author { color: var(--fg); font-weight: 600; }
  .tag { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px;
         text-transform: uppercase; letter-spacing: 0.03em; color: #0b0d12; }
  .tag.new { background: var(--new); }
  .tag.edited { background: var(--edited); }
  .tag.deleted { background: var(--deleted); }
  .body { white-space: pre-wrap; font-size: 14px; }
  .body.deleted { color: var(--muted); text-decoration: line-through; }
  .empty { color: var(--muted); padding: 40px; text-align: center; }
  /* lightbox */
  #lb { position: fixed; inset: 0; background: rgba(0,0,0,.85); display: none;
        align-items: center; justify-content: center; z-index: 20; cursor: zoom-out; }
  #lb img { max-width: 94vw; max-height: 94vh; border-radius: 8px; }
  @media (max-width: 640px) { .card { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>Nextdoor Watcher</h1>
  <select id="postsel"></select>
  <span class="grow"></span>
  <span class="filterbtn" data-k="new">new</span>
  <span class="filterbtn" data-k="edited">edited</span>
  <span class="filterbtn" data-k="deleted">deleted</span>
</header>
<main id="main"></main>
<div id="lb"><img alt=""></div>
<script>
const DATA = __DATA__;
const filters = { new: true, edited: true, deleted: true };
const sel = document.getElementById('postsel');
const main = document.getElementById('main');
const lb = document.getElementById('lb');

function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function fmt(ts){ if(!ts) return '—'; return ts.replace('T',' ').replace('Z',' UTC'); }

function postLabel(p){
  let title = '';
  if (p.post_body) title = p.post_body.split('\n')[0].slice(0, 70);
  else if (p.url) title = p.url.replace(/^https?:\/\/(www\.)?/,'');
  const t = p.totals || {};
  return `${title || p.slug}  ·  ${p.captures.length} captures` +
         (t.edited ? `, ${t.edited} edited` : '') +
         (t.deleted ? `, ${t.deleted} deleted` : '');
}

DATA.posts.forEach((p,i)=>{
  const o = document.createElement('option');
  o.value = i; o.textContent = postLabel(p);
  sel.appendChild(o);
});

function captureCard(c){
  const cls = c.klass || 'new';
  const shot = c.screenshot
    ? `<img class="shot" src="${esc(c.screenshot)}" alt="screenshot" loading="lazy">`
    : `<div class="noshot">no screenshot</div>`;
  return `<div class="card${c.is_reply?' reply':''}">
    <div>${shot}</div>
    <div>
      <div class="meta">
        <span class="tag ${cls}">${esc(cls)}</span>
        <span class="author">${esc(c.author || 'unknown')}</span>
        <span>· ${esc(fmt(c.observed_at))}</span>
        ${c.timestamp_text?`<span>· posted ${esc(c.timestamp_text)}</span>`:''}
        ${c.is_reply?'<span>· reply</span>':''}
        <span>· ${esc(c.comment_id||'')}</span>
      </div>
      <div class="body${cls==='deleted'?' deleted':''}">${esc(c.body_text) || '<em>(no text)</em>'}</div>
    </div>
  </div>`;
}

function render(){
  const p = DATA.posts[+sel.value];
  if(!p){ main.innerHTML = '<div class="empty">No posts captured yet.</div>'; return; }
  const t = p.totals || {};
  let html = `<div class="postmeta">
    ${p.url?`<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a>`:esc(p.slug)}
    <div class="badges">
      <span class="badge">slug: ${esc(p.slug)}</span>
      <span class="badge">first seen: ${esc(fmt(p.first_seen_at))}</span>
      <span class="badge">last scrape: ${esc(fmt(p.last_scrape_at))} (${esc(p.last_scrape_result||'?')})</span>
      <span class="badge">comments live: ${t.live??'—'}/${t.seen??'—'}</span>
      <span class="badge">edited ever: ${t.edited??0}</span>
      <span class="badge">deleted ever: ${t.deleted??0}</span>
    </div>
    ${p.post_body?`<div class="postbody">${esc(p.post_body)}</div>`:''}
  </div>`;

  // Group captures by comment, ordered by first observation; events within a
  // comment shown chronologically so an edit/delete reads as a history.
  const order = [];
  const byComment = new Map();
  for (const c of p.captures){
    if (!filters[c.klass || 'new']) continue;
    const k = c.comment_id || c.run_id;
    if (!byComment.has(k)){ byComment.set(k, []); order.push(k); }
    byComment.get(k).push(c);
  }

  if (!order.length){
    html += '<div class="empty">No captures match the current filters.</div>';
  } else {
    for (const k of order){
      const events = byComment.get(k).sort((a,b)=>(a.observed_at||'').localeCompare(b.observed_at||''));
      html += `<div class="group"><h3>${esc(k)} · ${events.length} capture${events.length>1?'s':''}</h3>`;
      html += events.map(captureCard).join('');
      html += `</div>`;
    }
  }
  main.innerHTML = html;
}

sel.addEventListener('change', render);
document.querySelectorAll('.filterbtn').forEach(b=>{
  b.addEventListener('click', ()=>{
    const k = b.dataset.k; filters[k] = !filters[k];
    b.classList.toggle('off', !filters[k]); render();
  });
});
main.addEventListener('click', e=>{
  if (e.target.classList.contains('shot')){
    lb.querySelector('img').src = e.target.src; lb.style.display = 'flex';
  }
});
lb.addEventListener('click', ()=>{ lb.style.display='none'; lb.querySelector('img').src=''; });

render();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the watcher data browser HTML.")
    ap.add_argument("--data", default="data", help="Path to the watcher data dir (default: data)")
    ap.add_argument("--out", default="index.html", help="Output HTML file (default: index.html)")
    args = ap.parse_args()

    data_dir = Path(args.data).resolve()
    posts = collect_posts(data_dir)
    payload = json.dumps({"posts": posts}, ensure_ascii=False)
    out = Path(args.out)
    out.write_text(HTML.replace("__DATA__", payload), encoding="utf-8")

    total_caps = sum(len(p["captures"]) for p in posts)
    print(f"Wrote {out} — {len(posts)} posts, {total_caps} captures.")


if __name__ == "__main__":
    main()
