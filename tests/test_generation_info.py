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


def test_recovered_page_retains_origin_failure_without_marking_final_fetch_failed():
    item = candidate()
    item.article_retrieval.material_origin = 'same_article'
    item.article_retrieval.origin_failure = RetrievalFailure(fallback_reason='cloudflare_challenge')
    assert info(item)['materials']['webpage'] == {
        'status': 'success', 'method': 'direct', 'origin': 'same_article', 'reason': 'cloudflare_challenge',
    }


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


def test_additive_schema_accepts_both_optional_fields_independently():
    for omitted in ((), ('generation_info',), ('provenance',), ('generation_info', 'provenance')):
        data = payload()
        for name in omitted:
            del data['sections']['ai']['items'][0][name]
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
