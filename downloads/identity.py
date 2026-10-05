import hashlib
import json
from decimal import Decimal, InvalidOperation


def chapter_number(value):
    text = str(value)
    try:
        number = Decimal(text.replace("-", "."))
        if number.is_finite():
            return format(number.normalize(), "f")
    except InvalidOperation:
        pass
    return text


def identity(source, series, chapter, group):
    components = [str(source), str(series), chapter_number(chapter), str(group)]
    if any(not v or len(v) > 2048 for v in components):
        raise ValueError("Invalid chapter identity.")
    canonical = json.dumps([1] + components, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
