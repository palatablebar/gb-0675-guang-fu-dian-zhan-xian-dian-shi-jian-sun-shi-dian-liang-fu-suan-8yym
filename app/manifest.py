import hashlib, json
from datetime import datetime, timezone
from app.config import RULE_VERSION

def digest(raw, context):
    return hashlib.sha256(raw + json.dumps(context,ensure_ascii=False,sort_keys=True).encode() + RULE_VERSION.encode()).hexdigest()

def make_manifest(source_digest, source, elapsed):
    return {'run_id':source_digest[:16], 'sha256':source_digest, 'rule_version':RULE_VERSION,
            'source':source, 'generated_at':datetime.now(timezone.utc).isoformat(),
            'elapsed_ms':round(elapsed*1000,3), 'timezone':'Asia/Shanghai',
            'tier':'official', 'provisional':False}

def provisional_manifest(prefix_digest, provenance, elapsed):
    # P- 前缀在批号层面与正式批物理隔离；正式 run_id 是完整原件摘要前16位，
    # 不会与临时批号碰撞，store/review/export 也一律拒绝临时批号。
    return {'run_id':'P-'+prefix_digest[:14], 'sha256':prefix_digest, 'rule_version':RULE_VERSION,
            'source':'upload-session', 'generated_at':datetime.now(timezone.utc).isoformat(),
            'elapsed_ms':round(elapsed*1000,3), 'timezone':'Asia/Shanghai',
            'tier':'provisional', 'provisional':True, 'provenance':provenance}
