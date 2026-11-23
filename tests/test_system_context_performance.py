"""Passage selection keeps exhaustive-search results while stopping at its score bound."""
from copy import deepcopy
import re
import unittest
from unittest.mock import patch

from ctn_data import ai_context


def exhaustive_fragment(source, max_tokens):
    """Pre-optimization selection oracle: rank every fitting complete passage."""
    text = source.get('text', '')
    if not isinstance(text, str) or not text:
        return None
    terms = [str(term).lower() for term in source.get('matched_terms', []) if term]
    original_start = source.get('text_excerpt_start', source.get('excerpt_start', 0))
    if not isinstance(original_start, int):
        original_start = 0
    candidates = []
    for match in re.finditer(r'[^\n.!?。！？]+(?:[.!?。！？]+|\n+|$)', text):
        segment = match.group()
        if not segment.strip():
            continue
        row = deepcopy(source)
        row.update(text=segment, text_truncated=True, coverage='excerpt',
                   excerpt_start=original_start + match.start(),
                   text_excerpt_start=original_start + match.start(),
                   excerpt_end=original_start + match.end(),
                   excerpt_offset_unit='unicode_codepoints',
                   text_length=source.get('text_length', len(text)))
        if ai_context.estimate_tokens(row) <= max_tokens:
            candidates.append((sum(term in segment.lower() for term in terms), row))
    # max keeps the first candidate on ties, matching the original strict > update.
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class ContextPerformanceTests(unittest.TestCase):
    def check_selection(self, source, max_tokens=500, *, estimates=None):
        snapshot = deepcopy(source)
        expected = exhaustive_fragment(source, max_tokens)
        with patch.object(ai_context, 'estimate_tokens', wraps=ai_context.estimate_tokens) as counted:
            result = ai_context._fragment(source, max_tokens)
        self.assertEqual(result, expected)
        self.assertEqual(source, snapshot)
        if estimates is not None:
            self.assertEqual(counted.call_count, estimates)
        return result

    def test_empty_terms_stop_at_first_fitting_passage(self):
        for terms in ([], ['', None, False], None):
            with self.subTest(terms=terms):
                source = {'id': 'post:1', 'text': ' \n First complete sentence.\n' + 'Later details.\n' * 99}
                if terms is not None:
                    source['matched_terms'] = terms
                result = self.check_selection(source, estimates=1)
                self.assertEqual(result['text'].strip(), 'First complete sentence.')

    def test_empty_terms_skip_oversized_passage_before_stopping(self):
        source = {'id': 'post:1', 'text': 'q' * 3000 + '. Fits now. Later sentence.'}
        result = self.check_selection(source, estimates=2)
        self.assertEqual(result['text'], ' Fits now.')

    def test_unfitting_maximum_and_partial_match_do_not_stop_search(self):
        source = {'id': 'post:1', 'matched_terms': ['alpha', 'beta'],
                  'text': 'alpha beta ' + 'q' * 3000 + '. alpha partial. alpha beta accepted. alpha beta later.'}
        result = self.check_selection(source, estimates=3)
        self.assertEqual(result['text'], ' alpha beta accepted.')

    def test_duplicate_casefolded_terms_preserve_scoring_and_first_tie(self):
        source = {'id': 'post:1', 'matched_terms': ['ALPHA', 'alpha', 'beta', '', None],
                  'text': 'alpha partial. ALPHA beta accepted. alpha beta later.'}
        result = self.check_selection(source, estimates=2)
        self.assertEqual(result['text'], ' ALPHA beta accepted.')

    def test_no_complete_match_still_chooses_first_best_partial(self):
        result = self.check_selection({'id': 'post:1', 'matched_terms': ['alpha', 'beta'],
                                       'text': 'alpha first. beta second. alpha third.'}, estimates=3)
        self.assertEqual(result['text'], 'alpha first.')

    def test_no_fitting_passage_returns_none(self):
        self.assertIsNone(self.check_selection({'id': 'post:1', 'text': 'First. Second. Third.'},
                                              max_tokens=1, estimates=3))

    def test_unicode_offsets_preserve_priority_and_existing_fallback_rules(self):
        text = '无关😀。 alpha beta chosen! Later.'
        selected_start = text.index(' alpha')
        for offsets, base in [({}, 0), ({'excerpt_start': 13}, 13),
                              ({'text_excerpt_start': 29, 'excerpt_start': 13}, 29),
                              ({'text_excerpt_start': 'invalid', 'excerpt_start': 13}, 0),
                              ({'text_excerpt_start': True}, 1), ({'excerpt_start': -5}, -5)]:
            with self.subTest(offsets=offsets):
                source = {'id': 'post:1', 'text': text, 'matched_terms': ['alpha', 'beta'],
                          'text_length': 5000, 'nested': {'source_id': 'parent:1'}, **offsets}
                result = self.check_selection(source, estimates=2)
                self.assertEqual(result['excerpt_start'], base + selected_start)
                self.assertEqual(result['text_excerpt_start'], base + selected_start)
                self.assertEqual(result['excerpt_end'], base + text.index('!') + 1)
                self.assertEqual(result['text_length'], 5000)
                self.assertEqual(result['excerpt_offset_unit'], 'unicode_codepoints')

    def test_empty_or_nontext_sources_preserve_none(self):
        for value in ('', None, 123, []):
            with self.subTest(text=value):
                self.assertIsNone(self.check_selection({'id': 'post:1', 'text': value}, estimates=0))


if __name__ == '__main__':
    unittest.main()
