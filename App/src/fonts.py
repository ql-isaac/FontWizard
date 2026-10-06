from dataclasses import dataclass, field
from pathlib import Path

from font_detection import inspect_font
from settings import (
    OPTIONAL_WEIGHTS,
    REGISTRY_NAMES,
    get_system_weights,
    is_yahei_weight,
    mod_filename,
)

@dataclass
class FontPlanEntry:
    weight: str
    source_path: Path
    source_label: str
    system_filename: str
    registry_name: str
    generated_filename: str
    family_name: str
    full_name: str


@dataclass
class ValidationSummary:
    entries: list[FontPlanEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors and bool(self.entries)


def resolve_font_selection(selection, weights=None):
    active_weights = weights if weights is not None else get_system_weights()
    regular_path = selection.get("regular")
    if not regular_path:
        return {}

    resolved = {}
    mono_fallback = selection.get("consolas_regular") or regular_path

    # Microsoft YaHei is replaced as a set: replacing only some of its
    # weights would leave the system in a mixed state, so any single YaHei
    # assignment makes all YaHei slots use that font.
    yahei_source = next(
        (selection.get(weight) for weight in OPTIONAL_WEIGHTS if selection.get(weight)),
        None,
    )

    for weight in active_weights:
        chosen = selection.get(weight)
        if weight in OPTIONAL_WEIGHTS:
            if yahei_source:
                resolved[weight] = yahei_source
        elif weight.startswith("consolas_"):
            resolved[weight] = chosen or mono_fallback
        else:
            resolved[weight] = chosen or regular_path
    return resolved


def validate_selection(selection, source_labels=None, weights=None):
    active_weights = weights if weights is not None else get_system_weights()
    summary = ValidationSummary()
    resolved = resolve_font_selection(selection, active_weights)
    if not resolved:
        summary.errors.append("Choose a regular font to continue.")
        return summary

    metadata_cache = {}
    ui_family_names = set()
    mono_family_names = set()
    cjk_family_names = set()
    variable_rejections = set()
    invalid_source_rejections = set()
    ttc_rejections = set()
    cjk_rejections = set()
    source_labels = source_labels or {}

    for weight, source in resolved.items():
        source_path = Path(source)
        if not source_path.exists():
            summary.errors.append(f"Font file not found for {weight.replace('_', ' ')}.")
            continue
        cache_key = str(source_path.resolve())

        if source_path.suffix.lower() == ".ttc" and not is_yahei_weight(weight):
            if cache_key not in ttc_rejections:
                summary.errors.append(
                    "TrueType Collections (.ttc) can only be used for the Microsoft YaHei slots. "
                    f"Choose a plain .ttf file for {weight.replace('_', ' ').title()}."
                )
                ttc_rejections.add(cache_key)
            continue
        if cache_key not in metadata_cache:
            try:
                metadata_cache[cache_key] = inspect_font(source_path)
            except ValueError as exc:
                if cache_key not in invalid_source_rejections:
                    ext = source_path.suffix.lower()
                    if ext == ".otf":
                        summary.errors.append(
                            f"OpenType (.otf) fonts are not supported. "
                            f"System font replacement requires TrueType (.ttf) files. "
                            f"File: {source_path.name}"
                        )
                    elif ext != ".ttf":
                        summary.errors.append(
                            f"This file type ({ext}) is not supported. "
                            f"Choose a .ttf font file."
                        )
                    else:
                        summary.errors.append(str(exc))
                    invalid_source_rejections.add(cache_key)
                continue

        metadata = metadata_cache[cache_key]
        if metadata.is_variable and weight != "variable":
            if cache_key not in variable_rejections:
                summary.errors.append(
                    "Variable fonts can only be used for the Variable UI slot. Choose a static .ttf file for static weights."
                )
                variable_rejections.add(cache_key)
            continue

        if is_yahei_weight(weight):
            if not metadata.covers_cjk:
                if cache_key not in cjk_rejections:
                    summary.errors.append(
                        f"{source_path.name} does not contain Chinese (CJK) glyphs and cannot replace Microsoft YaHei. "
                        "Choose a Chinese-capable TrueType font (e.g. 微软雅黑 / 思源黑体 / MiSans)."
                    )
                    cjk_rejections.add(cache_key)
                continue
            cjk_family_names.add(metadata.family_name)
        elif weight.startswith("consolas_"):
            mono_family_names.add(metadata.family_name)
        else:
            ui_family_names.add(metadata.family_name)

        system_filename = active_weights[weight]
        summary.entries.append(
            FontPlanEntry(
                weight=weight,
                source_path=metadata.path,
                source_label=source_labels.get(weight, "resolved"),
                system_filename=system_filename,
                registry_name=REGISTRY_NAMES[weight],
                generated_filename=mod_filename(system_filename, metadata.path),
                family_name=metadata.family_name,
                full_name=metadata.full_name,
            )
        )

    if (
        len(ui_family_names) > 1
        or len(mono_family_names) > 1
        or len(cjk_family_names) > 1
    ):
        summary.warnings.append(
            "Selected styles come from multiple font families. Review them carefully before applying."
        )

    return summary

