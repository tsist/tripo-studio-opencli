import copy
import shutil
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'skills/tripo-studio-opencli/scripts'))
import tripo_studio_driver as d
import tripo_actions as a
import tripo_preview as p
from tripo_submit import SingleSubmission, SubmissionConfigError, SubmissionUnknown

PROJECT = '00000002-0000-4000-8000-000000000002'
SOURCE = '00000001-0000-4000-8000-000000000001'
CANDIDATE = '00000003-0000-4000-8000-000000000003'
ENDPOINT = '/v2/studio/operation/uv_edit/apply'
PF = {'balance': {'credit_balance': 1000}, 'quota': {}}


class UVApply(unittest.TestCase):
    TASK_ID = 'uvtest'

    def setUp(self):
        self.clicks = 0
        self.calls = []
        self.networks = 0
        self.mismatch = False
        self.readback_error = False
        self.closes = []
        self.close_status = []
        self._initialize()

    def tearDown(self):
        profile = getattr(getattr(self, 'args', None), 'profile', None)
        if profile:
            for key in list(d._BROWSER_PAGES):
                if key[0] == profile:
                    d._BROWSER_PAGES.pop(key, None)

    def _test_root(self):
        base = (Path(__file__).resolve().parent / 'integration-tests').resolve()
        root = (base / uuid.uuid4().hex).resolve()
        if base not in root.parents:
            raise RuntimeError('integration test path escaped the test workspace')
        return base, root

    def _project_url(self, project=PROJECT):
        return d.REGION_ORIGINS['cn'] + '/workspace/smart-uv/' + project

    def _initialize(self, *, remove_context=False, context_overrides=None):
        self.clicks = 0
        self.calls = []
        self.networks = 0
        self.mismatch = False
        self.readback_error = False
        self.closes = []
        self.close_status = []

        _, root = self._test_root()
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,root,ignore_errors=True)
        self.ledger = root / 'ledger.json'
        d._ledger(
            self.ledger,
            'init-task',
            '--task-id',
            self.TASK_ID,
            '--budget-cap',
            '0',
            '--max-submissions',
            '3',
        )
        self.args = d.build_parser().parse_args(
            [
                '--profile', 'offline',
                '--ledger',
                str(self.ledger),
                'postprocess',
                '--operation',
                'smart-uv',
                '--project-id',
                PROJECT,
                '--task-id',
                self.TASK_ID,
                '--step-key',
                'apply',
                '--apply-preview-step',
                'preview',
                '--unpriced-apply-cap',
                '0',
                '--execute-spend',
            ]
        )
        d._ledger(
            self.ledger,
            'reserve',
            '--task-id',
            self.TASK_ID,
            '--step-key',
            'preview',
            '--operation',
            'uv_preview',
            '--credits',
            '0',
        )
        ctx = {
            'profile': self.args.profile,
            'session': 'uv-preview-' + uuid.uuid4().hex[:10],
            'page': 'uv-preview-page-' + uuid.uuid4().hex[:10],
        }
        if context_overrides:
            ctx.update(context_overrides)
        self.preview_ctx = None if remove_context else copy.deepcopy(ctx)
        preview = {
            'project_id': PROJECT,
            'source_operator_id': SOURCE,
            'candidate_operator_id': CANDIDATE,
        }
        if self.preview_ctx is not None:
            preview['browser_context'] = copy.deepcopy(self.preview_ctx)
        evidence = {
            'result_kind': 'preview_ready',
            'asset_applied': False,
            'preview': preview,
        }
        d._ledger(
            self.ledger,
            'commit',
            '--task-id',
            self.TASK_ID,
            '--step-key',
            'preview',
            '--status',
            'success',
            '--evidence-json',
            json.dumps(evidence),
        )
        self.tab_entry = {'page': ctx['page'], 'url': self._project_url()}
        if (
            not remove_context
            and ctx.get('profile')
            and ctx.get('session')
            and ctx.get('page')
        ):
            d._BROWSER_PAGES[(ctx['profile'], ctx['session'])] = ctx['page']

    def state(self):
        return d._ledger(self.ledger, 'status', '--task-id', self.TASK_ID)

    def transport(self, profile, *parts, **kw):
        op = parts[2]
        self.calls.append(op)
        if op == 'tab':
            self.assertEqual(parts[3], 'list')
            return json.dumps([self.tab_entry] if self.tab_entry is not None else [])
        if op == 'wait':
            return json.dumps({'error': {'code': 'xhr_not_seen'}})
        if op == 'eval':
            return json.dumps({'complete': True, 'count': 1})
        if op == 'open':
            page = 'owned-uv-test'
            d._BROWSER_PAGES[(profile, parts[1])] = page
            return json.dumps({'page': page})
        if op == 'click':
            self.clicks += 1
            return json.dumps({'clicked': True, 'matches_n': 1})
        if op == 'close':
            session = parts[1]
            page = d._BROWSER_PAGES.get((profile, session))
            self.closes.append({'profile': profile, 'session': session, 'page': page})
            self.close_status.append(
                self.state()['steps'].get('apply', {}).get('status')
            )
            d._BROWSER_PAGES.pop((profile, session), None)
            return json.dumps({'closed': page is not None})
        if op == 'network':
            if '--detail' in parts:
                return json.dumps(
                    {
                        'key': 'uv-new',
                        'url': 'https://api-cn.tripo3d.com' + ENDPOINT,
                        'method': 'POST',
                        'status': 200,
                        'body': {
                            'code': 0,
                            'data': {
                                'project_id': PROJECT,
                                'previous_operator_id': SOURCE,
                                'current_operator_id': SOURCE
                                if self.mismatch
                                else CANDIDATE,
                            },
                        },
                    }
                )
            self.networks += 1
            return json.dumps(
                {
                    'captured_at': '2026-11-15T12:00:01Z'
                    if self.networks > 1
                    else '2026-11-15T12:00:00Z',
                    'entries': []
                    if self.networks == 1
                    else [
                        {
                            'key': 'uv-new',
                            'url': 'https://api-cn.tripo3d.com' + ENDPOINT,
                            'method': 'POST',
                            'status': 200,
                            'timestamp': '2026-11-15T12:00:01Z',
                        }
                    ],
                }
            )
        return '{}'

    def ui_class(self):
        parent = self

        class UI:
            actions = []

            def __init__(self, *args):
                pass

            def prepare_uv_apply(self, *args):
                return self.uv_state()

            def command(self, *args):
                return {}

            def one(self, *args, **kwargs):
                return {'ref': '4'}

            def uv_state(self):
                return {
                    'project_id': PROJECT,
                    'source_operator_id': SOURCE,
                    'readonly': False,
                    'candidates': [{'operator_id': CANDIDATE, 'selected': True}],
                    'apply_submit': {'text': '保存', 'disabled': False},
                }

            def wait_uv_state(self, predicate, *args, **kwargs):
                if parent.readback_error:
                    raise OSError('readback unavailable')
                value = {
                    'project_id': PROJECT,
                    'source_operator_id': CANDIDATE,
                }
                if not predicate(value):
                    raise RuntimeError('unexpected verification predicate')
                return value

        return UI

    def run_apply(self):
        try:
            with patch.object(d, '_ensure_daemon'), patch.object(
                d, '_fetch_preflight', return_value=PF
            ), patch.object(d, '_opencli', side_effect=self.transport), patch.object(
                d,
                '_poll_operator',
                return_value={'status': 'success', 'operator_id': CANDIDATE},
            ), patch.object(
                p, 'PostUI', self.ui_class()
            ):
                return p.apply_uv_preview(d, self.args)
        finally:
            self.assertNotIn(
                'open',
                self.calls,
                'UV apply must attach to the recorded page, never open/navigate',
            )

    def test_strict_uv_receipt_and_project_activation(self):
        result = self.run_apply()
        self.assertTrue(result['asset_applied'])
        self.assertEqual(result['applied_operator_id'], CANDIDATE)
        self.assertEqual(self.clicks, 1)
        self.assertEqual(self.state()['steps']['apply']['status'], 'success')
        self.assertIn('tab', self.calls)
        self.assertIn('close', self.calls)
        self.assertEqual(self.calls.count('click'), 1)
        self.assertEqual(self.calls.count('close'), 1)
        self.assertLess(self.calls.index('tab'), self.calls.index('click'))
        self.assertLess(self.calls.index('click'), self.calls.index('close'))
        ctx = self.preview_ctx
        self.assertEqual(
            self.closes,
            [
                {
                    'profile': ctx['profile'],
                    'session': ctx['session'],
                    'page': ctx['page'],
                }
            ],
            'success must close exactly the recorded page once',
        )
        self.assertEqual(self.close_status, ['success'])
        self.assertNotIn((ctx['profile'], ctx['session']), d._BROWSER_PAGES)

    def test_prepare_only_keeps_recorded_page_without_submit_or_apply_reserve(self):
        self.args.prepare_only = True
        self.args.execute_spend = False
        result = self.run_apply()
        self.assertTrue(result['prepared'])
        self.assertTrue(result['ready'])
        self.assertFalse(result['submitted'])
        self.assertEqual(self.clicks, 0)
        self.assertIn('tab', self.calls)
        self.assertNotIn('close', self.calls)
        self.assertEqual(self.closes, [])
        self.assertTrue(self.args._keep_browser)
        self.assertNotIn('apply', self.state()['steps'])
        ctx = self.preview_ctx
        self.assertEqual(
            d._BROWSER_PAGES[(ctx['profile'], ctx['session'])], ctx['page']
        )

    def test_wrong_current_operator_halts_without_retry_or_page_close(self):
        self.mismatch = True
        with self.assertRaises(SubmissionUnknown):
            self.run_apply()
        self.assertTrue(self.state()['halted'])
        self.assertEqual(self.clicks, 1)
        self.assertNotIn('close', self.calls)
        self.assertTrue(self.args._keep_browser)
        ctx = self.preview_ctx
        self.assertEqual(
            d._BROWSER_PAGES[(ctx['profile'], ctx['session'])], ctx['page']
        )

    def test_apply_readback_failure_is_unknown(self):
        self.readback_error = True
        with self.assertRaises(OSError):
            self.run_apply()
        self.assertEqual(self.state()['steps']['apply']['status'], 'unknown')
        self.assertNotIn('close', self.calls)
        self.assertEqual(self.clicks, 1)
        ctx = self.preview_ctx
        self.assertEqual(
            d._BROWSER_PAGES[(ctx['profile'], ctx['session'])], ctx['page']
        )

    def test_cap_is_required_before_any_ui(self):
        self.args.unpriced_apply_cap = None
        with self.assertRaises(RuntimeError):
            self.run_apply()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.clicks, 0)
        self.assertNotIn('apply', self.state()['steps'])

    def test_duplicate_apply_intent_for_same_candidate_is_rejected(self):
        d._ledger(
            self.ledger,
            'reserve',
            '--task-id',
            self.TASK_ID,
            '--step-key',
            'previous',
            '--operation',
            'apply',
            '--credits',
            '0',
            '--metadata-json',
            json.dumps({'preview_step': 'preview'}),
        )
        with self.assertRaises(RuntimeError):
            self.run_apply()
        self.assertEqual(self.clicks, 0)
        self.assertEqual(self.calls, [])

    def test_uv_identity_cannot_be_used_with_another_endpoint(self):
        submission = SingleSubmission(
            lambda *a: self.fail('must reject before any command'),
            json.loads,
            lambda *a: None,
        )
        with self.assertRaises(SubmissionConfigError):
            submission.run(
                '/v2/studio/operation/remesh',
                '4',
                expected_project_id=PROJECT,
                uv_apply_source_id=SOURCE,
                uv_apply_candidate_id=CANDIDATE,
            )

    def test_uv_apply_requires_exact_identity_before_click(self):
        submission = SingleSubmission(
            lambda *a: self.fail('must reject before any command'),
            json.loads,
            lambda *a: None,
        )
        with self.assertRaises(SubmissionConfigError):
            submission.run(ENDPOINT, '4', expected_project_id=PROJECT)

    def test_apply_rejects_missing_or_mismatched_preview_context_without_click_or_reserve(self):
        cases = {
            'missing-context': {'remove_context': True},
            'missing-page': {'context_overrides': {'page': ''}},
            'wrong-profile': {'context_overrides': {'profile': 'other-profile'}},
            'missing-tab': {},
            'wrong-page': {},
            'wrong-route': {},
            'wrong-project-url': {},
            'same-session-conflict': {},
        }
        for label, kwargs in cases.items():
            with self.subTest(label=label):
                self._initialize(**kwargs)
                if label == 'missing-tab':
                    self.tab_entry = None
                elif label == 'wrong-page':
                    self.tab_entry['page'] = 'other-page'
                elif label == 'wrong-route':
                    self.tab_entry['url'] = (
                        d.REGION_ORIGINS['cn']
                        + '/workspace/texture-edit/'
                        + PROJECT
                    )
                elif label == 'wrong-project-url':
                    self.tab_entry['url'] = self._project_url('other-project-123')
                elif label == 'same-session-conflict':
                    ctx = self.preview_ctx
                    d._BROWSER_PAGES[(ctx['profile'], ctx['session'])] = (
                        'other-page'
                    )

                with self.assertRaises(RuntimeError):
                    self.run_apply()

                self.assertEqual(self.clicks, 0)
                self.assertNotIn('click', self.calls)
                self.assertNotIn('close', self.calls)
                self.assertEqual(self.closes, [])
                state = self.state()
                self.assertNotIn('apply', state['steps'])
                self.assertFalse(state.get('halted', False))
                if label in {'missing-context', 'missing-page', 'wrong-profile'}:
                    self.assertNotIn('tab', self.calls)
                else:
                    self.assertIn('tab', self.calls)

                ctx = self.preview_ctx
                if label == 'same-session-conflict':
                    self.assertEqual(
                        d._BROWSER_PAGES[(ctx['profile'], ctx['session'])],
                        'other-page',
                    )
                elif ctx and ctx.get('page'):
                    self.assertEqual(
                        d._BROWSER_PAGES[(ctx['profile'], ctx['session'])],
                        ctx['page'],
                        'attach failures must not close or replace the recorded page',
                    )


if __name__ == '__main__':
    unittest.main()
