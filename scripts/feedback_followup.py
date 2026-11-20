import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from feedback_tickets import fetch_ticket_json, ticket_hash


def deliver(request):
    ticket_id = request['feedback_id']
    note = request['note']
    if not re.fullmatch(r'fb_[0-9a-f]{32}', ticket_id) or not isinstance(note, str) or not note.strip() or len(note.encode('utf-16-le')) > 4000:
        raise ValueError('Invalid follow-up request')
    payload = {'action': 'needs_info', 'note': note}

    def read_records():
        detail = fetch_ticket_json('/' + ticket_id)
        if not detail.get('ok') or detail['feedback_id'] != ticket_id or detail.get('identity_conflicts') != []:
            raise ValueError('Ticket identity check failed')
        records = detail['records']
        initials = [r for r in records if r['kind'] == 'initial']
        if len(initials) != 1:
            raise ValueError('Expected one initial ticket')
        return records

    records = read_records()
    events = [r for r in records if r['kind'] == 'maintainer_event']
    if any(r['payload'] == payload for r in events):
        print('Identical follow-up already present')
        return
    if events and events[-1]['payload']['action'] in ('fixed', 'released'):
        raise ValueError('Ticket already fixed or released')
    event = {'schema': 2, 'feedback_id': ticket_id, 'submission_id': os.environ['GITHUB_RUN_ID'].zfill(32),
             'kind': 'maintainer_event', 'created_at': request['created_at'], 'payload': payload}
    datetime.fromisoformat(event['created_at']).astimezone(timezone.utc)
    event['content_sha256'] = ticket_hash(event)
    if not fetch_ticket_json(method='POST', body=event).get('ok'):
        raise ValueError('Missing write acknowledgement')
    for delay in (0, 2, 5, 10, 20, 30):
        time.sleep(delay)
        rows = [r for r in read_records() if r['submission_id'] == event['submission_id']]
        if len(rows) == 1 and all(rows[0].get(k) == v for k, v in event.items()):
            print('Follow-up verified in original ticket')
            return
    raise ValueError('Readback not confirmed')


if __name__ == '__main__':
    try:
        deliver(json.loads(os.environ['REQUEST']))
    except (Exception, SystemExit) as exc:
        sys.exit(str(exc) if type(exc) is ValueError else 'Feedback operation failed')
