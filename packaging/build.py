"""Build on each target OS: python packaging/build.py."""
import subprocess
import shutil
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
separator=';' if sys.platform=='win32' else ':'
name='面试题库助手'
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed','--name',name,
         '--add-data',f'{root/"static"}{separator}static',
         '--add-data',f'{root/"seeds"}{separator}seeds',
         '--collect-all','keyring','--collect-all','platformdirs',str(root/'launcher.py')]
subprocess.run(command,check=True,cwd=root)
bundle=root/'dist'/(name+'.app' if sys.platform=='darwin' else name)
for record in bundle.rglob('RECORD'):
    if record.parent.name.endswith('.dist-info'):
        record.unlink()
if sys.platform=='darwin':
    shutil.rmtree(root/'dist'/name,ignore_errors=True)
    subprocess.run(['codesign','--force','--deep','--sign','-',str(bundle)],check=True)
(root/(name+'.spec')).unlink(missing_ok=True)
shutil.rmtree(root/'build',ignore_errors=True)
print(bundle)
