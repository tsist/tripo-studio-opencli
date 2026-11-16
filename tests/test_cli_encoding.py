"""CLI JSON must remain UTF-8 on clean Windows runners, regardless of locale."""
import json,os,shutil,subprocess,sys,unittest,uuid
from pathlib import Path
SCRIPTS=Path(__file__).resolve().parents[1]/'skills/tripo-studio-opencli/scripts'

class CliEncoding(unittest.TestCase):
    def env(self):return {**os.environ,'PYTHONIOENCODING':'ascii','PYTHONUTF8':'0','PYTHONDONTWRITEBYTECODE':'1'}
    def test_ledger_unicode_note_is_utf8_json_under_ascii_parent(self):
        base=Path(__file__).resolve().parent
        root=base/('encoding-test-'+uuid.uuid4().hex)
        assert root.resolve().is_relative_to(base.resolve())
        root.mkdir();self.addCleanup(shutil.rmtree,root,ignore_errors=True)
        p=subprocess.run([sys.executable,str(SCRIPTS/'tripo_ledger.py'),'--ledger',str(root/'ledger.json'),
            'init-task','--task-id','encoding-test','--budget-cap','0','--note','中文回执'],env=self.env(),capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8',errors='replace'))
        body=json.loads(p.stdout.decode('utf-8'))
        self.assertIn('中文回执',json.dumps(body,ensure_ascii=False))
    def test_driver_protocol_is_utf8_json_under_ascii_parent(self):
        # Execute the actual CLI main with a local read-only command stand-in, never a browser.
        code="import sys;sys.path.insert(0,sys.argv[1]);import tripo_studio_driver as d;d.cmd_preflight=lambda args: {'ok':True,'message':'\\u4e2d\\u6587'};sys.argv=['driver','preflight'];raise SystemExit(d.main())"
        p=subprocess.run([sys.executable,'-c',code,str(SCRIPTS)],env=self.env(),capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr.decode('utf-8',errors='replace'))
        self.assertEqual(json.loads(p.stdout.decode('utf-8'))['message'],'中文')

if __name__=='__main__':unittest.main()
