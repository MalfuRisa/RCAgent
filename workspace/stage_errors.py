"""Stable paths for schema and business rejections; retain original reasons."""
import re


def normalized_errors(rows):
    result = []
    for row in rows:
        row = dict(row)
        field = row.get('field') or ''
        path = row.get('path') or '/' + re.sub(r'\[(\d+)\]', r'.\1', field).replace('.', '/')
        row.update(path=path, code=row.get('code') or row.get('reason') or 'invalid',
                   message=row.get('message') or row.get('reason') or 'invalid')
        result.append(row)
    return result
