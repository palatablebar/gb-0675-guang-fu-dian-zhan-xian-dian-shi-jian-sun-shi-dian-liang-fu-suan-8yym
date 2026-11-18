import csv, io
from app.errors import BusinessError
from app.reasons import reject

COUNTS = lambda: {'input':0,'blank':0,'repeated_header':0,'duplicate':0,'truncated_tail':0}


def _parse_rows(text, state, fields, aliases):
    """解析已确定完整的文本，返回 (records, original_header, header)。"""
    stream = csv.reader(io.StringIO(text, newline=''), strict=True)
    original = next(stream)
    header = [aliases.get(v.strip(), v.strip()) for v in original]
    if header != fields:
        raise BusinessError('BAD_HEADER', '字段或顺序不匹配')
    records = []
    for cells in stream:
        line = stream.line_num
        state['counts']['input'] += 1
        if not cells or not any(v.strip() for v in cells):
            state['counts']['blank'] += 1
            continue
        if cells == original:
            state['counts']['repeated_header'] += 1
            continue
        row = dict(zip(header, cells)); row['_line'] = line
        if len(cells) != len(header):
            reject(state, row, 'COLUMN_COUNT', f'需要{len(header)}列，实际{len(cells)}列')
            continue
        if not row.get('id', '').strip():
            reject(state, row, 'MISSING_ID', '编号不能为空')
            continue
        row['id'] = row['id'].strip()
        records.append(row)
    return records


def _dedupe(state, records):
    by_id = {}
    for row in records:
        by_id.setdefault(row['id'], []).append(row)
    selected = []
    for _key, rows in by_id.items():
        identities = {tuple((k, v) for k, v in r.items() if k != '_line') for r in rows}
        if len(identities) > 1:
            for row in rows:
                reject(state, row, 'DUPLICATE_CONFLICT', '同一编号存在不同原值')
        else:
            selected.append(rows[0])
            state['counts']['duplicate'] += len(rows) - 1
    state['records'] = selected


def _publish(state, encoding):
    state['encoding'] = encoding
    state['views'][state['context']['keys'][0]] = {
        'rows': [dict(r) for r in state['records']],
        'metrics': {'采信行': len(state['records']), '隔离行': len(state['quarantine']),
                    '重复行': state['counts']['duplicate']},
        'title': '原始证据'}


def read_source(state, fields, aliases, provisional=False):
    raw = state['raw_bytes']
    if not raw:
        raise BusinessError('EMPTY_FILE', '请选择非空文件')
    if not raw.strip(b'\xef\xbb\xbf\r\n \t'):
        raise BusinessError('BLANK_FILE', '文件只有空行')
    text, encoding = None, None
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(enc); encoding = enc; break
        except UnicodeDecodeError:
            continue
    if text is None:
        if not provisional:
            raise BusinessError('BAD_ENCODING', '文件编码无法识别')
        # 截断点可能落在多字节字符中间：忽略末尾不完整字符，被丢字节随截断尾行计。
        text = raw.decode('utf-8', errors='ignore'); encoding = 'utf-8-sig'

    physical = text.splitlines(keepends=True)
    truncated_tail = None
    if provisional and physical and not physical[-1].endswith(('\n', '\r')):
        # 末物理行没有行终止符：网络在记录中间截断。即使恰好凑满列数也不采信，
        # 因为无法证明它就是完整原件中的完整记录。
        truncated_tail = physical.pop()
        text = ''.join(physical)

    state['counts'] = COUNTS()
    try:
        try:
            records = _parse_rows(text, state, fields, aliases)
        except StopIteration:
            if not provisional:
                raise BusinessError('HEADER_ONLY', '文件只有表头')
            # 连表头都没传完整：空态预览，等待后续分块。
            records = []
    except csv.Error:
        if not provisional:
            raise BusinessError('BROKEN_CSV', 'CSV引号或末行损坏')
        # 引号在记录中途被截断：把最后一条物理行整行隔离，用剩余完整行再解析一次。
        truncated_tail = physical.pop() if physical else truncated_tail
        text = ''.join(physical)
        state['counts'] = COUNTS()
        try:
            records = _parse_rows(text, state, fields, aliases)
        except StopIteration:
            records = []

    if not state['counts']['input'] and not provisional:
        raise BusinessError('HEADER_ONLY', '文件只有表头')
    if truncated_tail is not None:
        state['counts']['truncated_tail'] = 1
        state['quarantine'].append({
            'line': 0, 'id': '', 'code': 'TRUNCATED_TAIL',
            'detail': '网络截断的不完整末行，未采信；待续传补齐后重新评估',
            'raw': {'tail': truncated_tail[:200]}})

    _dedupe(state, records)
    _publish(state, encoding)
