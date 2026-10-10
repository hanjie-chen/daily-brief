"""Exercise the real provider adapter through routing, selection and rendering offline."""
import io
import json

from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import run_generate
from daily_brief.llm.openrouter_backend import OpenRouterBackend
from fakes import story


def test_openrouter_roundup_is_classified_from_comments_and_reused(tmp_path):
    comments = ('One developer recommends an offline calendar with local event search. '
                'Another developer describes a backup tool with encrypted snapshots. ') * 8
    calls = []

    def opener(request, timeout):
        body = json.loads(request.data)
        prompt = body['messages'][-1]['content']
        schema = body['response_format']['json_schema']['schema']
        if body['model'] == 'qwen/qwen3.8-flash':
            is_comment_classification = '"hn_comments"' in prompt
            label = 'core_non_ai' if is_comment_classification else 'community_roundup'
            calls.append(label)
            result = {'decisions': [{'id': '49686380', 'label': label}]}
        elif 'entries' in schema['properties']:
            calls.append('roundup_summary')
            assert 'comments (sole substantive evidence)' in prompt
            result = {'status': 'sufficient', 'introduction': '两个实用的离线工具。', 'reason': '',
                      'entries': [{'name': '日历', 'description': '可在本地搜索日程。'},
                                  {'name': '备份工具', 'description': '支持加密快照。'}]}
        else:
            calls.append('article_summary')
            result = {'status': 'sufficient', 'summary': '数据库支持离线搜索。',
                      'summary_sources': ['web_body'], 'reason': ''}
        payload = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
                   'usage': {'cost': .0001, 'prompt_tokens': 50, 'completion_tokens': 20, 'total_tokens': 70}}
        return io.BytesIO(json.dumps(payload).encode())

    backend = OpenRouterBackend(api_key='test', opener=opener, min_request_interval_seconds=0)
    result = run_generate(
        output_dir=tmp_path/'briefs', data_dir=tmp_path/'data', date_label='2026-09-16',
        algolia_stories=[story('49686380', 'Ask HN: What are you working on?',
                              url='https://news.ycombinator.com/item?id=49686380',
                              story_text='What practical tools do you recommend?', points=362, comments=1125),
                         story('1', 'SQLite update', points=100, comments=10)],
        hot_stories=[], model_backend=backend,
        article_fetcher=lambda url, **kwargs: 'SQLite now supports offline search.',
        hn_discussion_fetcher=lambda item_id: HNDiscussionResult(text=comments, comments=3,
            chars=len(comments), requested_items=4, failed_items=0),
        capture_model_inputs=True,
    )
    audit = {r['hn_item_id']: r for r in json.loads(result.data_path.read_text())}
    assert calls[:3] == ['community_roundup', 'core_non_ai', 'roundup_summary']
    assert calls.count('roundup_summary') == 1
    assert audit['49686380']['content_kind'] == 'community_roundup'
    assert audit['49686380']['summary_sources_used'] == ['hn_comments']
    assert audit['49686380']['selected'] is True
    assert audit['49686380']['summary_generation']['provider'] == 'openrouter'
    assert audit['49686380']['summary_generation']['model'] == 'openai/gpt-6-luna'
    assert audit['49686380']['summary_generation']['reasoning_effort'] == 'medium'
    public = json.loads((tmp_path/'briefs'/'2026-09-16.json').read_text())
    assert all(item['generation_info']['generation']['reasoning_effort'] == 'medium'
               for item in public['sections']['ai']['items'])
    assert '\n  - 日历：' in result.brief_path.read_text()
    assert 'test' not in json.dumps(backend.request_records)
