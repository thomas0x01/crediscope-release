import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

REPO = 'thomas0x01/crediscope-release'


def gh(*args, **kwargs):
    return subprocess.run(['gh', *args, '-R', REPO], check=True, text=True, **kwargs)


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r'fb_[0-9a-f]{32}', sys.argv[1]):
        sys.exit('Usage: send_feedback_followup.py fb_<full id> (note on stdin)')
    note = sys.stdin.read().strip()
    if not note or len(note.encode('utf-16-le')) > 4000:
        sys.exit('Follow-up must contain 1–2000 UTF-16 units')
    handle = uuid.uuid4().hex.upper()
    name = 'FEEDBACK_REQUEST_' + handle
    request = {'feedback_id': sys.argv[1], 'created_at': datetime.now(timezone.utc).isoformat(), 'note': note}
    gh('secret', 'set', name, input=json.dumps(request, ensure_ascii=False))
    try:
        run = gh('workflow', 'run', 'feedback-followup.yml', '--ref', 'main', '-f', 'request_id=' + handle, capture_output=True)
        url = run.stdout.strip()
        match = re.fullmatch(r'https://github\.com/thomas0x01/crediscope-release/actions/runs/(\d+)', url)
        if not match:
            sys.exit('No run URL returned. Check run history before retrying.')
        print(url, flush=True)
        gh('run', 'watch', match[1], '--interval', '4', '--exit-status')
    finally:
        gh('secret', 'delete', name)


if __name__ == '__main__':
    main()
