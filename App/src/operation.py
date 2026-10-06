import shutil
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTFont, TTCollection

from font_cache import refresh_windows_font_cache
from font_generation import build_font, build_ttc_font
from fonts import validate_selection
from operation_files import (
    backup_canonical_fonts,
    cleanup_orphaned_pending_ops,
    purge_system_font_cache,
    schedule_canonical_replacement,
    schedule_canonical_restore,
)
from settings import FONTS_DIR, default_registry_targets
from app_state import hash_file, iso_now


@dataclass
class OperationResult:
    success: bool
    message: str
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class FontWorkflow:
    def __init__(
        self,
        paths,
        registry,
        state_store,
        preflight,
        identity_fonts_root=None,
        active_fonts_root=None,
    ):
        self.paths = paths
        self.registry = registry
        self.state_store = state_store
        self.preflight = preflight
        self.identity_fonts_root = Path(identity_fonts_root or FONTS_DIR)
        self.active_fonts_root = Path(active_fonts_root or FONTS_DIR)

    def _system_weights(self):
        from settings import get_system_weights
        return get_system_weights(self.identity_fonts_root)

    def _system_font_files(self, entry_weights=None):
        """System files to back up / schedule.

        With entry_weights given, only the files behind those weights (plus the
        mono companions) are returned; otherwise every known system font is
        included. Restoring must pass the weights that were actually replaced,
        so untouched files are never hashed or staged for replacement.
        """
        from settings import get_existing_mono_companions
        weights = self._system_weights()
        if entry_weights is not None:
            selected = {weights[w] for w in entry_weights if w in weights}
        else:
            selected = set(weights.values())
        files = list(dict.fromkeys(selected))
        target_dir = self.identity_fonts_root if self.identity_fonts_root.exists() else self.active_fonts_root
        for companion_file in get_existing_mono_companions(target_dir):
            if companion_file not in files:
                files.append(companion_file)
        return files

    def validate(self, selection, source_labels=None):
        return validate_selection(selection, source_labels, weights=self._system_weights())

    def apply(self, selection, source_labels=None, progress=None):
        report = self.preflight.collect()
        if not report.is_supported:
            return OperationResult(False, "Font Wizard supports Windows 10 (build 10240+) and Windows 11.", report.issues, report.warnings)

        if not report.is_admin:
            return OperationResult(False, "Run Font Wizard as Administrator before applying fonts.", report.issues, report.warnings)

        if report.install_state == "pending_reboot_recovery":
            return OperationResult(
                False,
                "Restart Windows before applying another font.",
                ["A previous recovery still has file updates waiting for restart."],
                report.warnings,
            )

        summary = self.validate(selection, source_labels)
        if not summary.ok:
            return OperationResult(False, summary.errors[0] if summary.errors else "The selected font cannot be used.", summary.errors, summary.warnings)

        stage_dir = self.paths.make_temp_dir("fontwizard-build-")
        # Back up only the system files that are actually going to be
        # replaced (plus mono companions). Optional slots that were left
        # unset, such as Microsoft YaHei, are not touched at all.
        system_files = self._system_font_files(
            entry_weights=[entry.weight for entry in summary.entries]
        )

        try:
            self._emit(progress, 5, "Backing up original Windows system fonts...")
            backup_warnings, backed_count = backup_canonical_fonts(self, system_files)
            if backed_count < len(system_files):
                backup_warnings.append(
                    f"Note: {backed_count} of {len(system_files)} original fonts were backed up."
                )

            self._emit(progress, 15, "Cleaning up old pending operations...")
            pending_cleanup_warnings, _ = cleanup_orphaned_pending_ops(self)

            self._emit(progress, 30, "Compiling custom TrueType font weights...")
            artifacts = build_artifacts(self, summary.entries, stage_dir)

            self._emit(progress, 75, "Scheduling physical in-place replacement on boot...")
            manifest, schedule_warnings = schedule_canonical_replacement(self, artifacts)

            self._emit(progress, 88, "Purging system font caches...")
            cache_warnings = purge_system_font_cache()
            refresh_windows_font_cache()

            # Ensure Segoe WPC substitute is set in registry
            try:
                self.registry.ensure_font_substitutes()
            except Exception:
                pass

            self._emit(progress, 95, "Saving installation state...")
            state = self.state_store.load_or_empty()
            state["install"] = {
                "status": "pending_reboot_apply",
                "fonts": manifest,
                "backed_up_count": backed_count,
                "applied_at": iso_now(),
                "restored_at": None,
            }
            state["last_action"] = {
                "kind": "apply",
                "status": "success",
                "timestamp": iso_now(),
                "details": "Scheduled physical in-place replacement on boot.",
            }
            self.state_store.save(state)

            self._emit(progress, 100, "Font setup complete!")
            all_warnings = [
                *summary.warnings,
                *backup_warnings,
                *pending_cleanup_warnings,
                *schedule_warnings,
                *cache_warnings,
            ]
            return OperationResult(
                True,
                "Font changes scheduled. Restart Windows now to finish applying your font.",
                warnings=all_warnings,
            )
        except Exception as exc:
            state = self.state_store.load_or_empty()
            state["last_action"] = {
                "kind": "apply",
                "status": "failed",
                "timestamp": iso_now(),
                "details": str(exc),
            }
            self.state_store.save(state)
            return OperationResult(False, f"Failed to apply font changes: {exc}", [str(exc)], summary.warnings)
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)

    def _replaced_font_files(self):
        """System files recorded as replaced by the last apply.

        Falls back to every known system font when no usable manifest exists
        (for example after a manual repair), so restore still does its job.
        """
        try:
            state = self.state_store.load() or {}
            fonts = (state.get("install") or {}).get("fonts") or {}
            names = {
                str(entry["system_filename"])
                for entry in fonts.values()
                if isinstance(entry, dict) and entry.get("system_filename")
            }
            if names:
                target_dir = (
                    self.identity_fonts_root
                    if self.identity_fonts_root.exists()
                    else self.active_fonts_root
                )
                from settings import get_existing_mono_companions
                for companion in get_existing_mono_companions(target_dir):
                    names.add(companion)
                return sorted(names)
        except Exception:
            pass
        return self._system_font_files()

    def restore(self, progress=None):
        report = self.preflight.collect()
        if not report.is_admin:
            return OperationResult(False, "Run Font Wizard as Administrator before restoring fonts.", report.issues, report.warnings)

        system_files = self._replaced_font_files()

        try:
            self._emit(progress, 10, "Cleaning up stale pending files...")
            pending_cleanup_warnings, _ = cleanup_orphaned_pending_ops(self)

            self._emit(progress, 35, "Scheduling restore of original fonts on boot...")
            scheduled_count, restore_warnings = schedule_canonical_restore(self, system_files)
            if scheduled_count == 0:
                return OperationResult(
                    False,
                    "No authentic backup font files were found. You can restore original Windows fonts by running 'sfc /scannow' or 'DISM /Online /Cleanup-Image /RestoreHealth' in an Administrator terminal.",
                    restore_warnings,
                )

            self._emit(progress, 75, "Purging system font caches...")
            cache_warnings = purge_system_font_cache()
            refresh_windows_font_cache()

            # Clean up FontSubstitutes overrides on restore
            try:
                self.registry.remove_font_substitutes()
            except Exception:
                pass

            self._emit(progress, 92, "Saving restore state...")
            state = self.state_store.load_or_empty()
            state["install"] = {
                "status": "pending_reboot_recovery",
                "fonts": {},
                "applied_at": state.get("install", {}).get("applied_at"),
                "restored_at": iso_now(),
            }
            state["last_action"] = {
                "kind": "restore",
                "status": "success",
                "timestamp": iso_now(),
                "details": f"Scheduled restore of {scheduled_count} original Microsoft fonts on boot.",
            }
            self.state_store.save(state)

            self._emit(progress, 100, "Font restore scheduled!")
            all_warnings = [
                *pending_cleanup_warnings,
                *restore_warnings,
                *cache_warnings,
            ]
            return OperationResult(
                True,
                f"Original Windows fonts ({scheduled_count}/{len(system_files)}) scheduled for restore. Restart Windows now to finish.",
                warnings=all_warnings,
            )
        except Exception as exc:
            return OperationResult(False, f"Font restore failed: {exc}", [str(exc)])

    def _emit(self, callback, value, message):
        if callback:
            callback(value, message)


def _face_identity_snapshot(font):
    """The identity fields a replacement font must match on, so that Windows
    still resolves the original family/weight names to the built file."""
    return {
        "family_name": font["name"].getBestFamilyName(),
        "full_name": font["name"].getBestFullName(),
        "subfamily_name": font["name"].getBestSubFamilyName(),
        "mac_style": font["head"].macStyle,
        "os2_version": font["OS/2"].version,
        "weight_class": font["OS/2"].usWeightClass,
        "width_class": font["OS/2"].usWidthClass,
        "fs_selection": font["OS/2"].fsSelection,
        "italic_angle": font["post"].italicAngle,
    }


def _verify_build_output(output_path: Path, segoe_path: Path):
    built_font = None
    donor_font = None
    try:
        try:
            built_font = TTFont(output_path)
        except Exception as exc:
            raise RuntimeError(f"Built font could not be reopened: {output_path.name}") from exc

        try:
            donor_font = TTFont(segoe_path)
        except Exception as exc:
            raise RuntimeError(f"Could not inspect donor font: {segoe_path.name}") from exc

        expected = _face_identity_snapshot(donor_font)
        actual = _face_identity_snapshot(built_font)

        for key, expected_value in expected.items():
            if actual[key] != expected_value:
                if ("fvar" in built_font or "fvar" in donor_font) and key in ("os2_version", "weight_class", "fs_selection"):
                    continue
                raise RuntimeError(
                    f"Built font identity check failed for {output_path.name}: "
                    f"{key} was {actual[key]!r}, expected {expected_value!r}."
                )
    finally:
        if built_font is not None:
            built_font.close()
        if donor_font is not None:
            donor_font.close()


def _verify_collection_output(output_path: Path, donor_ttc_path: Path):
    built_collection = None
    donor_collection = None
    try:
        try:
            built_collection = TTCollection(str(output_path))
        except Exception as exc:
            raise RuntimeError(f"Built collection could not be reopened: {output_path.name}") from exc
        try:
            donor_collection = TTCollection(str(donor_ttc_path))
        except Exception as exc:
            raise RuntimeError(f"Could not inspect donor collection: {donor_ttc_path.name}") from exc

        if len(built_collection.fonts) != len(donor_collection.fonts):
            raise RuntimeError(
                f"Built collection face count mismatch for {output_path.name}: "
                f"{len(built_collection.fonts)} vs {len(donor_collection.fonts)} donor faces."
            )

        for index, (built_font, donor_font) in enumerate(
            zip(built_collection.fonts, donor_collection.fonts)
        ):
            expected = _face_identity_snapshot(donor_font)
            actual = _face_identity_snapshot(built_font)
            for key, expected_value in expected.items():
                if actual[key] != expected_value:
                    raise RuntimeError(
                        f"Built font identity check failed for {output_path.name} face {index}: "
                        f"{key} was {actual[key]!r}, expected {expected_value!r}."
                    )
    finally:
        if built_collection is not None:
            built_collection.close()
        if donor_collection is not None:
            donor_collection.close()


def build_artifacts(workflow, entries, stage_dir):
    artifacts = {}
    mono_regular_entry = None
    for entry in entries:
        if entry.weight == "consolas_regular":
            mono_regular_entry = entry

        backup_segoe = workflow.paths.backup_root / entry.system_filename
        if backup_segoe.exists() and backup_segoe.stat().st_size > 50_000:
            segoe_path = backup_segoe
        else:
            segoe_path = workflow.identity_fonts_root / entry.system_filename
        output_path = stage_dir / entry.system_filename

        if entry.system_filename.lower() == "seguivar.ttf":
            from font_generation import build_variable_font
            build_variable_font(entry.source_path, segoe_path, output_path)
            _verify_build_output(output_path, segoe_path)
        elif entry.system_filename.lower().endswith(".ttc"):
            build_ttc_font(entry.source_path, segoe_path, output_path)
            _verify_collection_output(output_path, segoe_path)
        else:
            build_font(entry.source_path, segoe_path, output_path)
            _verify_build_output(output_path, segoe_path)

        artifacts[entry.weight] = {
            "weight": entry.weight,
            "registry_name": entry.registry_name,
            "system_filename": entry.system_filename,
            "generated_filename": entry.system_filename,
            "source_path": str(entry.source_path),
            "family_name": entry.family_name,
            "full_name": entry.full_name,
            "staged_path": str(output_path),
            "hash": hash_file(output_path),
        }

    if mono_regular_entry is not None:
        from font_detection import inspect_font
        from settings import get_existing_mono_companions
        try:
            mono_meta = inspect_font(mono_regular_entry.source_path)
            is_mono = getattr(mono_meta, "is_monospace", False) or mono_regular_entry.source_label == "manual"
        except Exception:
            is_mono = True

        if is_mono:
            companions = get_existing_mono_companions(workflow.identity_fonts_root)
            for companion_file, companion_reg_name in companions.items():
                backup_comp = workflow.paths.backup_root / companion_file
                if backup_comp.exists() and backup_comp.stat().st_size > 50_000:
                    companion_sys_path = backup_comp
                else:
                    companion_sys_path = workflow.identity_fonts_root / companion_file
                companion_out_path = stage_dir / companion_file
                build_font(mono_regular_entry.source_path, companion_sys_path, companion_out_path)
                _verify_build_output(companion_out_path, companion_sys_path)
                companion_key = f"mono_companion_{companion_file.lower().replace('.', '_')}"
                artifacts[companion_key] = {
                    "weight": companion_key,
                    "registry_name": companion_reg_name,
                    "system_filename": companion_file,
                    "generated_filename": companion_file,
                    "source_path": str(mono_regular_entry.source_path),
                    "family_name": mono_regular_entry.family_name,
                    "full_name": mono_regular_entry.full_name,
                    "staged_path": str(companion_out_path),
                    "hash": hash_file(companion_out_path),
                }

    return artifacts
