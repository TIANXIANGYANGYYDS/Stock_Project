from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from app.core.config import PROJECT_ROOT

ARCHIVE_ROOT = PROJECT_ROOT / ".local/short_sentiment/classifications"

class ThemeCatalog:
    def __init__(self, path: Path | None = None, *, groups: dict | None = None):
        payload = (path or PROJECT_ROOT / "docs/短线题材分类匹配库.json").read_bytes() if groups is None else json.dumps(groups, ensure_ascii=False, sort_keys=True).encode()
        groups = json.loads(payload)
        self._payload = payload
        self.version = hashlib.sha256(payload).hexdigest()
        self.themes = {}
        self.names = {}
        self.by_code: dict[str, list[str]] = {}
        for name, group in groups.items():
            theme_id = "theme_" + hashlib.sha256(name.encode()).hexdigest()[:12]
            codes = sorted({row["股票代码"] for row in group["成分股"]})
            self.names.update({row["股票代码"]: row["股票名称"] for row in group["成分股"]})
            self.themes[theme_id] = {"theme_id": theme_id, "name": name, "member_count": len(codes)}
            for code in codes:
                self.by_code.setdefault(code, []).append(theme_id)
        self.themes["unclassified"] = {"theme_id": "unclassified", "name": "未分类", "member_count": 0}

    def ids(self, code: str) -> list[str]:
        return self.by_code.get(code, ["unclassified"])

    def archive(self):
        """Keep exact catalog bytes so a restart can resume the day's version."""
        ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        target = ARCHIVE_ROOT / (self.version + ".json")
        if target.exists():
            return
        with tempfile.NamedTemporaryFile(dir=ARCHIVE_ROOT, delete=False) as output:
            output.write(self._payload)
            temporary = Path(output.name)
        temporary.replace(target)

    @classmethod
    def restore(cls, version):
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("题材版本格式错误")
        path = ARCHIVE_ROOT / (version + ".json")
        if not path.is_file():
            raise ValueError("当天题材库已变更且旧版本未归档，不能静默改写当日分类")
        result = cls(path=path)
        if result.version != version:
            raise ValueError("归档题材库哈希不符")
        return result
