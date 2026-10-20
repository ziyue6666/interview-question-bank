"""Fail a source or packaged build containing copied private material."""
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
private=[b'/Users/'+b'zya',b'184'+b'73',b'Campus'+b'Flow',b'vcn'+b'07g4',b'tail'+b'scale-origin',b'InterviewQuestionBank.'+b'DeepSeek']
source_excludes={'.venv','build','dist','work','.git','__pycache__'}

def inspect(path):
    problems=[]
    for item in path.rglob('*'):
        if not item.is_file() or (path==root and (item.suffix=='.spec' or any(part in source_excludes for part in item.relative_to(root).parts))):continue
        relative=item.relative_to(path)
        if item.suffix.lower() in ('.sqlite3','.db') or item.name.endswith(('.sqlite3-shm','.sqlite3-wal')):
            problems.append(f'{relative}: database file')
            continue
        if item.suffix.lower() in ('.png','.jpg','.jpeg') and path==root:
            problems.append(f'{relative}: unexplained image asset')
        blob=item.read_bytes()
        for pattern in private:
            if pattern in blob:problems.append(f'{relative}: private marker')
    return problems

if __name__=='__main__':
    paths=[root]
    if len(sys.argv)>1:paths.append(Path(sys.argv[1]).resolve())
    issues=[x for path in paths for x in inspect(path)]
    if issues:
        print('\n'.join(issues));sys.exit(1)
    print('Privacy audit passed:',', '.join(str(x) for x in paths))
