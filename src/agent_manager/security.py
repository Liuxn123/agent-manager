from __future__ import annotations

import re
from typing import Any

from .domain import UserError

SENSITIVE = re.compile(r"(?i)(password|passphrase|secret|token|api[_-]?key|authorization|credential)")


def redact(text: str) -> str:
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", "[已隐藏私钥]", text)
    text = re.sub(r"(?i)(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})", "[已隐藏凭据]", text)
    text = re.sub(r"(?i)((?:password|passphrase|token|api[_-]?key|authorization)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[已隐藏]@", text)
    return text[:40000]


def safe_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[已隐藏]" if SENSITIVE.search(str(key)) and not str(key).endswith(("_present", "_count", "_available", "_restored")) else safe_result(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_result(item) for item in value[:500]]
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (int, float, bool, type(None))):
        return value
    return redact(str(value))


class SecretStore:
    service = "personal-agent-manager"

    def _backend(self):
        import keyring
        backend = keyring.get_keyring()
        module = type(backend).__module__
        if "keyrings.alt" in module or backend.priority <= 0:
            raise UserError("系统安全凭据存储不可用，请临时输入口令，或配置系统钥匙串。")
        return keyring

    def save(self, identity: str, value: str) -> None:
        try:
            self._backend().set_password(self.service, identity, value)
        except UserError:
            raise
        except Exception as exc:
            raise UserError("无法保存到系统凭据存储。请检查系统钥匙串是否已解锁。") from exc

    def get(self, identity: str) -> str | None:
        try:
            return self._backend().get_password(self.service, identity)
        except UserError:
            raise
        except Exception as exc:
            raise UserError("无法读取系统凭据存储。") from exc

    def remove(self, identity: str) -> None:
        try:
            self._backend().delete_password(self.service, identity)
        except UserError:
            raise
        except Exception as exc:
            raise UserError("无法移除系统凭据，请检查凭据是否存在。") from exc
