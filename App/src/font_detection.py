import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

from settings import (
    CJK_REQUIRED_WEIGHTS,
    SOURCE_FONT_EXTENSIONS,
    WEIGHT_TARGETS,
)


# A sample of GB2312 level-1 (一 级) Han characters, the most frequently used
# simplified Chinese. Coverage of this sample is what decides whether a font
# can stand in for Microsoft YaHei: real Simplified Chinese fonts score 100%,
# Japanese/Korean fonts only 45-75% (they carry a Kanji subset but not the
# simplified forms), and Latin-only fonts 0%.
_CJK_PROBE_CODEPOINTS = frozenset({
    0x4E00, 0x4E01, 0x4E03, 0x4E08, 0x4E09, 0x4E0A, 0x4E0B, 0x4E0D, 0x4E0E,
    0x4E10, 0x4E12, 0x4E14, 0x4E15, 0x4E18, 0x4E19, 0x4E1A, 0x4E1B, 0x4E1C,
    0x4E1D, 0x4E20, 0x4E22, 0x4E24, 0x4E25, 0x4E26, 0x4E27, 0x4E28, 0x4E29,
    0x4E2A, 0x4E2B, 0x4E2C, 0x4E2D, 0x4E2E, 0x4E2F, 0x4E30, 0x4E31, 0x4E32,
    0x4E33, 0x4E34, 0x4E35, 0x4E36,
})
# Measured on Windows 11: YaHei/SimSun/Segoe UI score 100%, MS Gothic and
# Yu Gothic 75%, Malgun Gothic 45%, Arial/Tahoma 0%. The 85% gate keeps a
# wide margin below real Chinese fonts while excluding the CJK subset fonts.
_CJK_COVERAGE_MIN_RATIO = 0.85


@dataclass
class FontMetadata:
    path: Path
    extension: str
    family_name: str
    full_name: str
    subfamily_name: str
    weight_class: int = 400
    units_per_em: int = 2048
    is_italic: bool = False
    is_variable: bool = False
    is_monospace: bool = False
    covers_cjk: bool = False
    face_count: int = 1
    face_index: int = 0


WEIGHT_REGEX = [
    ("black_italic", re.compile(r"\b(?:black|heavy)\s*(?:italic|oblique)\b")),
    ("semibold_italic", re.compile(r"\b(?:semi\s*bold|semibold|demibold)\s*(?:italic|oblique)\b")),
    ("semilight_italic", re.compile(r"\bsemi\s*light\s*(?:italic|oblique)\b")),
    ("light_italic", re.compile(r"\b(?:light|thin|extra\s*light|extralight)\s*(?:italic|oblique)\b")),
    ("bold_italic", re.compile(r"\b(?:bold|extra\s*bold|extrabold)\s*(?:italic|oblique)\b")),
    ("semibold", re.compile(r"\b(?:semi\s*bold|semibold|demibold)\b")),
    ("semilight", re.compile(r"\bsemi\s*light\b")),
    ("black", re.compile(r"\b(?:black|heavy)\b")),
    ("light", re.compile(r"\b(?:light|thin|extra\s*light|extralight)\b")),
    ("bold", re.compile(r"\b(?:bold|extra\s*bold|extrabold)\b")),
    ("italic", re.compile(r"\b(?:italic|oblique)\b")),
    ("regular", re.compile(r"\b(?:regular|roman|book|normal)\b")),
]


def _tokenize(value):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    parts = re.split(r"[^a-z0-9]+", normalized.lower())
    return [part for part in parts if part]


def _normalized_text(*values: str) -> str:
    return " ".join(part for value in values for part in _tokenize(value))


def _clean_family_text(text: str) -> str:
    cleaned = text
    for _, pattern in WEIGHT_REGEX:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\b(?:medium|regular|roman|book|normal)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def classify_weight_from_strings(*values):
    haystack = _normalized_text(*values)
    for weight, pattern in WEIGHT_REGEX:
        if pattern.search(haystack):
            return weight
    return "regular"


def count_collection_faces(path: str | os.PathLike[str]) -> int:
    """Number of faces in a .ttc collection (1 for plain .ttf)."""
    font_path = Path(path)
    if font_path.suffix.lower() != ".ttc":
        return 1
    try:
        with open(font_path, "rb") as handle:
            return _read_collection_face_count(handle)
    except Exception:
        return 1


def _read_collection_face_count(handle) -> int:
    """Read the face count straight from the TTC header.

    Avoids a full fontTools parse (and a second file open) just to learn how
    many faces the collection has.
    """
    handle.seek(0)
    header = handle.read(12)
    if len(header) < 12 or header[:4] != b"ttcf":
        return 1
    return max(1, int.from_bytes(header[8:12], "big"))


def _font_covers_cjk(font) -> bool:
    try:
        cmap = font.getBestCmap()
    except Exception:
        return False
    if not cmap:
        return False
    hits = sum(1 for codepoint in _CJK_PROBE_CODEPOINTS if codepoint in cmap)
    return hits >= _CJK_COVERAGE_MIN_RATIO * len(_CJK_PROBE_CODEPOINTS)


def _font_signature(font_path: Path):
    """Identity of a file's current content, so cached metadata is dropped
    when the user swaps or rewrites a font at the same path."""
    try:
        stat = font_path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=1024)
def _inspect_font_cached(font_path_str: str, face_index: int, signature) -> FontMetadata:
    font_path = Path(font_path_str)
    extension = font_path.suffix.lower()
    if extension not in SOURCE_FONT_EXTENSIONS:
        raise ValueError(f"Unsupported font type: {font_path.suffix}")
    try:
        font = TTFont(font_path, fontNumber=face_index)
    except Exception as exc:
        raise ValueError(f"This font file is corrupted or unreadable: {font_path.name}") from exc

    try:
        for req in ("head", "name", "OS/2", "glyf", "loca"):
            if req not in font:
                if extension == ".ttc":
                    raise ValueError(
                        f"Only TrueType collections with glyf outlines are supported (.ttc): {font_path.name}"
                    )
                raise ValueError(f"This font file is corrupted or missing standard TrueType '{req}' table: {font_path.name}")

        try:
            weight_class = font["OS/2"].usWeightClass
        except Exception:
            weight_class = 400
        
        try:
            is_italic = bool(font["head"].macStyle & 0x2) or font["post"].italicAngle != 0
        except Exception:
            is_italic = False

        try:
            is_mono = bool(font["post"].isFixedPitch != 0)
        except Exception:
            is_mono = False

        if not is_mono:
            try:
                panose = getattr(font["OS/2"], "panose", None)
                if panose and getattr(panose, "bProportion", 0) == 9:
                    is_mono = True
            except Exception:
                pass

        try:
            family_name = font["name"].getBestFamilyName() or font_path.stem
        except Exception:
            family_name = font_path.stem

        try:
            full_name = font["name"].getBestFullName() or font_path.stem
        except Exception:
            full_name = font_path.stem

        try:
            subfamily_name = font["name"].getBestSubFamilyName() or ""
        except Exception:
            subfamily_name = ""

        if not is_mono:
            combined = f"{font_path.stem} {family_name} {full_name}".lower()
            if any(term in combined for term in ("mono", "code", "console", "typewriter")):
                is_mono = True

        try:
            units_per_em = font["head"].unitsPerEm
        except Exception:
            units_per_em = 1000

        covers_cjk = _font_covers_cjk(font)
        face_count = count_collection_faces(font_path)

        metadata = FontMetadata(
            path=font_path,
            extension=extension,
            family_name=family_name,
            full_name=full_name,
            subfamily_name=subfamily_name,
            weight_class=weight_class,
            units_per_em=units_per_em,
            is_italic=is_italic,
            is_variable="fvar" in font,
            is_monospace=is_mono,
            covers_cjk=covers_cjk,
            face_count=face_count,
            face_index=face_index,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"This font file is corrupted or unreadable: {font_path.name}") from exc
    finally:
        font.close()
    return metadata


def inspect_font(path: str | os.PathLike[str], face_index: int = 0) -> FontMetadata:
    font_path = Path(path).resolve()
    return _inspect_font_cached(str(font_path), face_index, _font_signature(font_path))


def classify_weight(path, metadata=None):
    metadata = metadata or inspect_font(path)
    if metadata.is_variable:
        return "variable"
    return classify_weight_from_strings(
        metadata.path.stem,
        metadata.family_name,
        metadata.full_name,
        metadata.subfamily_name,
    )


def infer_family_label_from_strings(*values):
    for value in values:
        normalized = _clean_family_text(_normalized_text(value))
        if normalized:
            return normalized
    return _clean_family_text(_normalized_text(*values))


def _family_label(path, metadata):
    return infer_family_label_from_strings(metadata.family_name, metadata.full_name, path.stem)


def _score_candidate(candidate, metadata, target_weight, target_value, target_italic, is_mono_target, primary_metadata, primary_root):
    score = 0

    if is_mono_target:
        if metadata.is_monospace:
            score += 20000
    else:
        if metadata.is_monospace == primary_metadata.is_monospace:
            score += 5000

    cand_name = _clean_family_text(_normalized_text(metadata.family_name, metadata.full_name, candidate.stem))
    if primary_root and primary_root in cand_name:
        score += 3000

    if metadata.is_italic == target_italic:
        score += 10000
    else:
        score -= 5000

    score -= abs(metadata.weight_class - target_value)

    norm_stem = _normalized_text(candidate.stem, metadata.subfamily_name)
    target_clean = target_weight.replace("consolas_", "").replace("_", " ")
    for kw in target_clean.split():
        if kw in norm_stem:
            score += 250

    return score


def detect_weight_overrides(primary_path, existing=None, weights=None, manual_overrides=None):
    from settings import get_system_weights
    active_weights = weights if weights is not None else get_system_weights()
    primary = Path(primary_path).resolve()
    folder = primary.parent
    primary_metadata = inspect_font(primary)
    primary_family = _family_label(primary, primary_metadata)
    primary_root = _tokenize(primary_family)[0] if _tokenize(primary_family) else ""
    existing = existing or {}
    manual_overrides = manual_overrides or {}
    detected = {}

    all_candidates = []
    variable_candidates = []
    for candidate in folder.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() not in SOURCE_FONT_EXTENSIONS:
            continue
        try:
            metadata = inspect_font(candidate)
        except Exception:
            continue
        if not metadata.is_variable:
            all_candidates.append((candidate, metadata))
        else:
            variable_candidates.append((candidate, metadata))

    if (primary, primary_metadata) not in all_candidates:
        all_candidates.append((primary, primary_metadata))

    for target_weight in active_weights:
        if target_weight == "variable":
            if target_weight in manual_overrides and manual_overrides[target_weight]:
                override_path = Path(manual_overrides[target_weight])
                if override_path.exists():
                    detected[target_weight] = str(override_path.resolve())
                    continue
            if target_weight in existing and existing[target_weight]:
                continue
            best_var = None
            if variable_candidates:
                for cand, meta in variable_candidates:
                    cand_name = _clean_family_text(_normalized_text(meta.family_name, meta.full_name, cand.stem))
                    if primary_root and primary_root in cand_name:
                        best_var = cand
                        break
                if not best_var:
                    best_var = variable_candidates[0][0]
            if best_var is not None:
                detected["variable"] = str(best_var.resolve())
            else:
                detected["variable"] = str(primary)
            continue
        if target_weight in manual_overrides and manual_overrides[target_weight]:
            override_path = Path(manual_overrides[target_weight])
            if override_path.exists():
                detected[target_weight] = str(override_path.resolve())
                continue

        if target_weight in existing and existing[target_weight]:
            continue

        if target_weight not in WEIGHT_TARGETS:
            continue

        target_value, target_italic = WEIGHT_TARGETS[target_weight]
        is_mono_target = target_weight.startswith("consolas_")

        if target_weight in CJK_REQUIRED_WEIGHTS:
            # YaHei slots may only be filled with fonts that render Simplified
            # Chinese; leave them unset rather than silently replacing
            # Microsoft YaHei with a Latin-only or Japanese/Korean font.
            eligible_candidates = [
                (candidate, metadata)
                for candidate, metadata in all_candidates
                if metadata.covers_cjk
            ]
            if not eligible_candidates:
                continue
        else:
            # Segoe UI / Consolas replacements are always built as a single
            # plain .ttf, so a collection must never be auto-selected here.
            eligible_candidates = [
                (candidate, metadata)
                for candidate, metadata in all_candidates
                if metadata.extension != ".ttc"
            ]
            if not eligible_candidates:
                eligible_candidates = [(primary, primary_metadata)]

        best_cand = None
        best_score = float("-inf")

        for candidate, metadata in eligible_candidates:
            score = _score_candidate(
                candidate,
                metadata,
                target_weight,
                target_value,
                target_italic,
                is_mono_target,
                primary_metadata,
                primary_root,
            )
            if score > best_score:
                best_score = score
                best_cand = candidate

        if best_cand is not None:
            detected[target_weight] = str(best_cand.resolve())
        elif target_weight not in CJK_REQUIRED_WEIGHTS:
            detected[target_weight] = str(primary)
        # CJK slots with no qualifying candidate stay unset.

    return detected



