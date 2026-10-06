from dataclasses import dataclass
from pathlib import Path

from paths import RuntimePaths
from checks import PreflightService
from font_detection import detect_weight_overrides, inspect_font
from operation import FontWorkflow
from app_state import ManagedStateStore
from settings import OPTIONAL_WEIGHTS, is_yahei_weight
from win_registry import WindowsFontRegistry


@dataclass
class SelectionState:
    paths: dict
    labels: dict


class FontWizardController:
    def __init__(self):
        self.paths = RuntimePaths.discover()
        self.paths.ensure_runtime_dirs()

        self.state_store = ManagedStateStore(self.paths.state_path)
        self.registry = WindowsFontRegistry()
        self.preflight = PreflightService(self.paths, self.registry, self.state_store)
        self.workflow = FontWorkflow(
            self.paths,
            self.registry,
            self.state_store,
            self.preflight,
        )
        system_weights = self.workflow._system_weights()
        self.selection = SelectionState(
            paths={w: None for w in system_weights},
            labels={w: "unset" for w in system_weights},
        )

    def refresh_preflight(self):
        report = self.preflight.collect()

        if report.managed_state_valid:
            state = self.state_store.load()
            if state:
                status = state.get("install", {}).get("status")
                if report.install_state == "clean" and status in (
                    "pending_reboot",
                    "pending_reboot_recovery",
                    "pending_reboot_apply",
                ):
                    state["install"]["status"] = "clean"
                    try:
                        self.state_store.save(state)
                    except OSError:
                        pass
                elif report.install_state == "managed" and status == "pending_reboot_apply":
                    state["install"]["status"] = "managed"
                    try:
                        self.state_store.save(state)
                    except OSError:
                        pass
                elif report.install_state == "partial" and status != "partial":
                    state["install"]["status"] = "partial"
                    try:
                        self.state_store.save(state)
                    except OSError:
                        pass

        return report

    def set_regular_font(self, path):
        metadata = inspect_font(path)
        if metadata.is_variable:
            raise ValueError(
                "Variable fonts cannot be selected as the primary static font. Select a static font (Regular) here, and assign your variable font to the Variable UI Font slot."
            )
        if metadata.extension == ".ttc":
            # The primary font feeds every Segoe UI / Consolas slot, all of
            # which are plain .ttf. Collections are only valid for the
            # Microsoft YaHei slots.
            raise ValueError(
                "TrueType Collections (.ttc) cannot be used as the primary font. "
                "Choose a plain .ttf file here, then assign your .ttc to the Microsoft YaHei cards."
            )

        resolved_path = str(Path(path).resolve())
        self.primary_font_path = resolved_path
        system_weights = self.workflow._system_weights()
        paths = self.selection.paths
        labels = self.selection.labels

        detected = detect_weight_overrides(resolved_path, weights=system_weights)

        # YaHei is replaced as a set: if any YaHei slot resolved to a
        # CJK-capable font, use that one font for all YaHei slots.
        yahei_source = next(
            (detected.get(w) for w in system_weights if w in OPTIONAL_WEIGHTS and detected.get(w)),
            None,
        )

        paths["regular"] = resolved_path
        labels["regular"] = "primary"
        for weight in system_weights:
            if weight == "regular":
                continue
            detected_path = detected.get(weight)
            if weight in OPTIONAL_WEIGHTS:
                # Microsoft YaHei slots are only filled when a CJK-capable
                # font was found in the same folder; otherwise they stay
                # unset and the original YaHei files are left untouched.
                paths[weight] = yahei_source
                labels[weight] = "auto-detected" if yahei_source else "unset"
            else:
                paths[weight] = detected_path or resolved_path
                labels[weight] = "auto-detected"

    def set_card_override(self, weight, path):
        metadata = inspect_font(path)
        if metadata.is_variable and weight != "variable":
            raise ValueError(
                "Variable fonts can only be assigned to the Variable UI slot. Choose a static .ttf file for static weights."
            )
        if is_yahei_weight(weight) and not metadata.covers_cjk:
            raise ValueError(
                f"{Path(path).name} does not contain Chinese (CJK) glyphs. "
                "Choose a Chinese-capable TrueType font (.ttf/.ttc) for Microsoft YaHei."
            )
        resolved_path = str(Path(path).resolve())
        self.selection.paths[weight] = resolved_path
        self.selection.labels[weight] = "manual"

        if is_yahei_weight(weight):
            # YaHei is replaced as a set, so assigning one slot assigns them
            # all; otherwise the system would be left half-replaced.
            from settings import YAHEI_WEIGHTS
            for yahei_weight in YAHEI_WEIGHTS:
                self.selection.paths[yahei_weight] = resolved_path
                self.selection.labels[yahei_weight] = "manual"

        if weight.startswith("consolas_"):
            from settings import CONSOLAS_WEIGHTS
            detected_mono = detect_weight_overrides(resolved_path, weights=CONSOLAS_WEIGHTS)
            for mono_weight in CONSOLAS_WEIGHTS:
                if mono_weight != weight and self.selection.labels.get(mono_weight) != "manual":
                    if mono_weight in detected_mono:
                        self.selection.paths[mono_weight] = detected_mono[mono_weight]
                        self.selection.labels[mono_weight] = "auto-detected"

    def reset_card_override(self, weight):
        primary_path = getattr(self, "primary_font_path", None) or self.selection.paths.get("regular")
        if not primary_path:
            return

        system_weights = self.workflow._system_weights()

        if weight == "regular":
            self.selection.paths["regular"] = primary_path
            self.selection.labels["regular"] = "primary"
            return

        if weight == "consolas_regular":
            from settings import CONSOLAS_WEIGHTS
            self.selection.labels["consolas_regular"] = "auto-detected"
            detected_from_primary = detect_weight_overrides(primary_path, weights=CONSOLAS_WEIGHTS)
            for mono_weight in CONSOLAS_WEIGHTS:
                if mono_weight == "consolas_regular" or self.selection.labels.get(mono_weight) != "manual":
                    self.selection.paths[mono_weight] = detected_from_primary.get(mono_weight) or primary_path
                    self.selection.labels[mono_weight] = "auto-detected"
            return

        if weight.startswith("consolas_"):
            from settings import CONSOLAS_WEIGHTS
            if self.selection.labels.get("consolas_regular") == "manual" and self.selection.paths.get("consolas_regular"):
                mono_root = self.selection.paths["consolas_regular"]
                detected_mono = detect_weight_overrides(mono_root, weights=CONSOLAS_WEIGHTS)
                self.selection.paths[weight] = detected_mono.get(weight) or mono_root
            else:
                detected_from_primary = detect_weight_overrides(primary_path, weights={weight: system_weights.get(weight)})
                self.selection.paths[weight] = detected_from_primary.get(weight) or primary_path
            self.selection.labels[weight] = "auto-detected"
            return

        if is_yahei_weight(weight):
            from settings import YAHEI_WEIGHTS
            detected_cjk = detect_weight_overrides(primary_path, weights=YAHEI_WEIGHTS)
            yahei_source = next(
                (detected_cjk.get(w) for w in YAHEI_WEIGHTS if detected_cjk.get(w)),
                None,
            )
            for yahei_weight in YAHEI_WEIGHTS:
                self.selection.paths[yahei_weight] = yahei_source
                self.selection.labels[yahei_weight] = (
                    "auto-detected" if yahei_source else "unset"
                )
            return

        detected = detect_weight_overrides(primary_path, weights={weight: system_weights.get(weight)})
        self.selection.paths[weight] = detected.get(weight) or primary_path
        self.selection.labels[weight] = "auto-detected"

    def apply(self, progress=None):
        return self.workflow.apply(
            self.selection.paths,
            self.selection.labels,
            progress=progress,
        )

    def restore(self, progress=None):
        return self.workflow.restore(progress=progress)

