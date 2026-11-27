"""CN receipt collection with the maintained driver and a real disposable ledger."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/tripo-studio-opencli/scripts"
sys.path.insert(0, str(SCRIPTS))
import tripo_actions
import tripo_studio_driver as driver
from tripo_pricing import CreditEstimate


class DeferredWorkflow(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='tripo-deferred-test-')
        self.addCleanup(temp.cleanup)
        self.args = SimpleNamespace(ledger=str(Path(temp.name) / 'ledger.json'), runtime_dir=temp.name,
                                    task_id='deferred-test', profile='fixture', region='cn')
        self.ledger('init-task', '--budget-cap', '100', '--max-submissions', '10', '--region', 'cn',
                    '--profile', self.args.profile)
        self.project_a = '00000001-0000-4000-8000-000000000001'
        self.project_b = '00000001-0000-4000-8000-000000000002'
        self.operator_a = '00000002-0000-4000-8000-000000000001'
        self.operator_b = '00000002-0000-4000-8000-000000000002'

    def ledger(self, command, *options):
        return driver._ledger(Path(self.args.ledger), command, '--task-id', self.args.task_id, *options)

    def submitted(self, key, operators, projects, baseline=100, cost=15, deferred=True):
        profile_key = 'deferred_profile' if deferred else 'studio_profile'
        metadata = {profile_key: self.args.profile, 'studio_profile': self.args.profile,
                    'expected_results': len(operators),
                    'preflight': {'balance': {'credit_balance': baseline}}}
        self.ledger('reserve', '--step-key', key, '--operation', 'generate_hd', '--credits', str(cost),
                    '--metadata-json', json.dumps(metadata))
        self.ledger('commit', '--step-key', key, '--status', 'submitting')
        evidence = {'operator_ids': operators, 'project_ids': projects}
        self.ledger('commit', '--step-key', key, '--status', 'submitted', '--operator-id', operators[0],
                    '--project-id', projects[0], '--evidence-json', json.dumps(evidence))

    def collect(self, rows, balance=70):
        with patch.object(driver, '_ensure_daemon'), patch.object(driver, '_opencli', return_value=json.dumps(rows)) as read:
            with patch.object(driver, '_fetch_preflight', return_value={'balance': {'credit_balance': balance}}):
                result = driver.cmd_collect(self.args)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[1:4], ('tripo-studio', 'progress', '--ids'))
        return result, read.call_args.args[4]

    def test_collect_overlapping_assets_by_receipt_then_resume_remaining(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        self.submitted('b', [self.operator_b], [self.project_b], baseline=85)
        result, ids = self.collect([
            {'operator_id': self.operator_b, 'status': 'prepare', 'progress': 0},
            {'operator_id': self.operator_a, 'status': 'success', 'progress': 100,
             'model_url': 'https://fixture.invalid/signature'},
        ])
        self.assertEqual(ids, f'{self.operator_a},{self.operator_b}')
        self.assertEqual(result['steps']['a']['state'], 'success')
        self.assertEqual(result['steps']['b']['state'], 'submitted')
        self.assertTrue(result['has_pending'])
        self.assertNotIn('signature', json.dumps(result))
        self.assertEqual(result['wallet']['observed_net_decrease'], 30)
        self.assertEqual(result['wallet']['submitted_credit_ceiling'], 30)

        result, ids = self.collect([{'operator_id': self.operator_b, 'status': 'success'}])
        self.assertEqual(ids, self.operator_b)
        self.assertFalse(result['has_pending'])
        self.assertEqual(self.ledger('status')['steps']['a']['evidence']['operator_ids'], [self.operator_a])

    def test_same_project_blocks_before_click_while_other_project_can_submit(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        self.args.submit_only = True
        self.args.project_id = self.project_b
        tripo_actions.check_queued_submission(driver, self.args, 85)
        self.args.project_id = self.project_a
        with self.assertRaisesRegex(RuntimeError, 'unsettled operation'):
            tripo_actions.check_queued_submission(driver, self.args, 85)

    def test_wrong_operator_or_transport_failure_leaves_durable_submission(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        with self.assertRaisesRegex(RuntimeError, 'unrelated progress identity'):
            self.collect([{'operator_id': self.operator_b, 'status': 'success'}], balance=85)
        with patch.object(driver, '_ensure_daemon'), patch.object(driver, '_opencli', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                driver.cmd_collect(self.args)
        self.assertEqual(self.ledger('status')['steps']['a']['status'], 'submitted')

    def test_unaccounted_wallet_decrease_halts_without_discarding_receipt(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        with self.assertRaisesRegex(RuntimeError, 'wallet decrease exceeds'):
            self.collect([{'operator_id': self.operator_a, 'status': 'success'}], balance=84)
        ledger = self.ledger('status')
        self.assertTrue(ledger['halted'])
        self.assertEqual(ledger['steps']['a']['status'], 'submitted')
        self.assertEqual(ledger['halt_evidence']['progress'][self.operator_a]['status'], 'success')
        self.assertEqual(ledger['steps']['a']['evidence']['operator_ids'], [self.operator_a])

    def test_profile_cannot_start_another_batch_in_the_shared_ledger(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        path = Path(self.args.ledger)
        driver._ledger(path, 'init-task', '--task-id', 'second', '--budget-cap', '100',
                       '--profile', self.args.profile, '--region', 'cn')
        metadata = json.dumps({'studio_profile': self.args.profile, 'deferred_profile': self.args.profile})
        with self.assertRaisesRegex(RuntimeError, 'another active spending batch'):
            driver._ledger(path, 'reserve', '--task-id', 'second', '--step-key', 'b',
                           '--credits', '15', '--operation', 'generate_hd', '--metadata-json', metadata)

    def test_collect_reports_unresolved_step_without_contacting_cloud(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        self.ledger('commit', '--step-key', 'a', '--status', 'unknown')
        with patch.object(driver, '_opencli') as browser:
            summary = driver.cmd_collect(self.args)
        browser.assert_not_called()
        self.assertTrue(summary['has_pending'])
        self.assertTrue(summary['halted'])
        self.assertEqual(summary['requires_other_action'], {'a': 'unknown'})

    def test_page_bound_steps_use_the_batch_wallet_when_cloud_work_overlaps(self):
        self.submitted('a', [self.operator_a], [self.project_a])
        self.args.project_id = self.project_b
        self.args.step_key = 'b'
        self.args._before_preflight = {'balance': {'credit_balance': 100}}
        self.args._estimated_credits = 10
        self.args.timeout_sec = 60
        self.args._batch_wallet = tripo_actions.check_queued_submission(driver, self.args, 100)
        self.submitted('b', [self.operator_b], [self.project_b], cost=10, deferred=False)
        receipt = {'operator_ids': [self.operator_b], 'project_ids': [self.project_b]}
        with patch.object(driver, '_poll_operator', return_value={'status': 'success'}):
            with patch.object(driver, '_fetch_preflight', return_value={'balance': {'credit_balance': 75}}):
                tripo_actions.finish_operators(driver, self.args, receipt, verify=lambda: {'asset_applied': True})

        result = self.ledger('status')['steps']['b']
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['evidence']['asset_applied'])
        self.assertEqual(result['evidence']['wallet']['submitted_credit_ceiling'], 25)

    def test_grouped_receipt_tracks_other_results_until_the_whole_step_is_terminal(self):
        self.submitted('batch', [self.operator_a, self.operator_b], [self.project_a, self.project_b], cost=30)
        failed = {'operator_id': self.operator_a, 'status': 'failed'}
        result, _ = self.collect([failed, {'operator_id': self.operator_b, 'status': 'queued'}])
        self.assertTrue(result['has_pending'])
        self.assertEqual(result['steps']['batch']['progress'][0]['status'], 'failed')
        result, _ = self.collect([failed, {'operator_id': self.operator_b, 'status': 'success'}])
        self.assertFalse(result['has_pending'])
        self.assertTrue(result['halted'])
        self.assertEqual(result['steps']['batch']['state'], 'failed')

    def test_cli_rejects_unapproved_or_page_bound_deferred_actions(self):
        unsafe_inputs = [
            ['generate', '--mode', 'text', '--prompt', 'test', '--submit-only'],
            ['--region', 'global', 'collect', '--task-id', 'test'],
            ['postprocess', '--operation', 'smart-uv', '--project-id', self.project_a,
             '--source-operator-id', self.operator_a, '--execute-spend', '--submit-only'],
        ]
        for args in unsafe_inputs:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(sys, 'argv', ['tripo_studio_driver.py', *args]), self.assertRaises(SystemExit) as error:
                    driver.main()
                self.assertEqual(error.exception.code, 2)

    def test_generate_returns_after_ledger_receipt_without_waiting_for_cloud(self):
        args = driver.build_parser().parse_args([
            '--profile', self.args.profile, '--runtime-dir', self.args.runtime_dir, 'generate',
            '--mode', 'text', '--prompt', 'Fixture', '--task-id', self.args.task_id,
            '--step-key', 'gen', '--submit-only', '--execute-spend',
        ])
        estimate = CreditEstimate('generate_hd', 15, 0, 15, None, [])
        preflight = {'balance': {'credit_balance': 100}, 'quota': {}}
        observed = {'submit': {'text': 'Generate 15', 'credit_prop': 15}}
        receipt = {'project_ids': [self.project_a], 'operator_ids': [self.operator_a]}

        def store_verified_receipt(*_):
            self.submitted('gen', receipt['operator_ids'], receipt['project_ids'])
            return receipt

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(driver, '_ensure_daemon'))
            browser = stack.enter_context(patch.object(driver, '_opencli'))
            stack.enter_context(patch.object(driver, '_fetch_preflight', return_value=preflight))
            stack.enter_context(patch.object(tripo_actions, 'estimate_generate', return_value=estimate))
            submit = stack.enter_context(patch.object(tripo_actions, 'execute_ui', side_effect=store_verified_receipt))
            wait = stack.enter_context(patch.object(tripo_actions, 'finish_operators'))
            ui = stack.enter_context(patch.object(tripo_actions, 'StudioUI')).return_value
            ui.verify_generate.return_value = observed
            ui.one_any.return_value = {'ref': 1}
            result = driver.cmd_generate(args)

        self.assertEqual(result['state'], 'submitted')
        self.assertEqual(result['receipt'], receipt)
        self.assertEqual(self.ledger('status')['steps']['gen']['status'], 'submitted')
        self.assertEqual(browser.call_args.args[3], 'close')
        submit.assert_called_once()
        wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
