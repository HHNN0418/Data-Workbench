"""Native typed account review validates evidence output before exposing it."""
import copy
import json
import unittest
from unittest.mock import patch

from ctn_data import jev
from ctn_data.ai_providers import ProviderError


class JevTests(unittest.TestCase):
    def setUp(self):
        self.provider = {'protocol': 'typesafe', 'base_url': jev.API_BASE,
                         'model': 'jev-1.13.0', 'api_key': 'fake-private-jev-key'}
        self.state = {'account_id': 1001, 'observed_posts': [
            {'post_id': 7, 'body': 'A public observation', 'created_at': '2026-10-19T12:00:00Z'}]}

    def response(self):
        answers = {}
        for name, question in jev.review_questions().items():
            options = list(question['criteria'])
            answers[name] = {'type': 'choice', 'choice': options[0], 'confidence': 1.0,
                             'probabilities': {option: float(i == 0)
                                               for i, option in enumerate(options)}}
        return {'model': 'jev-1.13.0', 'answers': answers,
                'usage': {'input_tokens': 200, 'output_tokens': 30}}

    def test_one_native_call_preserves_distribution_and_labels_without_success_probability(self):
        response = self.response()
        response['credential'] = self.provider['api_key']
        response['answers']['automation_evidence']['private'] = self.provider['api_key']
        response['usage']['private'] = self.provider['api_key']
        with patch.object(jev, '_request_json', return_value=response) as request:
            result = jev.judge_account(self.provider, self.state)
        request.assert_called_once()
        endpoint, raw, headers = request.call_args.args
        payload = json.loads(raw)
        self.assertEqual(endpoint, jev.API_BASE + '/systemone')
        self.assertEqual(headers['Authorization'], 'Bearer fake-private-jev-key')
        self.assertEqual(payload['state'], {'account_observations': self.state})
        self.assertEqual(payload['model'], 'jev-1.13.0')
        self.assertEqual(payload['questions'], jev.review_questions())
        self.assertEqual(set(payload), {'state', 'model', 'questions'})
        self.assertNotIn(self.provider['api_key'], raw.decode())
        self.assertEqual(result['answers']['automation_evidence']['probabilities'],
                         response['answers']['automation_evidence']['probabilities'])
        self.assertTrue(result['uncalibrated_for_ctn'])
        self.assertNotIn('success_probability', result)
        self.assertNotIn(self.provider['api_key'], json.dumps(result))
        instructions = json.dumps(payload['questions'])
        self.assertIn('NPC citizens also use AI systems', instructions)
        self.assertIn('Missing news coverage does not establish falsehood', instructions)

    def test_all_required_choices_are_validated_not_fabricated_or_normalized(self):
        cases = []
        invalid = self.response()
        invalid['answers'].pop('evidence_sufficiency')
        cases.append(invalid)
        invalid = self.response()
        invalid['answers']['unknown'] = invalid['answers']['coordinated_activity']
        cases.append(invalid)
        for field, value in (('type', 'score'), ('choice', 'confirmed_bot'),
                             ('choice', ['insufficient']), ('confidence', -0.1),
                             ('confidence', 1.1), ('confidence', float('nan')),
                             ('confidence', True), ('confidence', '1')):
            invalid = self.response()
            invalid['answers']['automation_evidence'][field] = value
            cases.append(invalid)
        for probabilities in ({'insufficient': 1},
                              {'insufficient': 0, 'weak': 0, 'moderate': 0, 'strong': 0},
                              {'insufficient': 2, 'weak': -1, 'moderate': 0, 'strong': 0},
                              {'insufficient': float('nan'), 'weak': 0, 'moderate': 0, 'strong': 0},
                              {'insufficient': True, 'weak': 0, 'moderate': 0, 'strong': 0},
                              {'insufficient': 0, 'weak': 1, 'moderate': 0, 'strong': 0}):
            invalid = self.response()
            invalid['answers']['automation_evidence']['probabilities'] = probabilities
            cases.append(invalid)
        for usage in (None, {}, {'input_tokens': -1, 'output_tokens': 2},
                      {'input_tokens': True, 'output_tokens': 2}):
            invalid = self.response()
            invalid['usage'] = usage
            cases.append(invalid)
        cases.extend((None, [], {'choices': []}))
        for response in cases:
            with self.subTest(response=response), patch.object(jev, '_request_json', return_value=response) as request:
                with self.assertRaises(ProviderError) as caught:
                    jev.judge_account(self.provider, self.state)
                self.assertEqual(caught.exception.status_code, 502)
                request.assert_called_once()

    def test_rejects_unsupported_connection_or_missing_key_before_transport(self):
        for update in ({'protocol': 'openai_chat'}, {'protocol': 'unknown'},
                       {'base_url': 'https://example.com/v1'},
                       {'base_url': 'http://127.0.0.1/v1'},
                       {'base_url': jev.API_BASE + '?key=secret'},
                       {'base_url': 'https://api.typesafe.ai.evil.example/v1'},
                       {'model': ''}, {'api_key': ''}, {'api_key': 'invalid\nkey'}):
            provider = dict(self.provider, **update)
            with self.subTest(update=update), patch.object(jev, '_request_json') as request:
                with self.assertRaises((ValueError, ProviderError)):
                    jev.judge_account(provider, self.state)
                request.assert_not_called()

    def test_bad_state_is_rejected_without_sending_or_truncating(self):
        for state in (None, '', {}, [], 1, {'number': float('inf')},
                      {1: 'not a string key'}, {'custom': object()},
                      'x' * (jev.MAX_STATE_BYTES + 1)):
            with self.subTest(state_type=type(state)), patch.object(jev, '_request_json') as request:
                with self.assertRaises(ValueError):
                    jev.judge_account(self.provider, state)
                request.assert_not_called()

    def test_transport_errors_are_sanitized_without_retries(self):
        for error, status in ((TimeoutError('fake-private-jev-key'), 504),
                              (OSError('Authorization Bearer fake-private-jev-key'), 502),
                              (ValueError('upstream raw secret'), 502)):
            with self.subTest(error=type(error)), patch.object(jev, '_request_json', side_effect=error) as request:
                with self.assertRaises(ProviderError) as caught:
                    jev.judge_account(self.provider, self.state)
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn('fake-private-jev-key', str(caught.exception))
                self.assertNotIn('upstream raw secret', str(caught.exception))
                request.assert_called_once()
        error = ProviderError('AI 服务限流或额度不足，请稍后重试', 429)
        with patch.object(jev, '_request_json', side_effect=error) as request:
            with self.assertRaises(ProviderError) as caught:
                jev.judge_account(self.provider, self.state)
            self.assertEqual(caught.exception.status_code, 429)
            request.assert_called_once()

    def test_upstream_model_cannot_echo_secret_and_rubrics_do_not_share_mutations(self):
        response = self.response()
        response['model'] = self.provider['api_key']
        with patch.object(jev, '_request_json', return_value=response):
            with self.assertRaises(ProviderError) as caught:
                jev.judge_account(self.provider, self.state)
        self.assertNotIn(self.provider['api_key'], str(caught.exception))
        questions = jev.review_questions()
        original = copy.deepcopy(questions)
        questions['automation_evidence']['criteria']['strong'] = 'mutated'
        self.assertEqual(jev.review_questions(), original)

    def test_categorical_validator_is_shared_without_inventing_probabilities(self):
        choices = {'evidence_sufficiency': 'limited', 'automation_evidence': 'weak',
                   'coordinated_activity': 'insufficient'}
        self.assertEqual(jev.validate_choices(choices), choices)
        self.assertIsNot(jev.validate_choices(choices), choices)
        self.assertEqual({name: tuple(question['criteria'])
                          for name, question in jev.review_questions().items()},
                         jev.JUDGMENT_CHOICES)
        for invalid in ({}, dict(choices, unknown='strong'),
                        dict(choices, automation_evidence='confirmed_bot'),
                        dict(choices, automation_evidence={'choice': 'weak'})):
            with self.assertRaises(ValueError):
                jev.validate_choices(invalid)


if __name__ == '__main__':
    unittest.main()
