from importlib import import_module
from copy import deepcopy
import json, time, base64, hashlib
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest, provisional_manifest

def _load_context(payload, snapshot):
    if snapshot is not None:
        return deepcopy(snapshot['context'])
    context=json.loads((ROOT/'sample_data/context.json').read_text())
    context.update(deepcopy(payload.get('overrides',{})))
    return context

def evaluate(payload=None, until=7, snapshot=None, raw_override=None, provisional=False, provenance=None):
    """正式评估：raw_override 仅来自上传会话终结时拼齐的完整原件并经摘要校验。"""
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    if raw_override is not None:
        raw=raw_override
    elif snapshot is not None:
        raw=base64.b64decode(snapshot['bytes_b64'],validate=True)
    elif 'csv_text' in payload:
        raw=str(payload['csv_text']).encode()
    else:
        raw=(ROOT/'sample_data'/f'{dataset}.csv').read_bytes()
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    context=_load_context(payload,snapshot)
    start=time.perf_counter()
    state={'raw_bytes':raw,'context':context,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset,
           'provisional':False}
    for index,key in enumerate(MODULES,1):
        if index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'context':deepcopy(context)}
    state.pop('raw_bytes',None)
    state['manifest']=make_manifest(digest(raw,context),dataset,time.perf_counter()-start)
    return state

def evaluate_provisional(raw_prefix, context, provenance):
    """临时风险预览：只允许吃“从偏移0连续的最长前缀”。

    与正式评估共用同一套模块规则，仅在采信入口把网络截断的末行显式隔离
    （TRUNCATED_TAIL），并在 manifest 上打上 provisional 标记。结果批号带
    P- 前缀，永不进入正式 runs 表，也不能复核或导出。
    """
    if len(raw_prefix)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    start=time.perf_counter()
    state={'raw_bytes':raw_prefix,'context':deepcopy(context),'records':[],'quarantine':[],
           'counts':{},'views':{},'source':'upload-session','provisional':True,
           'provenance':deepcopy(provenance)}
    import_module('app.modules.intake.rules').apply(state, provisional=True)
    for index,key in enumerate(MODULES[1:],2):
        import_module(f'app.modules.{key}.rules').apply(state)
    state.pop('raw_bytes',None)
    prefix_digest=hashlib.sha256(raw_prefix).hexdigest()
    state['manifest']=provisional_manifest(prefix_digest, provenance, time.perf_counter()-start)
    return state
