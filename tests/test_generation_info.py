import json

import pytest

from daily_brief.models import ArticleRetrieval, Candidate, DiscussionRetrieval, RetrievalFailure, SummaryGeneration
from daily_brief.output import render_public_brief_json, validate_public_brief
from daily_brief.output.public_schema import PublicBriefValidationError
from fakes import story


def candidate():
    return Candidate(
        story=story('123', 'Example'), summary='有依据的摘要。', why='推荐理由',
        summary_status='success', summary_input_mode='materials',
        summary_sources_used=['web_body', 'hn_comments'], hn_post_retrieval_status='empty',
        article_retrieval=ArticleRetrieval(status='success', method='direct', material_origin='original'),
        discussion_retrieval=DiscussionRetrieval(status='success', comments=2, chars=40),
        summary_generation=SummaryGeneration(status='success', model='google/gemini-example:free'),
    )


def payload(item=None):
    return json.loads(render_public_brief_json('2026-10-08', '2026-10-08T08:00:00+08:00', [item or candidate()], []))


def info(item):
    return payload(item)['sections']['ai']['items'][0]['generation_info']


def test_success_exports_declared_sources_and_actual_model_separately_from_acquisition():
    item = candidate()
    item.summary_sources_used = ['web_body']
    result = info(item)
    assert result['materials']['hn_comments'] == {'status': 'success', 'reason': 'none'}
    assert result['materials']['hn_post'] == {'status': 'empty', 'reason': 'none'}
    assert result['summary_sources'] == ['web_body']
    assert result['generation'] == {'status': 'success', 'model': 'google/gemini-example:free', 'reason': 'none'}
    validate_public_brief(payload(item))


def test_comment_sampling_diagnostics_are_private():
    from daily_brief.output.render import render_candidates_json

    item = candidate()
    item.discussion_retrieval = DiscussionRetrieval(
        status='success', comments=31, chars=10_000, requested_items=40,
        top_level_comments=28, reply_comments=3, stop_reason='request_limit',
    )
    audit = json.loads(render_candidates_json([item]))[0]['discussion_retrieval']
    assert audit['top_level_comments'] == 28
    assert audit['reply_comments'] == 3
    assert audit['stop_reason'] == 'request_limit'
    public = payload(item)
    assert info(item)['materials']['hn_comments'] == {'status': 'success', 'reason': 'none'}
    for field in ('top_level_comments', 'reply_comments', 'stop_reason'):
        assert field not in json.dumps(public)
    validate_public_brief(public)


def test_github_issue_transport_is_public_direct_and_private_diagnostic():
    from daily_brief.output.render import render_candidates_json

    item = candidate()
    item.article_retrieval.method = 'github_issue'
    data = payload(item)
    public = data['sections']['ai']['items'][0]
    assert 'provenance' not in public
    assert public['generation_info']['materials']['webpage']['method'] == 'direct'
    assert json.loads(render_candidates_json([item]))[0]['article_retrieval']['method'] == 'github_issue'
    validate_public_brief(data)


def test_recovered_page_retains_origin_failure_without_marking_final_fetch_failed():
    item = candidate()
    item.article_retrieval.material_origin = 'same_article'
    item.article_retrieval.origin_failure = RetrievalFailure(fallback_reason='cloudflare_challenge')
    assert info(item)['materials']['webpage'] == {
        'status': 'success', 'method': 'direct', 'origin': 'same_article', 'reason': 'cloudflare_challenge',
    }


@pytest.mark.parametrize('origin', ['original', 'same_article'])
def test_github_api_limit_uses_existing_public_reason_after_recovery(origin):
    from daily_brief.output.render import render_candidates_json

    item = candidate()
    reason = 'github_api_rate_limited'
    item.article_retrieval.method = 'jina' if origin == 'original' else 'direct'
    item.article_retrieval.material_origin = origin
    if origin == 'same_article':
        item.article_retrieval.origin_failure = RetrievalFailure(fallback_reason=reason)
    else:
        item.article_retrieval.fallback_reason = reason
    data = payload(item)
    public = data['sections']['ai']['items'][0]
    assert public['generation_info']['materials']['webpage']['reason'] == 'rate_limited'
    assert 'provenance' not in public
    assert reason not in json.dumps(data)
    assert reason in render_candidates_json([item])
    validate_public_brief(data)


@pytest.mark.parametrize('recovered', [False, True])
def test_connection_failure_has_existing_public_network_reason(recovered):
    item = candidate()
    item.article_retrieval = ArticleRetrieval(
        status='success' if recovered else 'failed',
        method='jina' if recovered else 'direct',
        fallback_reason='request_failed' if recovered else '',
        error_code='' if recovered else 'request_failed',
    )
    assert info(item)['materials']['webpage']['reason'] == 'network_error'
    validate_public_brief(payload(item))


def test_nonempty_roundup_comments_are_acquired_even_when_not_sufficient():
    item = candidate()
    item.discussion_retrieval = DiscussionRetrieval(status='insufficient', chars=40, comments=1)
    assert info(item)['materials']['hn_comments']['status'] == 'success'
    item.discussion_retrieval.chars = 0
    assert info(item)['materials']['hn_comments']['status'] == 'empty'


def test_web_failure_does_not_determine_summary_success_and_unknown_diagnostics_are_hidden():
    item = candidate()
    item.article_retrieval = ArticleRetrieval(status='failed', error_code='secret raw error', error_message='secret')
    item.summary_sources_used = ['hn_comments']
    result = info(item)
    assert result['materials']['webpage']['status'] == 'failed'
    assert result['materials']['webpage']['reason'] == 'unknown'
    assert result['generation']['status'] == 'success'
    assert 'secret' not in json.dumps(result)
    item.article_retrieval.error_code = 'empty_content'
    assert info(item)['materials']['webpage']['status'] == 'empty'


def test_missing_wayback_capture_exports_safe_archive_reason_and_preserves_private_error():
    from daily_brief.output.render import render_candidates_json

    item = candidate()
    item.article_retrieval = ArticleRetrieval(
        status='failed', method='wayback', error_code='wayback_no_capture',
        fallback_reason='datadome_challenge', error_message='private diagnostic details',
    )
    item.summary_sources_used = ['hn_comments']
    data = payload(item)
    public = data['sections']['ai']['items'][0]
    assert public['generation_info']['materials']['webpage'] == {
        'status': 'failed', 'method': 'wayback', 'origin': 'unknown', 'reason': 'archive_unavailable',
    }
    assert 'provenance' not in public
    assert public['generation_info']['generation']['status'] == 'success'
    assert 'private diagnostic details' not in json.dumps(data)
    assert 'wayback_no_capture' not in json.dumps(data)
    assert json.loads(render_candidates_json([item]))[0]['article_retrieval']['error_code'] == 'wayback_no_capture'
    validate_public_brief(data)


def test_oversized_response_exports_reason_without_affecting_comment_summary():
    from daily_brief.output.render import render_candidates_json

    item = candidate()
    item.article_retrieval = ArticleRetrieval(
        status='failed', method='direct', error_code='response_too_large',
        error_message='private diagnostic details',
    )
    item.summary_sources_used = ['hn_comments']
    data = payload(item)
    result = data['sections']['ai']['items'][0]['generation_info']
    assert result['materials']['webpage'] == {
        'status': 'failed', 'method': 'direct', 'origin': 'unknown', 'reason': 'response_too_large',
    }
    assert result['summary_sources'] == ['hn_comments']
    assert result['generation']['status'] == 'success'
    assert 'private diagnostic details' not in json.dumps(data)
    audit = json.loads(render_candidates_json([item]))[0]['article_retrieval']
    assert audit['error_code'] == 'response_too_large'
    assert audit['fallback_attempted'] is False
    validate_public_brief(data)


def test_self_post_and_unrecorded_usage_model_are_not_guessed():
    item = candidate()
    item.article_retrieval = ArticleRetrieval(status='not_needed', method='story_text')
    item.summary_sources_used = []
    item.summary_generation.model = 'bad model with secret\ntext'
    result = info(item)
    assert result['materials']['webpage']['status'] == 'not_needed'
    assert result['materials']['webpage']['method'] == 'none'
    assert result['summary_sources'] is None
    assert result['generation']['model'] is None


@pytest.mark.parametrize(('status', 'code', 'expected'), [
    ('insufficient', '', 'source_material_insufficient'),
    ('failed', 'timeout', 'network_timeout'), ('failed', 'quota_exceeded', 'rate_limited'),
    ('failed', 'http_401', 'authentication_failed'), ('failed', 'http_503', 'provider_unavailable'),
    ('failed', 'not_allowlisted', 'unknown'),
])
def test_safe_generation_reason(status, code, expected):
    item = candidate()
    item.summary_status = status
    item.summary_generation.error_code = code
    result = info(item)
    assert result['generation']['reason'] == expected
    assert result['summary_sources'] == []


@pytest.mark.parametrize('change', [
    lambda x: x.update(extra='bad'),
    lambda x: x['materials']['webpage'].update(status='invented'),
    lambda x: x['materials']['webpage'].update(method='github_issue'),
    lambda x: x['materials']['webpage'].update(origin='private origin'),
    lambda x: x['materials']['webpage'].update(reason='private error'),
    lambda x: x['materials']['hn_post'].update(error_message='bad'),
    lambda x: x.update(summary_sources=['web_body', 'web_body']),
    lambda x: x.update(summary_sources=[{}]),
    lambda x: x.update(summary_sources='web_body'),
    lambda x: x['generation'].update(model='a' * 129),
    lambda x: x['generation'].update(model='api key'),
    lambda x: x['generation'].update(model='模型'),
    lambda x: x['generation'].update(model=True),
    lambda x: x['generation'].update(reason='raw error'),
    lambda x: x['generation'].update(status='skipped'),
])
def test_schema_rejects_invalid_or_unsafe_generation_info(change):
    data = payload()
    change(data['sections']['ai']['items'][0]['generation_info'])
    with pytest.raises(PublicBriefValidationError):
        validate_public_brief(data)


@pytest.mark.parametrize('include_generation_info', [False, True])
def test_schema_accepts_optional_generation_info(include_generation_info):
    data = payload()
    item = data['sections']['ai']['items'][0]
    assert 'provenance' not in item
    if not include_generation_info:
        del item['generation_info']
    validate_public_brief(data)


@pytest.mark.parametrize(('text', 'expected'), [('', 'empty'), ('HN self-post body', 'success')])
def test_collection_records_hn_post_presence(text, expected):
    from daily_brief.generation.pipeline import _candidate
    item = _candidate(story('123', 'AI release', story_text=text))
    assert item.hn_post_retrieval_status == expected


def test_no_material_skips_generation_without_inventing_model_or_sources():
    item = candidate()
    item.summary_status = 'skipped'
    item.summary_generation = SummaryGeneration(status='skipped')
    result = info(item)
    assert result['generation'] == {'status': 'not_attempted', 'model': None, 'reason': 'no_materials'}
    assert result['summary_sources'] == []


def test_null_source_declarations_and_model_are_valid_and_preserved():
    data = payload()
    result = data['sections']['ai']['items'][0]['generation_info']
    result['summary_sources'] = None
    result['generation']['model'] = None
    validate_public_brief(data)
    assert result['summary_sources'] is None
    assert result['generation']['model'] is None


@pytest.mark.parametrize('http_status', [401, 403, 503])
def test_material_http_failures_do_not_claim_model_service_failure(http_status):
    item = candidate()
    item.article_retrieval = ArticleRetrieval(status='failed', error_code=f'http_{http_status}')
    assert info(item)['materials']['webpage']['reason'] == 'http_error'


@pytest.mark.parametrize(('http_status', 'expected'), [(401, 'authentication_failed'), (429, 'rate_limited'), (503, 'provider_unavailable')])
def test_generic_generation_http_error_uses_recorded_response_status(http_status, expected):
    item = candidate()
    item.summary_status = 'failed'
    item.summary_generation = SummaryGeneration(status='failed', error_code='http_error', http_status=http_status)
    assert info(item)['generation']['reason'] == expected


def test_overlong_actual_model_is_not_published_as_a_truncated_identifier():
    from daily_brief.generation.summaries import generate_candidate_summary
    class Backend:
        last_summary_model = 'openrouter/' + 'x' * 140
        def summarize(self, item):
            item.summary_sources_used = ['web_body']
            return '有依据的摘要。'
    item = candidate()
    generate_candidate_summary(item, Backend())
    assert item.summary_generation.model == Backend.last_summary_model
    assert info(item)['generation']['model'] is None


@pytest.mark.parametrize('effort', ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', None])
def test_reasoning_effort_additive_contract(effort):
    item = candidate()
    item.summary_generation.reasoning_effort = effort
    data = payload(item)
    generation = data['sections']['ai']['items'][0]['generation_info']['generation']
    if effort is None:
        assert 'reasoning_effort' not in generation
        generation['reasoning_effort'] = None
    else:
        assert generation['reasoning_effort'] == effort
    validate_public_brief(data)


@pytest.mark.parametrize('effort', ['auto', '', {}, True, 3])
def test_invalid_effort_is_not_exported_and_is_rejected_at_public_boundary(effort):
    item = candidate()
    item.summary_generation.reasoning_effort = effort
    data = payload(item)
    generation = data['sections']['ai']['items'][0]['generation_info']['generation']
    assert 'reasoning_effort' not in generation
    generation['reasoning_effort'] = effort
    with pytest.raises(PublicBriefValidationError):
        validate_public_brief(data)
