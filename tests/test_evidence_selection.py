import pytest

from daily_brief.evidence_selection import select_evidence


def test_short_announcement_preserved_without_title_based_expansion():
    text = 'Without LOCAL.md, the tool now reads SHARED.md. Cloud editions are excluded.'
    result = select_evidence(text, title='Tool supports SHARED.md everywhere')
    assert result.text == text
    assert result.strategy == 'full_text'
    assert result.source_chars == result.selected_chars == len(text)


@pytest.mark.parametrize('position', ['head', 'middle', 'tail'])
def test_finds_news_beyond_old_limit_and_keeps_conditions(position):
    background = ('Older release\nRoutine maintenance for the editor and interface.\n' * 5000)
    relevant = ('\n2.1.277\nSeptember 18, 2026\n'
                'Added AGENTS.md support: in a project with no CLAUDE.md, read AGENTS.md instead. '
                'Not yet available on CloudOne or CloudTwo.\n')
    body = {'head': relevant + background,
            'middle': background + relevant + background,
            'tail': background + relevant}[position]
    result = select_evidence(body, title='Editor reads AGENTS.md if no CLAUDE.md', max_chars=6000)
    assert len(body.encode()) > 256 * 1024
    assert relevant.strip() in result.text
    assert len(result.text) <= 6000
    assert result.source_chars == len(body)
    assert result.strategy == 'relevant_excerpts'
    assert 'omissions exist' in result.text
    for section in result.sections:
        _, start, end = section.split(':')
        assert body[int(start):int(end)] in result.text


def test_huge_single_line_is_bounded_and_tail_fact_is_found():
    body = 'unrelated maintenance ' * 20000 + 'QuasarDB supports transactional writes, except on replicas.'
    result = select_evidence(body, title='QuasarDB adds transactional writes')
    assert 'QuasarDB supports transactional writes, except on replicas.' in result.text
    assert len(result.text) <= 24000


def test_unmatched_title_samples_full_extent_without_claiming_relevance():
    body = 'BEGIN ' + 'unrelated material\n' * 5000 + ' END'
    result = select_evidence(body, title='量子纠错实验', max_chars=6000)
    assert result.strategy == 'sampled_excerpts'
    assert 'not a relevance decision' in result.text
    assert 'BEGIN' in result.text
    assert ' END' in result.text


def test_url_fragment_can_locate_section_without_becoming_evidence():
    body = 'miscellaneous material\n' * 5000 + '\nSpecial section: PhotonicSwitch\nThis device routes light.'
    result = select_evidence(body, title='Interesting development', url='https://example.com/#PhotonicSwitch')
    assert 'This device routes light.' in result.text
    assert result.strategy == 'relevant_excerpts'
    assert 'Interesting development' not in result.text


def test_metadata_keeps_attribution_when_relevant_content_is_far_away():
    metadata = ('Page metadata (publisher-provided context, not article body):\n'
                'description: ' + 'Publisher self description. ' * 70 +
                '\n\nExtracted body:\n')
    body = metadata + 'Routine notes\n' * 4000 + 'ZetaDB added snapshot reads, excluding legacy tables.'
    result = select_evidence(body, title='ZetaDB snapshot reads', max_chars=6000)
    assert metadata in result.text
    assert 'ZetaDB added snapshot reads, excluding legacy tables.' in result.text
    assert len(result.text) <= 6000


def test_unicode_offsets_and_budget_preserve_source():
    body = '历史版本与其他说明。\n' * 9000 + '\n更新说明\n数据库新增向量索引，但暂不支持分区表。'
    result = select_evidence(body, title='数据库新增向量索引', max_chars=6000)
    assert '暂不支持分区表' in result.text
    assert len(result.text) <= 6000


def test_small_invalid_budget_is_rejected():
    with pytest.raises(ValueError):
        select_evidence('body', title='title', max_chars=0)
