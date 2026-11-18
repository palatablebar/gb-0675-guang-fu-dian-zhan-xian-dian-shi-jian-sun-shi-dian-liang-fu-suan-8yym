"""断点续传会话存储。

会话与分块全部持久化在 SQLite（与正式批次同库、不同表），进程或容器
重启后仍可接续。分块按 (session_id, offset) 内容寻址：
- 重传同一偏移只比对 sha256，相同则幂等忽略，不重复采信；
- 乱序到达只影响“从偏移 0 连续的最长前缀”，不影响最终拼出的字节；
- 最终只有 total_size 字节齐全且整体摘要吻合时，才允许生成正式批次。
"""
import json
import sqlite3
from app.errors import BusinessError
from app.store import connect

SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS upload_sessions (
  session_id TEXT PRIMARY KEY,
  total_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  filename TEXT NOT NULL,
  context TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  run_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finalized_at TEXT
)
"""
CHUNKS_DDL = """
CREATE TABLE IF NOT EXISTS upload_chunks (
  session_id TEXT NOT NULL,
  offset INTEGER NOT NULL,
  length INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY (session_id, offset)
)
"""


def init_db(path):
    with connect(path) as con:
        con.execute(SESSIONS_DDL)
        con.execute(CHUNKS_DDL)


def create_session(path, session_id, total_size, sha256, filename, context):
    init_db(path)
    with connect(path) as con:
        try:
            con.execute(
                'INSERT INTO upload_sessions(session_id,total_size,sha256,filename,context) VALUES(?,?,?,?,?)',
                (session_id, total_size, sha256, filename, json.dumps(context, ensure_ascii=False, sort_keys=True)),
            )
        except sqlite3.IntegrityError:
            raise BusinessError('SESSION_EXISTS', '上传会话已存在，请改用续传或新会话', 409)


def get_session(path, session_id):
    init_db(path)
    with connect(path) as con:
        row = con.execute('SELECT * FROM upload_sessions WHERE session_id=?', (session_id,)).fetchone()
    if not row:
        raise BusinessError('SESSION_NOT_FOUND', '上传会话不存在', 404)
    data = dict(row)
    data['context'] = json.loads(data['context'])
    return data


def _chunk_rows(con, session_id):
    return con.execute(
        'SELECT offset,length,sha256 FROM upload_chunks WHERE session_id=? ORDER BY offset', (session_id,)
    ).fetchall()


def contiguous_prefix(con, session_id, total_size):
    """从偏移 0 连续覆盖的最长字节前缀长度。"""
    cursor = 0
    for row in _chunk_rows(con, session_id):
        if row['offset'] != cursor:
            break
        cursor += row['length']
    return min(cursor, total_size)


def put_chunk(path, session_id, offset, length, chunk_sha, data, total_size):
    session = get_session(path, session_id)
    if session['status'] != 'open':
        raise BusinessError('SESSION_CLOSED', '会话已终结，不能再写入分块', 409)
    if offset < 0 or length <= 0 or offset + length > total_size:
        raise BusinessError('CHUNK_RANGE', '分块偏移越界或长度非法')
    with connect(path) as con:
        existing = con.execute(
            'SELECT sha256 FROM upload_chunks WHERE session_id=? AND offset=?', (session_id, offset)
        ).fetchone()
        if existing:
            # 同一偏移重传：摘要一致即幂等成功（网络重试的常规路径），不一致则拒绝，
            # 避免同一编号位置被两份不同内容覆盖。
            if existing['sha256'] != chunk_sha:
                raise BusinessError('CHUNK_CONFLICT', '同一偏移已存在不同内容的分块', 409)
            inserted = False
        else:
            # 新分块不允许与已有分块占据重叠字节区间，否则拼出的原件无法定义。
            overlap = con.execute(
                'SELECT 1 FROM upload_chunks WHERE session_id=? AND offset < ? AND offset + length > ? LIMIT 1',
                (session_id, offset + length, offset),
            ).fetchone()
            if overlap:
                raise BusinessError('CHUNK_OVERLAP', '分块与已有分块字节区间重叠，请按统一分块边界续传')
            con.execute(
                'INSERT INTO upload_chunks(session_id,offset,length,sha256,data) VALUES(?,?,?,?,?)',
                (session_id, offset, length, chunk_sha, sqlite3.Binary(data)),
            )
            inserted = True
        received = contiguous_prefix(con, session_id, total_size)
        missing = _gaps(con, session_id, total_size)
    return {'inserted': inserted, 'contiguous_bytes': received, 'missing_ranges': missing}


def _gaps(con, session_id, total_size):
    gaps, cursor = [], 0
    for row in _chunk_rows(con, session_id):
        if row['offset'] > cursor:
            gaps.append([cursor, row['offset']])
        cursor = max(cursor, row['offset'] + row['length'])
    if cursor < total_size:
        gaps.append([cursor, total_size])
    return gaps


def session_status(path, session_id):
    session = get_session(path, session_id)
    with connect(path) as con:
        rows = _chunk_rows(con, session_id)
        stored = sum(r['length'] for r in rows)
        received = contiguous_prefix(con, session_id, session['total_size'])
        missing = _gaps(con, session_id, session['total_size'])
    return {
        'session_id': session_id,
        'filename': session['filename'],
        'total_size': session['total_size'],
        'stored_bytes': min(stored, session['total_size']),
        'contiguous_bytes': received,
        'missing_ranges': missing,
        'complete': received == session['total_size'],
        'status': session['status'],
        'run_id': session['run_id'],
    }


def prefix_bytes(path, session_id):
    """组装从偏移 0 连续的最长前缀，供临时风险预览。"""
    session = get_session(path, session_id)
    with connect(path) as con:
        rows = con.execute(
            'SELECT offset,data,length FROM upload_chunks WHERE session_id=? ORDER BY offset', (session_id,)
        ).fetchall()
        parts, cursor = [], 0
        for row in rows:
            if row['offset'] != cursor:
                break
            parts.append(bytes(row['data']))
            cursor += row['length']
    return b''.join(parts)[: session['total_size']]


def assembled_bytes(path, session_id):
    """全部分块齐全时按 offset 拼接完整原件；有缺口则拒绝。"""
    session = get_session(path, session_id)
    with connect(path) as con:
        rows = con.execute(
            'SELECT offset,data,length FROM upload_chunks WHERE session_id=? ORDER BY offset', (session_id,)
        ).fetchall()
        parts, cursor = [], 0
        for row in rows:
            if row['offset'] != cursor:
                raise BusinessError('UPLOAD_INCOMPLETE', '分块存在缺口，不能终结上传', 409)
            parts.append(bytes(row['data']))
            cursor += row['length']
        if cursor != session['total_size']:
            raise BusinessError('UPLOAD_INCOMPLETE', '分块未集齐，不能终结上传', 409)
    raw = b''.join(parts)
    return session, raw


def mark_finalized(path, session_id, run_id):
    with connect(path) as con:
        cur = con.execute(
            "UPDATE upload_sessions SET status='finalized',run_id=?,finalized_at=datetime('now') WHERE session_id=? AND status='open'",
            (run_id, session_id),
        )
        if cur.rowcount != 1:
            raise BusinessError('SESSION_CLOSED', '会话已终结', 409)
