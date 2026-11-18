"""断点续传与两级结果边界的 HTTP 入口。

路由总览：
  POST   /api/uploads                 创建上传会话（声明 total_size 与整文件 sha256）
  PUT    /api/uploads/{sid}/chunks/{offset}  上传/重传单个分块（原始字节，幂等）
  GET    /api/uploads/{sid}           会话状态：缺口、连续前缀、是否可终结
  GET    /api/uploads/{sid}/preview   临时风险范围（仅连续前缀，P- 批号，不可归档）
  POST   /api/uploads/{sid}/finalize  拼齐并校验完整原件后生成正式批次

取舍：临时预览回答“现在已经能圈出多大风险范围”，正式批次回答“审计认可的
损失是多少”。两者来源不同、批号前缀不同、存储表不同，互不覆盖。
"""
import hashlib
import json
import uuid
from fastapi import APIRouter, Request, Body
from fastapi.templating import Jinja2Templates
from app.config import ROOT, MAX_BYTES, META
from app.errors import BusinessError
from app import store, uploads
from app.engine import evaluate, evaluate_provisional

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _db(request: Request):
    from app.main import DB_PATH
    return DB_PATH


@router.post('/api/uploads')
def create_upload(request: Request, payload: dict = Body(...)):
    total_size = payload.get('total_size')
    sha = payload.get('sha256', '')
    filename = str(payload.get('filename', 'upload.csv'))[:200]
    context_overrides = payload.get('overrides', {})
    if not isinstance(total_size, int) or total_size <= 0:
        raise BusinessError('BAD_SIZE', 'total_size 须为正整数')
    if total_size > MAX_BYTES:
        raise BusinessError('TOO_LARGE', '文件超过2MB', 413)
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha.lower()):
        raise BusinessError('BAD_DIGEST', 'sha256 须为64位十六进制')
    if not isinstance(context_overrides, dict):
        raise BusinessError('BAD_CONTEXT', '参数须为对象')
    # 上下文在创建会话时冻结，续传期间不可改，保证“同字节+同上下文→同批号”。
    context = json.loads((ROOT / 'sample_data/context.json').read_text())
    context.update(context_overrides)
    session_id = uuid.uuid4().hex
    uploads.create_session(_db(request), session_id, total_size, sha.lower(), filename, context)
    return uploads.session_status(_db(request), session_id)


@router.put('/api/uploads/{session_id}/chunks/{offset}')
async def upload_chunk(request: Request, session_id: str, offset: int):
    data = await request.body()
    chunk_sha = hashlib.sha256(data).hexdigest()
    declared = request.headers.get('X-Chunk-SHA256', '').lower()
    if declared and declared != chunk_sha:
        raise BusinessError('CHUNK_DIGEST_MISMATCH', '分块摘要与声明不符')
    session = uploads.get_session(_db(request), session_id)
    result = uploads.put_chunk(
        _db(request), session_id, offset, len(data), chunk_sha, data, session['total_size']
    )
    return {'session_id': session_id, 'offset': offset, 'length': len(data),
            'sha256': chunk_sha, **result}


@router.get('/api/uploads/{session_id}')
def upload_status(request: Request, session_id: str):
    return uploads.session_status(_db(request), session_id)


@router.get('/api/uploads/{session_id}/preview')
def upload_preview(request: Request, session_id: str):
    session = uploads.get_session(_db(request), session_id)
    prefix = uploads.prefix_bytes(_db(request), session_id)
    if not prefix:
        raise BusinessError('NO_PREFIX', '尚无连续字节，无法生成预览', 409)
    provenance = {
        'session_id': session_id,
        'filename': session['filename'],
        'expected_sha256': session['sha256'],
        'total_size': session['total_size'],
        'contiguous_bytes': len(prefix),
        'basis': 'contiguous-prefix-from-offset-0',
        'warning': '临时结果：基于未传完的连续前缀，仅供接续期间圈定风险范围，不可归档',
    }
    state = evaluate_provisional(prefix, session['context'], provenance)
    return state


@router.get('/uploads/preview')
def preview_page(request: Request, session_id: str):
    session = uploads.get_session(_db(request), session_id)
    prefix = uploads.prefix_bytes(_db(request), session_id)
    if not prefix:
        raise BusinessError('NO_PREFIX', '尚无连续字节，无法生成预览', 409)
    provenance = {
        'session_id': session_id,
        'filename': session['filename'],
        'expected_sha256': session['sha256'],
        'total_size': session['total_size'],
        'contiguous_bytes': len(prefix),
        'basis': 'contiguous-prefix-from-offset-0',
        'warning': '临时结果：基于未传完的连续前缀，仅供接续期间圈定风险范围，不可归档',
    }
    state = evaluate_provisional(prefix, session['context'], provenance)
    status = uploads.session_status(_db(request), session_id)
    return templates.TemplateResponse(
        request=request, name='provisional.html',
        context={'meta': META, 'key': 'intake', 'state': state,
                 'view': state['views']['intake'], 'up': status})


@router.post('/api/uploads/{session_id}/finalize')
def upload_finalize(request: Request, session_id: str):
    session, raw = uploads.assembled_bytes(_db(request), session_id)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != session['sha256']:
        # 完整原件校验不过：明确拒绝，绝不降级成正式损失，临时预览仍可看。
        raise BusinessError('DIGEST_MISMATCH',
                            f'整文件摘要不符（期望 {session["sha256"][:12]}…，实得 {actual[:12]}…），拒绝采信')
    if len(raw) != session['total_size']:
        raise BusinessError('SIZE_MISMATCH', '整文件字节数与声明不符，拒绝采信')
    # 同一完整原件 + 同一冻结上下文 + 同一规则版本 ⇒ 同一摘要 ⇒ 同一批号，
    # store.save 的 INSERT OR IGNORE 保证重复上传只复用同一正式批，不混入新批。
    # source 在评估前确定，使 manifest 摘要与整文件提交路径保持同一批号规则。
    state = evaluate({'dataset': 'dirty', 'overrides': {}}, raw_override=raw)
    state['manifest']['upload_session'] = session_id
    state['manifest']['upload_filename'] = session['filename']
    saved = store.save(_db(request), state)
    uploads.mark_finalized(_db(request), session_id, saved['manifest']['run_id'])
    return saved
