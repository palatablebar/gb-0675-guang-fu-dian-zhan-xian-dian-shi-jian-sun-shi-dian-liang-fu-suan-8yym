"""断点续传与两级结果边界测试。

覆盖目标：
1. 网络中断后可断点续跑、不重复采信；乱序到达得到同一份正式结果；
2. 临时风险范围与正式归档来源边界清楚，临时批号不可复核/导出/入史。
"""
import hashlib
import pytest
from fastapi.testclient import TestClient
from app import main, store, uploads
from app.config import ROOT
from app.engine import evaluate


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'up.sqlite'))
    return TestClient(main.app)


@pytest.fixture
def clean_bytes():
    return (ROOT / 'sample_data' / 'clean.csv').read_bytes()


def _session(client, raw, name='field.csv', chunk=64):
    sha = hashlib.sha256(raw).hexdigest()
    r = client.post('/api/uploads', json={'total_size': len(raw), 'sha256': sha, 'filename': name})
    assert r.status_code == 200, r.text
    return r.json()['session_id'], sha


def _put(client, sid, raw, offset, chunk):
    part = raw[offset:min(offset + chunk, len(raw))]
    return client.put(f'/api/uploads/{sid}/chunks/{offset}',
                      content=part, headers={'X-Chunk-SHA256': hashlib.sha256(part).hexdigest()})


def test_resume_after_interruption_and_idempotent_retry(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    # 第一批只传到一半即“断网”
    chunk = 64
    half = (len(raw) // 2 // chunk) * chunk
    for off in range(0, half, chunk):
        assert _put(client, sid, raw, off, chunk).status_code == 200
    status = client.get(f'/api/uploads/{sid}').json()
    assert status['complete'] is False
    assert status['contiguous_bytes'] == half
    # 同偏移重传：幂等，不新增采信
    again = _put(client, sid, raw, 0, chunk).json()
    assert again['inserted'] is False
    # 续传剩余分块（含最后不足一个 chunk 的尾块）
    for off in range(half, len(raw), chunk):
        _put(client, sid, raw, off, chunk)
    status = client.get(f'/api/uploads/{sid}').json()
    assert status['complete'] is True
    assert status['missing_ranges'] == []


def test_out_of_order_chunks_yield_identical_run(client, clean_bytes):
    # 统一为 LF 字节，排除 CRLF/文本通道通用换行的差异，纯验证乱序重组
    raw = clean_bytes.replace(b'\r\n', b'\n')
    sid, sha = _session(client, raw)
    chunk = 2  # 极小分块，制造大量乱序与重传压力；2426 可被 2 整除
    offsets = list(range(0, len(raw), chunk))
    shuffled = list(reversed(offsets))  # 完全逆序
    for off in shuffled:
        assert _put(client, sid, raw, off, chunk).status_code == 200
    # 乱序期间连续前缀可能为0，但拼齐后必须完整
    assert client.get(f'/api/uploads/{sid}').json()['complete'] is True
    fin = client.post(f'/api/uploads/{sid}/finalize').json()
    # 与一次性整文件提交的正式批号完全一致（同字节+同上下文+同规则版本）
    direct = client.post('/api/runs', json={'csv_text': raw.decode()}).json()
    assert fin['manifest']['run_id'] == direct['manifest']['run_id']
    assert fin['manifest']['sha256'] == direct['manifest']['sha256']
    # 裸文件 sha 在 finalize 内部已校验通过（不匹配会返回 DIGEST_MISMATCH）
    assert fin['manifest']['provisional'] is False
    # 业务结果逐字段一致
    assert fin['balance'] == direct['balance']
    # 批次历史只有一条：重复上传不混新批
    assert len(client.get('/api/runs').json()) == 1


def test_duplicate_full_upload_reuses_same_run(client, clean_bytes):
    raw = clean_bytes
    sid1, _ = _session(client, raw, 'first.csv')
    chunk = 128
    for off in range(0, len(raw), chunk):
        _put(client, sid1, raw, off, chunk)
    r1 = client.post(f'/api/uploads/{sid1}/finalize').json()
    sid2, _ = _session(client, raw, 'second-upload.csv')
    for off in range(0, len(raw), chunk):
        _put(client, sid2, raw, off, chunk)
    r2 = client.post(f'/api/uploads/{sid2}/finalize').json()
    assert r1['manifest']['run_id'] == r2['manifest']['run_id']
    assert client.get(f'/api/uploads/{sid2}').json()['run_id'] == r1['manifest']['run_id']


def test_chunk_conflict_same_offset_rejected(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    assert _put(client, sid, raw, 0, 64).status_code == 200
    forged = b'x' * 64
    r = client.put(f'/api/uploads/{sid}/chunks/0', content=forged,
                   headers={'X-Chunk-SHA256': hashlib.sha256(forged).hexdigest()})
    assert r.status_code == 409
    assert r.json()['code'] == 'CHUNK_CONFLICT'


def test_declared_chunk_digest_checked(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    r = client.put(f'/api/uploads/{sid}/chunks/0', content=raw[:64],
                   headers={'X-Chunk-SHA256': '0' * 64})
    assert r.status_code == 422
    assert r.json()['code'] == 'CHUNK_DIGEST_MISMATCH'


def test_finalize_blocked_when_incomplete(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    _put(client, sid, raw, 0, 64)
    r = client.post(f'/api/uploads/{sid}/finalize')
    assert r.status_code == 409
    assert r.json()['code'] == 'UPLOAD_INCOMPLETE'


def test_digest_mismatch_refuses_official_run(client, clean_bytes):
    raw = clean_bytes
    sid = client.post('/api/uploads', json={
        'total_size': len(raw), 'sha256': 'a' * 64, 'filename': 'bad.csv'}).json()['session_id']
    chunk = 128
    for off in range(0, len(raw), chunk):
        _put(client, sid, raw, off, chunk)
    r = client.post(f'/api/uploads/{sid}/finalize')
    assert r.status_code == 422
    assert r.json()['code'] == 'DIGEST_MISMATCH'
    # 没有产生任何正式批次
    assert client.get('/api/runs').json() == []


def test_provisional_preview_is_marked_and_quarantines_truncated_tail(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    # 只传前 200 字节，末行在记录中间被截断
    for off in range(0, 200, 64):
        _put(client, sid, raw, off, 64)
    preview = client.get(f'/api/uploads/{sid}/preview').json()
    assert preview['manifest']['run_id'].startswith('P-')
    assert preview['manifest']['provisional'] is True
    assert preview['provenance']['contiguous_bytes'] >= 192
    codes = {q['code'] for q in preview['quarantine']}
    assert 'TRUNCATED_TAIL' in codes
    # 已完整到达的行仍然进入临时视图
    assert preview['views']['intake']['metrics']['采信行'] >= 1


def test_provisional_id_cannot_review_export_rerun_or_page(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    for off in range(0, 200, 64):
        _put(client, sid, raw, off, 64)
    pid = client.get(f'/api/uploads/{sid}/preview').json()['manifest']['run_id']
    assert client.post(f'/api/runs/{pid}/review', json={'revision': 1}).status_code == 422
    assert client.get(f'/api/export/{pid}').status_code == 422
    assert client.post(f'/api/runs/{pid}/rerun').status_code == 422
    assert client.get(f'/api/runs/{pid}').status_code == 422
    assert client.get(f'/intake?run_id={pid}').status_code == 422
    # 临时批号不进批次历史
    assert all(r['id'] != pid for r in client.get('/api/runs').json())


def test_preview_then_complete_finalize_keeps_boundary(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    for off in range(0, 256, 64):
        _put(client, sid, raw, off, 64)
    preview = client.get(f'/api/uploads/{sid}/preview').json()
    for off in range(256, len(raw), 64):
        _put(client, sid, raw, off, 64)
    official = client.post(f'/api/uploads/{sid}/finalize').json()
    # 同一文件，临时与正式批号前缀不同，互不覆盖
    assert preview['manifest']['run_id'] != official['manifest']['run_id']
    assert official['manifest']['run_id'][0] != 'P'
    # 正式批可以导出，临时批始终不行
    assert client.get(f"/api/export/{official['manifest']['run_id']}").status_code == 200
    # 正式结果无截断隔离码
    assert 'TRUNCATED_TAIL' not in {q['code'] for q in official['quarantine']}


def test_preview_html_page_renders(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    for off in range(0, 256, 64):
        _put(client, sid, raw, off, 64)
    html = client.get(f'/uploads/preview?session_id={sid}')
    assert html.status_code == 200
    assert '临时' in html.text and '不可归档' in html.text


def test_preview_before_header_arrives_is_empty_state(client, clean_bytes):
    raw = clean_bytes
    sid, _ = _session(client, raw)
    # 只传 5 个字节，连表头都不完整
    r = client.put(f'/api/uploads/{sid}/chunks/0', content=raw[:5])
    assert r.status_code == 200
    preview = client.get(f'/api/uploads/{sid}/preview').json()
    assert preview['manifest']['run_id'].startswith('P-')
    assert preview['records'] == []
    assert {q['code'] for q in preview['quarantine']} == {'TRUNCATED_TAIL'}
    html = client.get(f'/uploads/preview?session_id={sid}')
    assert html.status_code == 200


def test_session_survives_store_reopen(tmp_path, monkeypatch, clean_bytes):
    db = str(tmp_path / 'durable.sqlite')
    monkeypatch.setattr(main, 'DB_PATH', db)
    c = TestClient(main.app)
    raw = clean_bytes
    sid, _ = _session(c, raw)
    _put(c, sid, raw, 0, 64)
    # 模拟进程重启：新建客户端，会话与分块仍在
    c2 = TestClient(main.app)
    status = c2.get(f'/api/uploads/{sid}').json()
    assert status['stored_bytes'] == 64
    for off in range(64, len(raw), 64):
        _put(c2, sid, raw, off, 64)
    assert c2.post(f'/api/uploads/{sid}/finalize').status_code == 200
