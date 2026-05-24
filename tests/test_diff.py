"""Diff-engine tests (spec §18.1) driven by fixture HTML."""

from __future__ import annotations

from pathlib import Path

from nextdoor_watcher.diff import diff
from nextdoor_watcher.extract import extract_from_html

from .helpers import build_thread_html

RUN1 = "2026-05-21T14:30:05Z"
RUN2 = "2026-05-22T09:15:00Z"
RUN3 = "2026-05-23T11:00:00Z"


def comments_from(spec):
    return extract_from_html(build_thread_html(spec))


def test_fixture_file_parses_with_reply_nesting():
    html = (Path(__file__).parent / "fixtures" / "thread_v1.html").read_text()
    comments = extract_from_html(html)
    by_id = {c.comment_id: c for c in comments}
    assert set(by_id) == {"comment_aaa", "comment_aaa_r1", "comment_bbb"}
    assert by_id["comment_aaa"].id_source == "dom"
    assert by_id["comment_aaa"].author_display_name == "Jane D."
    assert by_id["comment_aaa_r1"].is_reply is True
    assert by_id["comment_aaa_r1"].parent_comment_id == "comment_aaa"
    assert by_id["comment_bbb"].is_reply is False


def test_first_run_seed_records_without_capturing():
    comments = comments_from([{"id": "c_aaa", "body": "hi"}, {"id": "c_bbb", "body": "yo"}])
    res = diff(comments, {}, run_id=RUN1, observed_at=RUN1, is_first_run=True,
               first_run_mode="seed")
    assert res.actions == []
    assert res.new_count == 0
    assert set(res.next_seen) == {"c_aaa", "c_bbb"}
    assert res.next_seen["c_aaa"]["current_content_hash"]
    assert res.next_seen["c_aaa"]["revisions"][0]["class"] == "seed"


def test_first_run_capture_all_captures_everything():
    comments = comments_from([{"id": "c_aaa", "body": "hi"}, {"id": "c_bbb", "body": "yo"}])
    res = diff(comments, {}, run_id=RUN1, observed_at=RUN1, is_first_run=True,
               first_run_mode="capture-all")
    assert {a.cls for a in res.actions} == {"new"}
    assert res.new_count == 2
    assert all(a.screenshot_rel and a.screenshot_rel.endswith("__new.png") for a in res.actions)


def test_second_run_no_change_no_captures():
    spec = [{"id": "c_aaa", "body": "hi"}, {"id": "c_bbb", "body": "yo"}]
    seed = diff(comments_from(spec), {}, run_id=RUN1, observed_at=RUN1,
                is_first_run=True, first_run_mode="seed")
    res = diff(comments_from(spec), seed.next_seen, run_id=RUN2, observed_at=RUN2,
               is_first_run=False)
    assert res.actions == []
    assert res.unchanged_count == 2


def test_second_run_n_new_captures():
    seed = diff(comments_from([{"id": "c_aaa", "body": "hi"}]), {}, run_id=RUN1,
                observed_at=RUN1, is_first_run=True, first_run_mode="seed")
    spec2 = [{"id": "c_aaa", "body": "hi"}, {"id": "c_new1", "body": "n1"},
             {"id": "c_new2", "body": "n2"}]
    res = diff(comments_from(spec2), seed.next_seen, run_id=RUN2, observed_at=RUN2,
               is_first_run=False)
    assert res.new_count == 2
    assert {a.comment_id for a in res.actions} == {"c_new1", "c_new2"}


def test_edited_comment_produces_one_edited_with_previous_revision():
    seed = diff(comments_from([{"id": "c_aaa", "body": "first version"}]), {},
                run_id=RUN1, observed_at=RUN1, is_first_run=True, first_run_mode="seed")
    res = diff(comments_from([{"id": "c_aaa", "body": "second version"}]),
               seed.next_seen, run_id=RUN2, observed_at=RUN2, is_first_run=False)
    assert res.edited_count == 1
    action = res.actions[0]
    assert action.cls == "edited"
    assert action.previous_body_text == "first version"
    assert action.previous_content_hash != action.comment.content_hash
    revs = res.next_seen["c_aaa"]["revisions"]
    assert [r["class"] for r in revs] == ["seed", "edited"]
    assert res.next_seen["c_aaa"]["current_body_text"] == "second version"


def test_missing_once_stays_live():
    spec = [{"id": "c_aaa", "body": "a"}, {"id": "c_bbb", "body": "b"}]
    seed = diff(comments_from(spec), {}, run_id=RUN1, observed_at=RUN1,
                is_first_run=True, first_run_mode="seed")
    res = diff(comments_from([{"id": "c_aaa", "body": "a"}]), seed.next_seen,
               run_id=RUN2, observed_at=RUN2, is_first_run=False)
    assert res.deleted_count == 0
    assert res.next_seen["c_bbb"]["status"] == "live"
    assert res.next_seen["c_bbb"]["missing_streak"] == 1


def test_missing_twice_confirms_deletion():
    spec = [{"id": "c_aaa", "body": "a"}, {"id": "c_bbb", "body": "b"}]
    seed = diff(comments_from(spec), {}, run_id=RUN1, observed_at=RUN1,
                is_first_run=True, first_run_mode="seed")
    miss1 = diff(comments_from([{"id": "c_aaa", "body": "a"}]), seed.next_seen,
                 run_id=RUN2, observed_at=RUN2, is_first_run=False)
    miss2 = diff(comments_from([{"id": "c_aaa", "body": "a"}]), miss1.next_seen,
                 run_id=RUN3, observed_at=RUN3, is_first_run=False)
    assert miss2.deleted_count == 1
    assert miss2.actions[0].cls == "deleted"
    assert miss2.actions[0].screenshot_rel is None
    entry = miss2.next_seen["c_bbb"]
    assert entry["status"] == "deleted"
    assert entry["first_missing_at"] == RUN2
    assert entry["confirmed_deleted_at"] == RUN3


def test_deleted_comment_reappears_as_new():
    spec = [{"id": "c_aaa", "body": "a"}, {"id": "c_bbb", "body": "b"}]
    seed = diff(comments_from(spec), {}, run_id=RUN1, observed_at=RUN1,
                is_first_run=True, first_run_mode="seed")
    miss1 = diff(comments_from([{"id": "c_aaa", "body": "a"}]), seed.next_seen,
                 run_id=RUN2, observed_at=RUN2, is_first_run=False)
    miss2 = diff(comments_from([{"id": "c_aaa", "body": "a"}]), miss1.next_seen,
                 run_id=RUN3, observed_at=RUN3, is_first_run=False)
    # c_bbb reappears
    res = diff(comments_from(spec), miss2.next_seen, run_id="2026-05-24T00:00:00Z",
               observed_at="2026-05-24T00:00:00Z", is_first_run=False)
    assert res.new_count == 1
    assert res.actions[0].comment_id == "c_bbb"
    assert res.next_seen["c_bbb"]["status"] == "live"


def test_replies_distinguished_from_top_level():
    comments = comments_from([
        {"id": "c_top", "body": "top", "replies": [{"id": "c_rep", "body": "reply"}]},
    ])
    by_id = {c.comment_id: c for c in comments}
    assert by_id["c_top"].is_reply is False
    assert by_id["c_rep"].is_reply is True
    assert by_id["c_rep"].parent_comment_id == "c_top"


def test_diff_does_not_mutate_input_seen():
    spec = [{"id": "c_aaa", "body": "a"}]
    seed = diff(comments_from(spec), {}, run_id=RUN1, observed_at=RUN1,
                is_first_run=True, first_run_mode="seed")
    snapshot = dict(seed.next_seen["c_aaa"])
    diff(comments_from([{"id": "c_aaa", "body": "changed"}]), seed.next_seen,
         run_id=RUN2, observed_at=RUN2, is_first_run=False)
    assert seed.next_seen["c_aaa"] == snapshot
