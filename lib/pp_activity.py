"""Chronological, searchable event pages over existing ledgers; no new database.

Scan cost is linear in history, memory bounded by a page (plus one record).
Stable timestamp/source IDs keep older pages anchored as new events arrive.
"""
from __future__ import annotations
import heapq
import json
from datetime import datetime, timezone
from pathlib import Path
from pp_common import read_json


def stamp(value):
    try:
        d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d.replace(tzinfo=timezone.utc).timestamp() if d.tzinfo is None else d.timestamp()
    except (ValueError, TypeError, OverflowError):
        return float('-inf')


def events(goal: Path):
    paths = [(p, 'board') for p in (goal / 'board/channels').glob('*.jsonl')]
    paths += [(goal / name, kind) for name, kind in (
        ('routing.jsonl','routing'), ('evidence.jsonl','check'),
        ('questions.jsonl','decision'), ('escalations.jsonl','alert'))]
    for path, kind in paths:
        try:
            with path.open(errors='replace') as fh:
                for line_no, line in enumerate(fh, 1):
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                    except ValueError:
                        continue  # a concurrent append may leave a partial last line
                    yield event(kind, f'{path.relative_to(goal)}:{line_no}', row)
        except OSError:
            continue
    for pattern, kind in [('runs/*/handoff.json','handoff'), ('runs/*/run.json','run'),
                          ('amendments/[0-9]*.json','amendment')]:
        for path in goal.glob(pattern):
            row = read_json(path, {})
            if isinstance(row, dict) and row:
                yield event(kind, str(path.relative_to(goal)), row)


def event(kind, identity, row):
    at = next((row[k] for k in ('at','ts','written_at','ended_at','finished_at','started_at') if row.get(k)), '')
    return {'kind':kind, 'id':identity, 'at':at, 'key':(stamp(at), identity),
            'body':json.dumps(row, ensure_ascii=False, indent=2), 'record':row}


def page(goal: Path, *, before=None, query='', limit=20):
    if limit < 1:
        raise ValueError('page size must be positive')
    query = query.casefold()
    matches = (e for e in events(goal) if (before is None or e['key'] < tuple(before))
               and (not query or query in (e['kind']+' '+e['body']).casefold()))
    selected = heapq.nlargest(limit + 1, matches, key=lambda e: e['key'])
    return selected[:limit], selected[limit-1]['key'] if len(selected) > limit else None


def render(rows, *, expanded=True, query=''):
    out = []
    for row in rows:
        rec = row['record']
        title = rec.get('subject') or rec.get('verdict') or rec.get('event') or rec.get('type') or ''
        out += [f"## {row['at'] or 'Date unknown'} · {row['kind']} · {title}",
                f"Source: {row['id']}"]
        if not expanded:
            if row['kind'] == 'run':
                body = f"Run {rec.get('run')} · {rec.get('backend','unknown model')} · exit {rec.get('exit_code','?')} · {rec.get('watchdog') or 'no watchdog event'}"
            elif row['kind'] == 'handoff':
                body = f"Run {rec.get('run')} · {rec.get('source','unknown source')} · {len(rec.get('done') or [])} done claims · {len(rec.get('blockers') or [])} blockers"
            elif row['kind'] == 'routing':
                body = f"{rec.get('mode','legacy')} · {rec.get('selected','unknown')} · {rec.get('reason','')}"
            else:
                body = str(rec.get('body') or rec.get('detail') or row['body'])
            text = ' '.join(body.split())
            if query:
                haystack = ' '.join(row['body'].split())
                at = haystack.casefold().find(query.casefold())
                if at >= 0:
                    text = haystack[max(0,at-50):at+150]
            out += [text[:200] + ('…' if len(text)>200 else ''), '']
            continue
        if row['kind'] == 'board':
            out += [f"{rec.get('author','unknown')} → {rec.get('channel','board')} · run {rec.get('run')}",
                    str(rec.get('body',''))]
        elif row['kind'] == 'handoff':
            out += [f"Run {rec.get('run')} · {rec.get('source','unknown source')}"]
            for field in ('done','learned','next_steps','blockers','open_questions'):
                if rec.get(field):
                    out += [field.replace('_',' ').title()+':', json.dumps(rec[field],ensure_ascii=False,indent=2)]
        elif row['kind'] == 'routing':
            effective = rec.get('effective') or {}
            out += [f"{rec.get('mode','legacy')} · selected {rec.get('selected','unknown')} · effective {effective.get('class','unknown')}",
                    str(rec.get('reason',''))]
            if rec.get('recovery'):
                out += ['Infrastructure recovery: '+json.dumps(rec['recovery'])]
        elif row['kind'] == 'check':
            out += [f"Run {rec.get('run')} · {rec.get('verdict','unknown')} · stable inputs: {rec.get('stable','unknown')}",
                    str(rec.get('detail','')), f"Revision: {rec.get('revision','unknown')}"]
        else:
            out += [row['body']]
        out += ['']
    return out or ['No matching activity. Clear / search or return to a newer page.']
