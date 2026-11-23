import json
import unittest
from unittest.mock import patch
from ctn_data.account_judgment import judge_account
from ctn_data.ai_providers import ProviderError


class AccountJudgmentTests(unittest.TestCase):
    def test_regular_provider_uses_same_categories_without_fake_probabilities(self):
        choices = {'evidence_sufficiency': 'limited', 'automation_evidence': 'weak', 'coordinated_activity': 'insufficient'}
        with patch('ctn_data.ai_providers.complete', return_value={'text': json.dumps(choices), 'model': 'test-model'}) as cloud:
            result = judge_account({'protocol': 'openai_chat'}, {'account': {'score': 60, 'examples': []}})
        self.assertEqual(cloud.call_count, 1)
        self.assertEqual(result['answers']['automation_evidence'], {'type': 'choice', 'choice': 'weak'})
        self.assertTrue(result['uncalibrated_for_ctn'])
        self.assertNotIn('probability', json.dumps(result))

    def test_invalid_categories_and_extra_probability_are_rejected(self):
        for value in ('not json', '{"bot_probability":0.99}', '{"evidence_sufficiency":"certain"}'):
            with patch('ctn_data.ai_providers.complete', return_value={'text': value, 'model': 'test'}):
                with self.assertRaises(ProviderError):
                    judge_account({'protocol': 'openai_chat'}, {'account': 1})

    def test_native_provider_dispatches_without_chat_protocol(self):
        with patch('ctn_data.jev.judge_account', return_value={'model': 'jev-test'}) as native, patch('ctn_data.ai_providers.complete') as cloud:
            result = judge_account({'protocol': 'typesafe'}, {'account': 1})
        native.assert_called_once()
        cloud.assert_not_called()
        self.assertEqual(result['model'], 'jev-test')
