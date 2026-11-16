"""Build allowlisted public archives. Never includes runtime state or git history."""
import hashlib,json,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
VERSION=json.loads((ROOT/'package.json').read_text(encoding='utf-8'))['version']
DIST=ROOT/'dist';DIST.mkdir(exist_ok=True)
TOP=['README.md','LICENSE','THIRD_PARTY_NOTICES.md','CHANGELOG.md','package.json','plugin.json','.gitignore','.gitattributes']
DIRS=['adapters','skills','tests','scripts','docs','.codex-plugin','.github']
def selected():
    paths=[ROOT/n for n in TOP]
    for name in DIRS:
        paths.extend(p for p in (ROOT/name).rglob('*') if p.is_file() and not {'__pycache__','integration-tests','local-tests'}.intersection(p.relative_to(ROOT).parts) and p.suffix not in {'.pyc','.lock','.log'})
    for p in paths:
        assert not p.is_symlink() and p.resolve().is_relative_to(ROOT.resolve())
    return sorted(set(paths))
files=selected()
def archive(kind,items,prefix):
    out=DIST/f'tripo-studio-opencli-{VERSION}-{kind}.zip'
    if out.exists():raise FileExistsError(f'Refusing to overwrite {out}')
    with zipfile.ZipFile(out,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for p,rel in items:z.write(p,str(Path(prefix)/rel))
    return out
source=archive('source',[(p,p.relative_to(ROOT)) for p in files],f'tripo-studio-opencli-{VERSION}')
skill=ROOT/'skills/tripo-studio-opencli'
skill_items=[(p,p.relative_to(skill)) for p in files if p.is_relative_to(skill)]+[(ROOT/'LICENSE',Path('LICENSE'))]
archive('skill',skill_items,'tripo-studio-opencli')
archive('plugin',[(p,p.relative_to(ROOT)) for p in files if p.relative_to(ROOT).parts[0] not in {'tests','.github'}],'tripo-studio-opencli')
archive('adapters',[(p,p.relative_to(ROOT/'adapters/tripo-studio')) for p in files if p.is_relative_to(ROOT/'adapters/tripo-studio')]+[(ROOT/'LICENSE',Path('LICENSE'))],'tripo-studio')
checks=[]
for p in sorted(DIST.glob('*.zip')):
    with zipfile.ZipFile(p) as z:
        assert z.testzip() is None
        assert all(not name.startswith('/') and '..' not in Path(name).parts for name in z.namelist())
    checks.append(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name)
(DIST/'SHA256SUMS').write_text('\n'.join(checks)+'\n',encoding='utf-8')
print(json.dumps({'version':VERSION,'archives':len(checks),'source_files':len(files),'destination':str(DIST)}))
