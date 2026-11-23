"""Collected hashtags are bounded suggestions, never independent evidence."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from ctn_data.generation_topics import tag_context
from ctn_data.db import connect, initialize
from ctn_data.social_topics import TAGS_KEY, POPULAR_KEY


def tag(name, rank=None, count=None):
    return {'tag_id': len(name), 'name': name, 'post_count': count,
            'popular_rank': rank, 'observed_at': '2026-10-20T01:00:00Z'}


class GenerationTopicTests(TestCase):
    def context(self, all_tags, popular=(), **kwargs):
        with patch('ctn_data.generation_topics.read_social_context', return_value={
                'items': all_tags, 'total': len(all_tags)}), patch(
                'ctn_data.generation_topics.topical_context', return_value={
                    'tags': list(popular), 'observed_at': '2026-10-20T01:00:00Z',
                    'popular_observed_at': '2026-10-20T01:00:00Z',
                    'all_tags_completed_at': '2026-10-20T00:00:00Z'}):
            return tag_context(object(), **kwargs)

    def test_matches_sources_and_interests_preserving_observed_popularity(self):
        result = self.context([tag('housing'), tag('yes2vax'), tag('workersrights'), tag('and')],
                              [tag('yes2vax', 1, 245)],
                              sources=[{'text': 'The #yes2vax discussion continues and needs care.'}],
                              interests=['housing'])
        self.assertEqual([row['name'] for row in result['tags']], ['yes2vax', 'housing'])
        self.assertEqual(result['tags'][0]['post_count'], 245)
        self.assertEqual(result['tags'][0]['popular_rank'], 1)
        self.assertEqual(result['tags'][0]['observed_at'], '2026-10-20T01:00:00Z')
        self.assertEqual(result['tags'][0]['popular_observed_at'], '2026-10-20T01:00:00Z')
        self.assertIsNone(result['tags'][1]['post_count'])
        self.assertFalse(result['popularity_is_factual_evidence'])

    def test_hot_unrelated_tags_do_not_become_suggestions(self):
        result = self.context([tag('workersrights')], [tag('yes2vax', 1, 245)],
                              sources=[{'text': 'A local train timetable.'}])
        self.assertEqual(result['tags'], [])

    def test_known_names_only_bounded_to_eight_even_with_many_hashtags(self):
        names = ['topic' + str(index) for index in range(12)]
        result = self.context([tag(name) for name in names],
                              query=' '.join('#' + name for name in names) + ' #invented')
        self.assertEqual(len(result['tags']), 8)
        self.assertNotIn('invented', [row['name'] for row in result['tags']])
        self.assertTrue(all('post_id' not in row and 'id' not in row for row in result['tags']))

    def test_ordinary_short_or_common_words_need_explicit_hashtag(self):
        result = self.context([tag('a'), tag('and'), tag('kingstonia')],
                              query='A report and discussion about Kingstonia. #and')
        self.assertEqual({row['name'] for row in result['tags']}, {'and', 'kingstonia'})

    def test_reads_the_shared_collected_catalogue_without_a_remote_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'topics.db'
            initialize(path)
            conn = connect(path)
            try:
                with conn:
                    conn.executemany('INSERT INTO meta(key,value) VALUES(?,?)', [
                        (TAGS_KEY, json.dumps({'items': [tag('housing'), tag('yes2vax')]})),
                        (POPULAR_KEY, json.dumps({'items': [tag('yes2vax', 1, 245)],
                                                  'observed_at': '2026-10-20T01:00:00Z'}))])
                result = tag_context(conn, sources=[{'text': 'Discussing #yes2vax'}])
            finally:
                conn.close()
        self.assertEqual(len(result['tags']), 1)
        self.assertEqual(result['tags'][0]['name'], 'yes2vax')
        self.assertEqual(result['tags'][0]['post_count'], 245)
        self.assertEqual(result['popular_observed_at'], '2026-10-20T01:00:00Z')

    def test_workers_rights_phrase_matches_only_a_collected_tag(self):
        result = self.context([tag('workersrights')], query="Workers' rights need attention.")
        self.assertEqual([row['name'] for row in result['tags']], ['workersrights'])
        self.assertEqual(result['tags'][0]['match_basis'], 'source_or_query_alias')
        self.assertEqual(result['tags'][0]['matched_alias'], 'workers rights')

    def test_health_choices_phrase_matches_a_persona_interest(self):
        result = self.context([tag('healthchoices')], interests=['health choices'])
        self.assertEqual([row['name'] for row in result['tags']], ['healthchoices'])
        self.assertEqual(result['tags'][0]['match_basis'], 'persona_interest_alias')

    def test_keep_kingstonia_open_phrase_preserves_collected_spelling(self):
        result = self.context([tag('KeepKingstoniaOpen')],
                              sources=[{'text': 'We want to keep Kingstonia open.'}])
        self.assertEqual([row['name'] for row in result['tags']], ['KeepKingstoniaOpen'])
        self.assertEqual(result['tags'][0]['matched_alias'], 'keep kingstonia open')

    def test_vaccination_topic_does_not_imply_a_yes2vax_stance(self):
        result = self.context([tag('yes2vax')], [tag('yes2vax', 1, 245)],
                              query='Questions about vaccination policy.')
        self.assertEqual(result['tags'], [])
        explicit = self.context([tag('yes2vax')], query='I support vaccination.')
        self.assertEqual([row['name'] for row in explicit['tags']], ['yes2vax'])

    def test_alias_cannot_create_a_tag_missing_from_the_catalogue(self):
        result = self.context([tag('kingstonia')], query='Workers rights and health choices.')
        self.assertEqual(result['tags'], [])
