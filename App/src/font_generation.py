from pathlib import Path

from fontTools.ttLib import TTFont, TTCollection


def _read_identity(font):
    identity = {
            "macStyle": font["head"].macStyle,
            "os2_version": font["OS/2"].version,
            "os2_weight": font["OS/2"].usWeightClass,
            "os2_width": font["OS/2"].usWidthClass,
            "os2_fsSelection": font["OS/2"].fsSelection,
            "post_italicAngle": font["post"].italicAngle,
            "name_records": [],
        }

    try:
        identity["os2_panose"] = font["OS/2"].panose
    except AttributeError:
        identity["os2_panose"] = None

    if hasattr(font["OS/2"], "usLowerOpticalPointSize"):
        identity["usLowerOpticalPointSize"] = font["OS/2"].usLowerOpticalPointSize
    if hasattr(font["OS/2"], "usUpperOpticalPointSize"):
        identity["usUpperOpticalPointSize"] = font["OS/2"].usUpperOpticalPointSize

    for record in font["name"].names:
        try:
            identity["name_records"].append(
                {
                    "nameID": record.nameID,
                    "platformID": record.platformID,
                    "platEncID": record.platEncID,
                    "langID": record.langID,
                    "string": record.toUnicode(),
                }
            )
        except UnicodeDecodeError:
            continue

    return identity


def read_segoe_identity(segoe_path):
    font = TTFont(segoe_path)
    try:
        identity = _read_identity(font)
    finally:
        font.close()
    return identity


def read_collection_identities(ttc_path) -> list[dict]:
    """Identity of every face in a donor .ttc, in collection order."""
    collection = TTCollection(str(ttc_path), lazy=False)
    try:
        return [_read_identity(font) for font in collection.fonts]
    finally:
        collection.close()


def _open_source_face(source_path: Path, target_weight: int):
    """Open the source face closest to the donor face's weight.

    A plain .ttf yields its single face; a .ttc yields the face whose
    OS/2.usWeightClass is nearest (preferring non-italic faces).
    """
    if source_path.suffix.lower() != ".ttc":
        return TTFont(str(source_path), lazy=False)

    from font_detection import count_collection_faces, inspect_font

    best_index = 0
    best_diff = None
    for index in range(count_collection_faces(source_path)):
        metadata = inspect_font(source_path, index)
        diff = abs(metadata.weight_class - target_weight) + (50 if metadata.is_italic else 0)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_index = index

    return TTFont(str(source_path), fontNumber=best_index, lazy=False)


def build_ttc_font(source_path, donor_ttc_path, output_path):
    """Build a .ttc whose faces keep every donor face's identity.

    Microsoft YaHei collections contain several faces (Microsoft YaHei,
    Microsoft YaHei UI, Light variants ...). Each donor face's name and
    OS/2 identity is transplanted onto the user's font so Windows keeps
    resolving every requested face name to the replaced file.
    """
    source_path = Path(source_path)
    donor_ttc_path = Path(donor_ttc_path)
    output_path = Path(output_path)

    if not source_path.exists():
        raise FileNotFoundError(f"Font not found: {source_path}")
    if not donor_ttc_path.exists():
        raise FileNotFoundError(f"System font not found: {donor_ttc_path}")

    identities = read_collection_identities(donor_ttc_path)
    if not identities:
        raise RuntimeError(f"Donor collection contains no faces: {donor_ttc_path.name}")

    built_fonts = []
    try:
        for identity in identities:
            font = _open_source_face(source_path, identity["os2_weight"])
            built_fonts.append(font)
            apply_identity(font, identity)

        collection = TTCollection()
        collection.fonts = built_fonts
        collection.save(str(output_path))
    finally:
        for font in built_fonts:
            try:
                font.close()
            except Exception:
                pass

    return str(output_path)


def apply_identity(font, identity):
    kept_names = []
    if "fvar" in font:
        kept_names = [n for n in font["name"].names if n.nameID > 255]
    
    font["name"].names = kept_names
    for record in identity["name_records"]:
        font["name"].setName(
            record["string"],
            record["nameID"],
            record["platformID"],
            record["platEncID"],
            record["langID"],
        )

    target_os2_version = identity["os2_version"]
    os2 = font["OS/2"]

    if target_os2_version >= 1:
        if not hasattr(os2, "ulCodePageRange1"):
            os2.ulCodePageRange1 = 0
        if not hasattr(os2, "ulCodePageRange2"):
            os2.ulCodePageRange2 = 0

    if target_os2_version >= 2:
        upem = font["head"].unitsPerEm if "head" in font else 2048
        if not hasattr(os2, "sxHeight") or os2.sxHeight is None:
            try:
                glyf = font["glyf"]
                if "x" in glyf and glyf["x"].numberOfContours != 0:
                    os2.sxHeight = glyf["x"].yMax
                else:
                    os2.sxHeight = int(upem * 0.5)
            except Exception:
                os2.sxHeight = int(upem * 0.5)

        if not hasattr(os2, "sCapHeight") or os2.sCapHeight is None:
            try:
                glyf = font["glyf"]
                if "H" in glyf and glyf["H"].numberOfContours != 0:
                    os2.sCapHeight = glyf["H"].yMax
                else:
                    os2.sCapHeight = int(upem * 0.7)
            except Exception:
                os2.sCapHeight = int(upem * 0.7)

        if not hasattr(os2, "usDefaultChar"):
            os2.usDefaultChar = 0
        if not hasattr(os2, "usBreakChar"):
            os2.usBreakChar = 32
        if not hasattr(os2, "usMaxContext"):
            os2.usMaxContext = 1

    if target_os2_version >= 5:
        if not hasattr(os2, "usLowerOpticalPointSize"):
            os2.usLowerOpticalPointSize = identity.get("usLowerOpticalPointSize", 0)
        if not hasattr(os2, "usUpperOpticalPointSize"):
            os2.usUpperOpticalPointSize = identity.get("usUpperOpticalPointSize", 0xFFFF // 20)

    os2.version = target_os2_version
    os2.usWeightClass = identity["os2_weight"]
    os2.usWidthClass = identity["os2_width"]
    os2.fsSelection = identity["os2_fsSelection"]

    if identity["os2_panose"] is not None:
        os2.panose = identity["os2_panose"]

    if "DSIG" in font:
        del font["DSIG"]

    font["head"].macStyle = identity["macStyle"]
    font["post"].italicAngle = identity["post_italicAngle"]


def build_font(source_path, segoe_path, output_path):
    source_path = Path(source_path)
    segoe_path = Path(segoe_path)
    output_path = Path(output_path)
    
    if not source_path.exists():
        raise FileNotFoundError(f"Font not found: {source_path}")
    if not segoe_path.exists():
        raise FileNotFoundError(f"System font not found: {segoe_path}")

    identity = read_segoe_identity(segoe_path)
    font = TTFont(source_path)
    try:
        apply_identity(font, identity)
        font.save(str(output_path))
    finally:
        font.close()

    return str(output_path)


def build_variable_font(source_path, segoe_var_path, output_path):
    """Build a clean static font containing Segoe UI Variable's identity.

    Tricking Windows 11 into using a user's static font when Segoe UI Variable is requested
    is achieved by generating a valid static TrueType font with Segoe UI Variable's name
    and OS/2 tables. Splicing partial variable tables without gvar is invalid per OpenType spec.
    """
    return build_font(source_path, segoe_var_path, output_path)

