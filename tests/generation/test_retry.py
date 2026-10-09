import json
from dataclasses import replace

import pytest

from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import RetryError, run_retry
from daily_brief.generation import retry
from daily_brief.models import ArticleRetrieval, Candidate
from daily_brief.output import render_candidates_json, render_markdown, render_public_brief_json
from fakes import FakeSummarizer, RaisingSummarizer, story

DAY = '2026-09-29'
STAMP = '2026-09-29T08:10:00+08:00'


def seed(tmp_path, *, failed=False, roundup=False):
    selected = []
    for item_id in ('1', '2'):
        item = Candidate(
            story=story(item_id, f'AI release {item_id}', points=80, comments=20),
            summary=f'Original summary {item_id}', summary_status='success',
            summary_basis='fetched_article', why='Original recommendation',
            selected=True, section='ai', score=12.5,
            article_retrieval=ArticleRetrieval(status='success', method='direct', material_origin='original'),
        )
        selected.append(item)
    if failed:
        selected[0].summary_status = 'failed'
        selected[0].summary = 'Original failure message'
    if roundup:
        selected[0].content_kind = 'community_roundup'
        selected[0].story = replace(selected[0].story, source_url=selected[0].story.hn_discussion_url)
        selected[0].summary_basis = 'hn_comments'
        selected[0].article_retrieval = ArticleRetrieval(status='not_needed', method='story_text')
    unselected = Candidate(story=story('3', 'AI not selected'), rejection_reason='not_selected')
    briefs, data = tmp_path / 'briefs', tmp_path / 'data'
    briefs.mkdir()
    data.mkdir()
    (briefs / f'{DAY}.json').write_text(render_public_brief_json(DAY, STAMP, selected, [], ai_note='Keep this note'))
    (briefs / f'{DAY}.md').write_text(render_markdown(DAY, selected, [], ai_note='Keep this note'))
    (data / f'{DAY}-hn-candidates.json').write_text(render_candidates_json([*selected, unselected]))
    (data / 'recommendation-history.json').write_text('history sentinel')
    (data / 'publish-state.json').write_text('publish sentinel')
    return briefs, data


def inputs(calls):
    def fetch_story(item_id):
        calls.append(('post', item_id))
        return story(item_id, 'Changed live title', story_text='Fresh HN post: on-demand HTML video.',
                     url='https://different.example/live-url')

    def fetch_article(url, **kwargs):
        calls.append(('article', url, kwargs))
        return 'Fresh webpage: editable video output.'

    def fetch_comments(item_id):
        calls.append(('comments', item_id))
        text = 'Fresh comment: it also supports offline playback. ' * 12
        return HNDiscussionResult(text, 3, len(text), 4, 0)

    return dict(hn_story_fetcher=fetch_story, article_fetcher=fetch_article,
                hn_discussion_fetcher=fetch_comments)


def read_outputs(briefs, data):
    return (json.loads((briefs / f'{DAY}.json').read_text()),
            json.loads((data / f'{DAY}-hn-candidates.json').read_text()))


def test_single_retry_refetches_all_material_preserves_selection_and_does_not_save_raw(tmp_path):
    from daily_brief.llm import build_summary_context
    briefs, data = seed(tmp_path)
    before, before_audit = read_outputs(briefs, data)
    original_files = set(tmp_path.rglob('*'))
    calls = []

    class Backend:
        def summarize(self, item):
            context = build_summary_context(item)
            assert all(text in context.text for text in ('Fresh HN post', 'Fresh webpage', 'Fresh comment'))
            assert item.story.title == 'AI release 1'
            item.summary_sources_used = ['web_body', 'hn_post']
            return '根据 HN 发帖者介绍：视频按需生成，可编辑。'

    result = run_retry(briefs, data, date_label=DAY, item_ids=['1'], model_backend=Backend(), **inputs(calls))
    public, audit = read_outputs(briefs, data)
    assert (result.attempted, result.updated, result.failed) == (1, 1, 0)
    assert [call[0] for call in calls] == ['post', 'article', 'comments']
    original_item = before['sections']['ai']['items'][0]
    updated_item = public['sections']['ai']['items'][0]
    for key in ('hn_item_id', 'title', 'source_url', 'discussion_url', 'points', 'comments', 'why'):
        assert updated_item[key] == original_item[key]
    assert calls[1][1] == original_item['source_url']
    assert calls[1][2]['wayback_not_after'].isoformat() == f'{DAY}T08:00:00+08:00'
    assert public['sections']['ai']['items'][1] == before['sections']['ai']['items'][1]
    assert public['sections']['ai']['note'] == 'Keep this note'
    assert audit[1:] == before_audit[1:]
    assert audit[0]['score'] == before_audit[0]['score']
    assert audit[0]['last_retry']['status'] == 'updated'
    assert updated_item['summary'] in (briefs / f'{DAY}.md').read_text()
    assert set(tmp_path.rglob('*')) == original_files
    assert (data / 'recommendation-history.json').read_text() == 'history sentinel'
    assert (data / 'publish-state.json').read_text() == 'publish sentinel'
    saved = ''.join(p.read_text() for p in tmp_path.rglob('*') if p.is_file())
    assert 'Fresh HN post' not in saved and 'Fresh comment' not in saved and 'Fresh webpage' not in saved


def test_retry_defaults_to_failed_selected_items_only(tmp_path):
    briefs, data = seed(tmp_path, failed=True)
    calls = []
    result = run_retry(briefs, data, date_label=DAY, model_backend=FakeSummarizer(), **inputs(calls))
    assert result.updated == 1
    assert [c for c in calls if c[0] == 'post'] == [('post', '1')]


def test_default_retry_includes_failed_retrieval_with_successful_comment_summary(tmp_path):
    briefs, data = seed(tmp_path)
    path = data / f'{DAY}-hn-candidates.json'
    audit = json.loads(path.read_text())
    audit[0]['article_retrieval']['status'] = 'failed'
    path.write_text(json.dumps(audit))
    result = run_retry(briefs, data, date_label=DAY, model_backend=FakeSummarizer(), **inputs([]))
    assert result.attempted == 1


def test_no_failures_is_noop_without_model_or_network(tmp_path, monkeypatch):
    briefs, data = seed(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('No work must not construct a model or fetch sources')
    monkeypatch.setattr(retry, 'create_model_backend', forbidden)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = run_retry(briefs, data, date_label=DAY, hn_story_fetcher=forbidden)
    assert result.attempted == 0
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize('old_failed', [False, True])
def test_failed_retry_preserves_published_fields_and_updates_only_latest_diagnostics(tmp_path, old_failed):
    briefs, data = seed(tmp_path, failed=old_failed)
    old_public = (briefs / f'{DAY}.json').read_bytes()
    old_markdown = (briefs / f'{DAY}.md').read_bytes()
    _, old_audit = read_outputs(briefs, data)
    for _ in range(2):
        result = run_retry(briefs, data, date_label=DAY, item_ids=['1'],
                           model_backend=RaisingSummarizer(), **inputs([]))
        assert result.failed == 1 and result.updated == 0
    _, audit = read_outputs(briefs, data)
    assert (briefs / f'{DAY}.json').read_bytes() == old_public
    assert (briefs / f'{DAY}.md').read_bytes() == old_markdown
    assert audit[0]['last_retry']['summary_generation']['error_code'] == 'quota_exceeded'
    assert audit[0]['last_retry']['status'] == 'failed'
    assert {k: v for k, v in audit[0].items() if k != 'last_retry'} == old_audit[0]
    assert audit[1:] == old_audit[1:]


def test_hn_post_failure_does_not_block_web_and_comments(tmp_path):
    briefs, data = seed(tmp_path)
    kwargs = inputs([])
    def fail_post(item_id):
        raise RuntimeError('HN temporarily unavailable')
    kwargs['hn_story_fetcher'] = fail_post
    result = run_retry(briefs, data, date_label=DAY, item_ids=['1'], model_backend=FakeSummarizer(), **kwargs)
    public, audit = read_outputs(briefs, data)
    assert result.updated == 1
    assert audit[0]['last_retry']['hn_post_retrieval']['status'] == 'failed'
    assert audit[0]['last_retry']['hn_post_retrieval']['error_message'] == 'HN temporarily unavailable'
    assert audit[0]['hn_post_retrieval']['status'] == 'failed'
    assert public['sections']['ai']['items'][0]['generation_info']['materials']['hn_post']['status'] == 'failed'
    assert public['sections']['ai']['items'][0]['generation_info']['generation']['status'] == 'success'


def test_roundup_uses_fresh_comment_summary_without_reclassification(tmp_path):
    briefs, data = seed(tmp_path, roundup=True)
    calls = []
    class RoundupBackend:
        def summarize(self, item):
            assert item.content_kind == 'community_roundup'
            assert item.discussion_text.startswith('Fresh comment')
            return '社区分享两个工具。\n- Alpha：离线编辑器。\n- Beta：静态检查器。'
    result = run_retry(briefs, data, date_label=DAY, item_ids=['1'],
                       model_backend=RoundupBackend(), **inputs(calls))
    public, _ = read_outputs(briefs, data)
    assert result.updated == 1
    assert [c[0] for c in calls] == ['post', 'comments']
    assert public['sections']['ai']['items'][0]['summary'].startswith('社区分享两个工具。')
    assert '\n  - Alpha' in (briefs / f'{DAY}.md').read_text()


@pytest.mark.parametrize('mode', ['nonselected', 'missing_audit', 'wrong_date', 'mismatch', 'extra_selected'])
def test_invalid_saved_inputs_are_rejected_before_network(tmp_path, mode):
    briefs, data = seed(tmp_path)
    item_ids = ['1']
    public_path = briefs / f'{DAY}.json'
    audit_path = data / f'{DAY}-hn-candidates.json'
    public, audit = read_outputs(briefs, data)
    if mode == 'nonselected':
        item_ids = ['3']
    elif mode == 'missing_audit':
        audit_path.unlink()
    elif mode == 'wrong_date':
        public['date'] = '2026-09-28'
        public_path.write_text(json.dumps(public))
    elif mode == 'mismatch':
        audit[0]['source_url'] = 'https://wrong.example'
        audit_path.write_text(json.dumps(audit))
    else:
        audit[2]['selected'] = True
        audit_path.write_text(json.dumps(audit))
    calls = []
    with pytest.raises(RetryError):
        run_retry(briefs, data, date_label=DAY, item_ids=item_ids, model_backend=FakeSummarizer(), **inputs(calls))
    assert calls == []


def test_concurrent_brief_edit_is_not_overwritten(tmp_path):
    briefs, data = seed(tmp_path)
    public_path = briefs / f'{DAY}.json'
    original_audit = (data / f'{DAY}-hn-candidates.json').read_bytes()
    class EditingBackend(FakeSummarizer):
        def summarize(self, candidate):
            public_path.write_text(public_path.read_text() + '\n')
            return super().summarize(candidate)
    with pytest.raises(RetryError, match='changed during retry'):
        run_retry(briefs, data, date_label=DAY, item_ids=['1'], model_backend=EditingBackend(), **inputs([]))
    assert public_path.read_text().endswith('\n\n')
    assert (data / f'{DAY}-hn-candidates.json').read_bytes() == original_audit


def test_persistence_failure_rolls_back_already_written_artifacts(tmp_path, monkeypatch):
    briefs, data = seed(tmp_path)
    originals = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    real_write = retry.atomic_write_text
    def fail_public(path, text):
        if path == briefs / f'{DAY}.json':
            raise OSError('disk full')
        real_write(path, text)
    monkeypatch.setattr(retry, 'atomic_write_text', fail_public)
    with pytest.raises(RetryError, match='could not save'):
        run_retry(briefs, data, date_label=DAY, item_ids=['1'], model_backend=FakeSummarizer(), **inputs([]))
    assert all(p.read_bytes() == text for p, text in originals.items())
