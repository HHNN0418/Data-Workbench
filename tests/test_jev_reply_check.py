"""Jev-first reply check (D76): a Jev pass skips the DeepSeek check, anything else falls back to it."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctn_data import jev
from ctn_data.agent_control import AgentController
from ctn_data.ai_providers import ProviderError

JEV = {'id': 'jev', 'protocol': 'typesafe', 'enabled': True, 'model': 'jev-latest',
       'base_url': 'https://api.typesafe.ai/v1', 'api_key': 'secret'}


def answer(direction='toward_yes', concedes='no', unsupported='no'):
    return {'model': 'jev-latest', 'answers': {'direction': {'choice': direction}, 'concedes_no': {'choice': concedes},
                                               'unsupported': {'choice': unsupported}}}


class ReplyCheckTests(unittest.TestCase):
    def test_reads_the_three_verdicts(self):
        with patch('ctn_data.jev._request_json', return_value=answer(unsupported='yes')) as call:
            verdict = jev.reply_check(JEV, 'Free at GP clinics.', 'Is it free?', ['The jab is free.'])
        self.assertEqual(verdict, {'direction': 'toward_yes', 'concedes_no': False, 'unsupported': True})
        self.assertIn(b'"facts": ["The jab is free."]', call.call_args.args[1])

    def test_rejects_unknown_choices(self):
        with patch('ctn_data.jev._request_json', return_value=answer(direction='sideways')):
            with self.assertRaises(ProviderError):
                jev.reply_check(JEV, 'x', '', [])


class ControllerTests(unittest.TestCase):
    def controller(self, providers):
        controller = object.__new__(AgentController)
        controller.providers = SimpleNamespace(list_public=lambda: {'providers': providers},
                                               get=lambda provider_id: JEV)
        return controller

    def test_pass_skips_deepseek_and_fail_falls_back(self):
        controller = self.controller([JEV])
        facts = [{'id': 'f', 'claim': 'The jab is free.'}]
        with patch('ctn_data.jev.reply_check', return_value={'direction': 'toward_yes', 'concedes_no': False,
                                                             'unsupported': False}):
            quick = controller._jev_reply_check('Free.', 'reply', facts, {'text': 'Is it free?'})
        self.assertEqual((quick['pass'], quick['check']['checker']), (True, 'jev'))
        with patch('ctn_data.jev.reply_check', return_value={'direction': 'toward_yes', 'concedes_no': False,
                                                             'unsupported': True}):
            self.assertFalse(controller._jev_reply_check('Free.', 'reply', facts, None)['pass'])

    def test_posts_errors_and_missing_connection_use_deepseek(self):
        self.assertIsNone(self.controller([JEV])._jev_reply_check('x', 'post', [], None))
        self.assertIsNone(self.controller([])._jev_reply_check('x', 'reply', [], None))
        with patch('ctn_data.jev.reply_check', side_effect=ProviderError('down', 502)):
            self.assertIsNone(self.controller([JEV])._jev_reply_check('x', 'reply', [], None))


if __name__ == '__main__':
    unittest.main()
