"""签一枚 Supabase 角色 JWT（HS256）。

iss 必须逐字等于 API_EXTERNAL_URL：官方 compose 把 GOTRUE_JWT_ISSUER 接在那里，
GoTrue 校验是字符串比较而不是按域授权。所以换域名必须重新签 anon / service_role
两枚 key，否则 GoTrue 会拒收它们。

gen_secrets.sh（首次生成）和 set_domain.sh（换域名）都调用这里，
claim 只在这一个文件里维护。

用法: python3 jwt_key.py <secret> <issuer> <role>
"""

import base64
import hashlib
import hmac
import json
import sys
import time

secret, issuer, role = sys.argv[1], sys.argv[2], sys.argv[3]


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


now = int(time.time())
signing = (
    b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    + "."
    + b64(
        json.dumps(
            {"role": role, "iss": issuer, "iat": now, "exp": now + 10 * 365 * 86400},
            separators=(",", ":"),
        ).encode()
    )
)
print(signing + "." + b64(hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest()))
