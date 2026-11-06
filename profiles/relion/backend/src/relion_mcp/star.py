from __future__ import annotations

import codecs
import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

from ._relion_metadata_types import (
    RELION_METADATA_CANONICAL_LABELS,
    RELION_METADATA_LABEL_TYPES,
    RELION_WRITER_SPECIAL_LOOP_LABELS,
)


class StarFormatError(ValueError):
    """Raised when required RELION STAR metadata is missing or malformed."""


_STAR_NUMBER_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_STAR_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_STRICT_RELION_LOOP_HEADER_RE = re.compile(r"^_[^ #]+(?: #.*)?$")


def _canonical_relion_label(label: str) -> str:
    return RELION_METADATA_CANONICAL_LABELS.get(label, label)


def _invalid_typed_value(label: str, label_type: str, value: str) -> StarFormatError:
    return StarFormatError(
        f"Invalid RELION {label_type} metadata for {label}: {value!r}."
    )


def _validate_relion_scalar_writer_payload(
    label: str,
    label_type: str,
    token: str,
) -> None:
    """Reject numeric text that RELION's 12-character payload would corrupt."""
    if len(token) <= 12:
        return
    payload = token[:12]
    pattern = _STAR_INTEGER_RE if label_type == "integer" else _STAR_NUMBER_RE
    if pattern.fullmatch(payload) is None:
        raise StarFormatError(
            f"RELION's 12-character {label_type} writer payload would truncate "
            f"{label} from {token!r} to invalid numeric text {payload!r}."
        )
    full_number: int | float
    payload_number: int | float
    if label_type == "integer":
        full_number = int(token, 10)
        payload_number = int(payload, 10)
    else:
        full_number = float(token)
        payload_number = float(payload)
    same_value = payload_number == full_number
    if isinstance(full_number, float) and full_number == 0:
        same_value = same_value and math.copysign(1.0, payload_number) == math.copysign(
            1.0,
            full_number,
        )
    if not same_value:
        raise StarFormatError(
            f"RELION's 12-character {label_type} writer payload would truncate "
            f"{label} from {token!r} to {payload!r}, changing its parsed value."
        )


def _canonical_double(label: str, value: str) -> str:
    if _STAR_NUMBER_RE.fullmatch(value) is None:
        raise _invalid_typed_value(label, "double", value)
    number = float(value)
    if not math.isfinite(number):
        raise _invalid_typed_value(label, "finite double", value)

    def writer_token(current: float) -> str:
        use_scientific = (0 < abs(current) < 0.001) or abs(current) > 100000
        precision = 5 if current < 0 else 6
        presentation = "e" if use_scientific else "f"
        token = f"{current:.{precision}{presentation}}"
        _validate_relion_scalar_writer_payload(label, "double", token)
        return token

    token = writer_token(number)
    for _ in range(4):
        stabilized = writer_token(float(token))
        if stabilized == token:
            return "d:" + token
        token = stabilized
    raise StarFormatError(
        f"RELION double writer did not stabilize for {label}: {value!r}."
    )


def _vector_items(label: str, label_type: str, value: str) -> list[str]:
    if not value.startswith("[") or not value.endswith("]"):
        raise _invalid_typed_value(label, label_type, value)
    raw_body = value[1:-1]
    if raw_body == "":
        return []
    if not raw_body.strip(" "):
        raise _invalid_typed_value(label, label_type, value)
    body = raw_body.strip(" ")
    items = [item.strip(" ") for item in body.split(",")]
    if any(not item for item in items):
        raise _invalid_typed_value(label, label_type, value)
    return items


def _relion_escape_string_for_star(value: str) -> str:
    """Mirror RELION's string writer, including its historical BEL quirk."""
    if value == "":
        return '""'
    if value[0] in {'"', "'"} or " " in value or "\t" in value:
        escaped = ['"']
        for position, character in enumerate(value):
            if character == '"':
                next_position = position + 1
                if next_position == len(value) - 1 or (
                    next_position < len(value)
                    and value[next_position] in {" ", "\t"}
                ):
                    escaped.append("\x07")
            escaped.append(character)
        escaped.append('"')
        return "".join(escaped)
    return value


def _canonical_string(label: str, value: str) -> str:
    writer_token = _relion_escape_string_for_star(value)
    try:
        reparsed = _tokens(writer_token)
    except StarFormatError as exc:
        raise StarFormatError(
            f"RELION's string writer cannot safely round-trip {label}: {value!r}."
        ) from exc
    if reparsed != [value]:
        raise StarFormatError(
            f"RELION's string writer cannot safely round-trip {label}: {value!r}."
        )
    return "s:" + value


def _canonical_star_value(label: str, value: str) -> str:
    """Normalize a value according to RELION 5.0's declared label type/writer."""
    canonical_label = _canonical_relion_label(label)
    label_type = RELION_METADATA_LABEL_TYPES.get(label)
    if label_type is None:
        label_type = RELION_METADATA_LABEL_TYPES.get(canonical_label)
    if label_type in {None, "string", "unknown"}:
        return _canonical_string(canonical_label, value)
    if value in {".", "?"}:
        raise StarFormatError(
            f"RELION does not safely parse STAR null marker {value!r} for "
            f"non-string {label_type} metadata {canonical_label}."
        )
    if label_type == "double":
        return _canonical_double(canonical_label, value)
    if label_type == "int":
        if _STAR_INTEGER_RE.fullmatch(value) is None:
            if canonical_label == "rlnOpticsGroup":
                raise StarFormatError(
                    f"non-canonical rlnOpticsGroup integer metadata: {value!r}."
                )
            raise _invalid_typed_value(canonical_label, "integer", value)
        try:
            number = int(value, 10)
        except ValueError as exc:
            raise _invalid_typed_value(canonical_label, "integer", value) from exc
        if not -(1 << 63) <= number < (1 << 63):
            raise _invalid_typed_value(canonical_label, "64-bit integer", value)
        _validate_relion_scalar_writer_payload(
            canonical_label,
            "integer",
            str(number),
        )
        return f"i:{number}"
    if label_type == "bool":
        if _STAR_INTEGER_RE.fullmatch(value) is None:
            raise _invalid_typed_value(canonical_label, "boolean", value)
        try:
            number = int(value, 10)
        except ValueError as exc:
            raise _invalid_typed_value(canonical_label, "boolean", value) from exc
        if number not in {0, 1}:
            raise _invalid_typed_value(canonical_label, "boolean (0 or 1)", value)
        return f"b:{number}"
    if label_type == "int_vector":
        items = _vector_items(canonical_label, "integer-vector", value)
        normalized: list[str] = []
        for item in items:
            if _STAR_INTEGER_RE.fullmatch(item) is None:
                raise _invalid_typed_value(canonical_label, "integer-vector", value)
            try:
                number = int(item, 10)
            except ValueError as exc:
                raise _invalid_typed_value(
                    canonical_label,
                    "integer-vector",
                    value,
                ) from exc
            if not -(1 << 31) <= number < (1 << 31):
                raise _invalid_typed_value(
                    canonical_label,
                    "32-bit integer-vector",
                    value,
                )
            normalized.append(str(number))
        return "vi:[" + ",".join(normalized) + "]"
    if label_type == "double_vector":
        items = _vector_items(canonical_label, "double-vector", value)
        normalized_doubles: list[str] = []
        for item in items:
            if _STAR_NUMBER_RE.fullmatch(item) is None:
                raise _invalid_typed_value(canonical_label, "double-vector", value)
            number = float(item)
            if not math.isfinite(number):
                raise _invalid_typed_value(
                    canonical_label,
                    "finite double-vector",
                    value,
                )
            normalized_doubles.append(format(number, ".12g"))
        return "vd:[" + ",".join(normalized_doubles) + "]"
    raise StarFormatError(
        f"Unsupported RELION metadata type {label_type!r} for {canonical_label}."
    )


def _duplicate_relion_columns(columns: list[str]) -> list[str]:
    counts = Counter(_canonical_relion_label(column) for column in columns)
    return sorted(column for column, count in counts.items() if count > 1)


@dataclass(frozen=True)
class ParticleStarSummary:
    """Bounded summary used to validate class-selection inputs and outputs."""

    particle_count: int
    columns: tuple[str, ...]
    class_counts: dict[int, int]
    optics_group_counts: dict[str, int] | None
    class_optics_group_counts: dict[int, dict[str, int]] | None
    image_identity_by_class: dict[int, str]
    row_metadata_by_class: dict[int, str]
    row_metadata_without_group_number_by_class: dict[int, str]
    duplicate_image_name_count: int
    duplicate_image_name_examples: tuple[str, ...]


@dataclass(frozen=True)
class ParticleRowSummary:
    """Class-independent validation summary for a RELION particle STAR.

    Fresh extraction STAR files do not necessarily contain ``rlnClassNumber``.
    Direct particle utilities still need a bounded way to validate and count their
    rows, so this deliberately requires only the particle image identity column.
    """

    particle_count: int
    columns: tuple[str, ...]
    optics_group_counts: dict[str, int] | None
    duplicate_image_name_count: int
    duplicate_image_name_examples: tuple[str, ...]
    image_name_multiset_digest: str
    row_metadata_multiset_digest: str


def _relion_simplify_line(line: str) -> str:
    """Mirror RELION's ``unescape`` then ``simplify`` preprocessing."""
    unescaped = line.translate(
        {
            ord("\n"): None,
            ord("\v"): None,
            ord("\b"): None,
            ord("\r"): None,
            ord("\f"): None,
            ord("\x07"): None,
            ord("\t"): " ",
        }
    )
    return re.sub(r" +", " ", unescaped.strip(" "))


def _is_native_structural_data_line(line: str, target_marker: str) -> bool:
    """Match RELION's block suffix while excluding substring decoys."""
    without_newline = line.removesuffix("\n")
    ascii_space_trimmed = without_newline.strip(" ")
    marker_position = ascii_space_trimmed.find("data_")
    return (
        line.strip() == target_marker
        and marker_position >= 0
        and ascii_space_trimmed[marker_position:] == target_marker
    )


def _tokens(line: str) -> list[str]:
    """Simplify and tokenize one line like RELION's STAR loop reader."""
    line = _relion_simplify_line(line)
    tokens: list[str] = []
    cursor = 0
    line_length = len(line)
    while True:
        while cursor < line_length and line[cursor] in " \t\n":
            cursor += 1
        if cursor == line_length or line[cursor] == "#":
            return tokens

        quote = line[cursor] if line[cursor] in {'"', "'"} else None
        if quote is None:
            token_start = cursor
            while cursor < line_length and line[cursor] not in " \t\n":
                cursor += 1
            tokens.append(line[token_start:cursor])
            continue

        cursor += 1
        quoted: list[str] = []
        while cursor < line_length:
            character = line[cursor]
            if character == quote and (
                cursor + 1 == line_length or line[cursor + 1] in " \t\n"
            ):
                cursor += 1
                tokens.append("".join(quoted))
                break
            quoted.append(character)
            cursor += 1
        else:
            raise StarFormatError("Malformed STAR token syntax: unclosed quoted value.")


def _validate_native_star_bytes(path: Path) -> None:
    """Validate native-bound encoding and line endings using bounded memory."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if b"\r" in chunk:
                    raise StarFormatError(
                        "CRLF line endings or bare carriage returns are not accepted by "
                        f"RELION's STAR reader: {path}"
                    )
                if b"\x00" in chunk:
                    raise StarFormatError(
                        f"NUL bytes are not accepted in native-bound STAR file {path}."
                    )
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise StarFormatError(f"Invalid UTF-8 in native-bound STAR file {path}.") from exc


def _parse_star_lines(
    lines: list[str],
    *,
    strict_loops: bool = False,
    strict_target_block: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse already-loaded STAR lines; shared by full- and single-block readers."""
    blocks: dict[str, dict[str, Any]] = {}
    block: dict[str, Any] | None = None
    block_name: str | None = None
    strict_loop_blocks: set[str] = set()
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if (
            strict_loops
            and block is not None
            and (block_name or "") not in strict_loop_blocks
            and "loop_" in lines[i]
            and stripped != "loop_"
        ):
            raise StarFormatError(
                f"Strict data_{block_name or ''} contains a reserved loop_ substring "
                "before its structural loop_ line."
            )
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if stripped.startswith("data_"):
            name = stripped[5:].strip()
            block_name = name
            block = blocks.setdefault(name, {})
            i += 1
            continue
        if block is None:
            i += 1
            continue
        if stripped == "loop_":
            strict_block_name = block_name or ""
            if strict_loops and strict_block_name in strict_loop_blocks:
                raise StarFormatError(
                    f"Strict data_{strict_block_name} must contain exactly one loop."
                )
            if strict_loops:
                strict_loop_blocks.add(strict_block_name)
            i += 1
            columns: list[str] = []
            while i < len(lines):
                candidate = _relion_simplify_line(lines[i])
                if not candidate:
                    i += 1
                    continue
                if candidate.startswith(("#", ";")):
                    i += 1
                    continue
                if not candidate.startswith("_"):
                    break
                if strict_loops and _STRICT_RELION_LOOP_HEADER_RE.fullmatch(candidate) is None:
                    raise StarFormatError(
                        f"Strict data_{block_name or ''} loop has malformed column "
                        f"declaration {candidate!r}."
                    )
                columns.append(candidate.split(" ", 1)[0][1:])
                i += 1
            if strict_loops:
                duplicate_columns = _duplicate_relion_columns(columns)
                if duplicate_columns:
                    raise StarFormatError(
                        f"Strict data_{block_name or ''} loop has duplicate column "
                        "labels: " + ", ".join(duplicate_columns)
                    )
                writer_special_columns = sorted(
                    set(map(_canonical_relion_label, columns)).intersection(
                        RELION_WRITER_SPECIAL_LOOP_LABELS
                    )
                )
                if writer_special_columns:
                    raise StarFormatError(
                        f"Strict data_{block_name or ''} loop contains columns RELION "
                        "does not write as ordinary loop values: "
                        + ", ".join(writer_special_columns)
                    )
            values: list[str] = []
            strict_rows: list[list[str]] = []
            while i < len(lines):
                candidate = _relion_simplify_line(lines[i])
                if not candidate:
                    i += 1
                    if values or strict_rows:
                        break
                    continue
                if candidate.startswith("#"):
                    if strict_loops and strict_rows:
                        raise StarFormatError(
                            f"Strict data_{block_name or ''} loop rows do not permit "
                            "full-line comments after data rows begin."
                        )
                    i += 1
                    continue
                if candidate.startswith(("data_", "loop_", "_")):
                    if strict_loops:
                        raise StarFormatError(
                            f"Strict data_{block_name or ''} loop requires at least one "
                            "data row followed by a blank-line "
                            f"terminator before row-phase marker {candidate!r}."
                        )
                    break
                tokens = _tokens(lines[i])
                if strict_loops and len(tokens) != len(columns):
                    raise StarFormatError(
                        f"Strict data_{block_name or ''} loop rows require exactly "
                        f"{len(columns)} values on one physical line; got {len(tokens)}."
                    )
                if strict_loops:
                    strict_rows.append(tokens)
                else:
                    values.extend(tokens)
                i += 1
            for column in columns:
                block[column] = []
            width = len(columns)
            if width:
                rows = (
                    strict_rows
                    if strict_loops
                    else [
                        values[offset : offset + width]
                        for offset in range(0, len(values), width)
                    ]
                )
                for row in rows:
                    if len(row) != width:
                        if strict_loops:  # pragma: no cover - guarded while reading
                            raise StarFormatError(
                                f"Strict data_{block_name or ''} loop row is incomplete."
                            )
                        break
                    for column, value in zip(columns, row, strict=True):
                        block[column].append(value)
            continue
        if stripped.startswith("_"):
            if strict_loops:
                raise StarFormatError(
                    f"Strict data_{block_name or ''} must contain exactly one loop "
                    f"and no scalar metadata entries; got {stripped!r}."
                )
            tokens = _tokens(stripped)
            key = tokens[0][1:]
            block[key] = tokens[1] if len(tokens) > 1 else ""
        i += 1
    if strict_loops:
        required_loop_blocks = (
            (strict_target_block,) if strict_target_block is not None else tuple(blocks)
        )
        for name in required_loop_blocks:
            if name not in strict_loop_blocks:
                raise StarFormatError(
                    f"Strict data_{name} block is empty or otherwise does not contain "
                    "exactly one loop."
                )
    return blocks


def parse_star(path: str | Path) -> dict[str, dict[str, Any]]:
    """Parse the scalar and loop constructs used by RELION pipeline STAR files.

    This intentionally small parser is designed for pipeline metadata, not arbitrary
    CIF/STAR dialects. It preserves values as strings.
    """
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    return _parse_star_lines(lines)


def parse_star_block(
    path: str | Path,
    block_name: str,
    *,
    strict_loops: bool = False,
) -> dict[str, Any] | None:
    """Parse one named data block without retaining unrelated, potentially huge loops."""
    star_path = Path(path)
    if strict_loops:
        _validate_native_star_bytes(star_path)
    lines: list[str] = []
    collecting = False
    target_marker = f"data_{block_name}"
    try:
        with star_path.open(
            "r",
            encoding="utf-8",
            errors="strict" if strict_loops else "replace",
        ) as handle:
            for line in handle:
                stripped = line.strip()
                if (
                    strict_loops
                    and not collecting
                    and target_marker in line
                    and not _is_native_structural_data_line(line, target_marker)
                ):
                    raise StarFormatError(
                        f"Found reserved {target_marker} substring before its structural "
                        f"{target_marker} line in {path}."
                    )
                if stripped.startswith("data_"):
                    name = stripped[5:].strip()
                    if collecting and name != block_name:
                        if strict_loops:
                            lines.append(line.rstrip("\n"))
                        break
                    collecting = name == block_name
                if collecting:
                    lines.append(line.rstrip("\n"))
    except UnicodeDecodeError as exc:  # mutation after the bounded byte scan
        raise StarFormatError(f"Invalid UTF-8 in native-bound STAR file {path}.") from exc
    if not lines:
        return None
    return _parse_star_lines(
        lines,
        strict_loops=strict_loops,
        strict_target_block=block_name if strict_loops else None,
    ).get(block_name)


def relion_metadata_loop_digest(
    block: dict[str, Any],
    *,
    block_name: str,
) -> str:
    """Return an order-insensitive, RELION-type-aware digest of one loop."""
    columns = list(block)
    canonical_columns = [_canonical_relion_label(column) for column in columns]
    duplicate_columns = _duplicate_relion_columns(columns)
    if duplicate_columns:
        raise StarFormatError(
            f"data_{block_name} has duplicate column labels: "
            + ", ".join(duplicate_columns)
        )
    writer_special_columns = sorted(
        set(canonical_columns).intersection(RELION_WRITER_SPECIAL_LOOP_LABELS)
    )
    if writer_special_columns:
        raise StarFormatError(
            f"data_{block_name} contains columns RELION does not write as ordinary "
            "loop values: " + ", ".join(writer_special_columns)
        )
    values_by_column: list[list[str]] = []
    for column in columns:
        values = block[column]
        if not isinstance(values, list):
            raise StarFormatError(
                f"data_{block_name} must contain one loop; {column} is a scalar value."
            )
        values_by_column.append([str(value) for value in values])
    row_counts = {len(values) for values in values_by_column}
    if len(row_counts) > 1:
        raise StarFormatError(
            f"data_{block_name} columns do not contain the same number of rows."
        )
    row_count = next(iter(row_counts), 0)
    row_identity_sum = 0
    modulus = 1 << 256
    for row_index in range(row_count):
        metadata_pairs = sorted(
            (
                canonical_column,
                _canonical_star_value(column, values[row_index]),
            )
            for column, canonical_column, values in zip(
                columns,
                canonical_columns,
                values_by_column,
                strict=True,
            )
        )
        row_payload = "\0".join(
            item for pair in metadata_pairs for item in pair
        )
        row_identity_sum = (
            row_identity_sum
            + int.from_bytes(hashlib.sha256(row_payload.encode("utf-8")).digest(), "big")
        ) % modulus
    header_payload = "\0".join(sorted(canonical_columns))
    table_payload = (
        f"data_{block_name}\0{row_count}\0{header_payload}\0"
        f"{row_identity_sum:064x}"
    )
    return hashlib.sha256(table_payload.encode("utf-8")).hexdigest()


def particle_star_row_summary(
    path: str | Path,
    *,
    required_columns: Collection[str] = (),
    track_duplicate_image_names: bool = True,
    allow_unnamed_particle_block: bool = False,
) -> ParticleRowSummary:
    """Stream and validate ``data_particles`` without requiring class metadata.

    Duplicate tracking retains every distinct image name. Callers that already
    verify a cryptographic image-reference multiset can disable it to keep
    memory bounded for very large particle tables. ``allow_unnamed_particle_block``
    is reserved for native RELION outputs that write the particle loop under an
    unnamed ``data_`` block; ordinary particle inputs must retain the explicit
    ``data_particles`` block.
    """
    star_path = Path(path)
    _validate_native_star_bytes(star_path)
    found_block = False
    columns: list[str] = []
    first_row: str | None = None
    handle = star_path.open("r", encoding="utf-8", errors="strict")
    try:
        iterator = iter(handle)
        for line in iterator:
            stripped = line.strip()
            if "data_particles" in line:
                if not _is_native_structural_data_line(line, "data_particles"):
                    raise StarFormatError(
                        "Found reserved data_particles substring before its structural "
                        f"data_particles line in {star_path}."
                    )
                found_block = True
                break
            if allow_unnamed_particle_block and stripped == "data_":
                if not _is_native_structural_data_line(line, "data_"):
                    raise StarFormatError(
                        "Found a reserved data_ substring instead of a structural "
                        f"unnamed data_ line in {star_path}."
                    )
                found_block = True
                break
            if (
                allow_unnamed_particle_block
                and stripped.startswith("data_")
                and stripped != "data_optics"
            ):
                raise StarFormatError(
                    f"Unexpected particle data block {stripped!r} in {star_path}; "
                    "the native removed-particle sidecar may use only data_particles "
                    "or the unnamed data_ block."
                )
        if not found_block:
            expected_block = (
                "data_particles or native unnamed data_"
                if allow_unnamed_particle_block
                else "data_particles"
            )
            raise StarFormatError(f"Missing {expected_block} block in {star_path}")

        for line in iterator:
            stripped = line.strip()
            if "loop_" in line and stripped != "loop_":
                raise StarFormatError(
                    f"data_particles in {star_path} contains a reserved loop_ substring "
                    "before its structural loop_ line."
                )
            if stripped.startswith("data_"):
                raise StarFormatError(
                    f"Missing particle loop in data_particles block of {star_path}"
                )
            if stripped.startswith("_"):
                raise StarFormatError(
                    f"data_particles in {star_path} must contain a loop as its first "
                    f"data construct and no scalar metadata entries; got {stripped!r}."
                )
            if stripped == "loop_":
                break
        else:
            raise StarFormatError(
                f"Missing particle loop in data_particles block of {star_path}"
            )

        for line in iterator:
            stripped = _relion_simplify_line(line)
            if not stripped:
                continue
            if stripped.startswith(("#", ";")):
                continue
            if stripped.startswith("_"):
                if _STRICT_RELION_LOOP_HEADER_RE.fullmatch(stripped) is None:
                    raise StarFormatError(
                        f"data_particles in {star_path} has malformed loop column "
                        f"declaration {stripped!r}."
                    )
                columns.append(stripped.split(" ", 1)[0][1:])
                continue
            first_row = line
            break

        if not columns:
            raise StarFormatError(f"Particle loop in {star_path} has no columns")
        duplicate_columns = _duplicate_relion_columns(columns)
        if duplicate_columns:
            raise StarFormatError(
                f"data_particles in {star_path} has duplicate column labels: "
                + ", ".join(duplicate_columns)
            )
        writer_special_columns = sorted(
            set(map(_canonical_relion_label, columns)).intersection(
                RELION_WRITER_SPECIAL_LOOP_LABELS
            )
        )
        if writer_special_columns:
            raise StarFormatError(
                f"data_particles in {star_path} contains columns RELION does not write "
                "as ordinary loop values: "
                + ", ".join(writer_special_columns)
            )
        missing_columns = sorted(set(required_columns).difference(columns))
        if missing_columns:
            raise StarFormatError(
                f"data_particles in {star_path} is missing required columns: "
                + ", ".join(missing_columns)
            )
        if "rlnImageName" not in columns:
            raise StarFormatError(
                f"data_particles in {star_path} is missing required column: rlnImageName"
            )

        image_index = columns.index("rlnImageName")
        optics_index = columns.index("rlnOpticsGroup") if "rlnOpticsGroup" in columns else None
        numeric_indices = {
            column: columns.index(column)
            for column in (
                "rlnCoordinateX",
                "rlnCoordinateY",
                "rlnOriginXAngst",
                "rlnOriginYAngst",
            )
            if column in columns
        }
        micrograph_index = (
            columns.index("rlnMicrographName")
            if "rlnMicrographName" in columns
            else None
        )
        optics_counts: Counter[str] | None = Counter() if optics_index is not None else None
        seen_image_names: set[str] | None = (
            set() if track_duplicate_image_names else None
        )
        duplicate_count = 0
        duplicate_examples: list[str] = []
        image_identity_sum = 0
        row_metadata_identity_sum = 0
        row_count = 0
        modulus = 1 << 256

        def consume_row(row: list[str]) -> None:
            nonlocal duplicate_count, image_identity_sum, row_count
            nonlocal row_metadata_identity_sum
            image_name = row[image_index]
            if image_name in {"", ".", "?"}:
                raise StarFormatError(f"Missing rlnImageName in {star_path}")
            for column, index in numeric_indices.items():
                raw_value = row[index]
                try:
                    value = float(raw_value)
                except ValueError as exc:
                    raise StarFormatError(
                        f"{column} must be finite numeric metadata in {star_path}; "
                        f"got {raw_value!r}."
                    ) from exc
                if not math.isfinite(value):
                    raise StarFormatError(
                        f"{column} must be finite numeric metadata in {star_path}; "
                        f"got {raw_value!r}."
                    )
            if (
                micrograph_index is not None
                and row[micrograph_index] in {"", ".", "?"}
            ):
                raise StarFormatError(f"Missing rlnMicrographName in {star_path}")
            if seen_image_names is not None:
                if image_name in seen_image_names:
                    duplicate_count += 1
                    if (
                        len(duplicate_examples) < 10
                        and image_name not in duplicate_examples
                    ):
                        duplicate_examples.append(image_name)
                else:
                    seen_image_names.add(image_name)
            image_identity_sum = (
                image_identity_sum
                + int.from_bytes(
                    hashlib.sha256(image_name.encode("utf-8")).digest(),
                    "big",
                )
            ) % modulus
            metadata_pairs = sorted(
                (_canonical_relion_label(column), value)
                for column, value in zip(columns, row, strict=True)
            )
            metadata_payload = "\0".join(
                item
                for column, value in metadata_pairs
                for item in (column, _canonical_star_value(column, value))
            )
            row_metadata_identity_sum = (
                row_metadata_identity_sum
                + int.from_bytes(
                    hashlib.sha256(metadata_payload.encode("utf-8")).digest(),
                    "big",
                )
            ) % modulus
            if optics_index is not None and optics_counts is not None:
                optics_group = row[optics_index]
                if not optics_group:
                    raise StarFormatError(f"Empty rlnOpticsGroup in {star_path}")
                optics_counts[optics_group] += 1
            row_count += 1

        row_lines = ([first_row] if first_row is not None else [])
        width = len(columns)
        for line in chain(row_lines, iterator):
            stripped = _relion_simplify_line(line)
            if not stripped:
                if row_count == 0:
                    continue
                break
            if stripped.startswith("#"):
                raise StarFormatError(
                    f"data_particles in {star_path} does not permit full-line comments "
                    "after particle rows begin."
                )
            if stripped.startswith(("data_", "loop_", "_")):
                raise StarFormatError(
                    f"data_particles in {star_path} requires at least one data row "
                    "followed by a blank-line terminator "
                    f"before row-phase marker {stripped!r}."
                )
            row = _tokens(line)
            if len(row) != width:
                raise StarFormatError(
                    f"data_particles in {star_path} requires exactly one complete particle "
                    f"row with {width} values per physical line; got {len(row)}."
                )
            consume_row(row)

        if allow_unnamed_particle_block:
            for line in iterator:
                stripped = line.strip()
                if (
                    stripped.startswith("data_")
                    and stripped != "data_optics"
                ):
                    raise StarFormatError(
                        f"Unexpected additional data block {stripped!r} in {star_path}; "
                        "the native removed-particle sidecar must contain one particle "
                        "table."
                    )

        return ParticleRowSummary(
            particle_count=row_count,
            columns=tuple(columns),
            optics_group_counts=(
                dict(sorted(optics_counts.items())) if optics_counts is not None else None
            ),
            duplicate_image_name_count=duplicate_count,
            duplicate_image_name_examples=tuple(duplicate_examples),
            image_name_multiset_digest=f"{image_identity_sum:064x}",
            row_metadata_multiset_digest=f"{row_metadata_identity_sum:064x}",
        )
    except UnicodeDecodeError as exc:  # mutation after the bounded byte scan
        raise StarFormatError(
            f"Invalid UTF-8 in native-bound STAR file {star_path}."
        ) from exc
    finally:
        handle.close()


def particle_star_summary(path: str | Path) -> ParticleStarSummary:
    """Stream the RELION ``data_particles`` loop and validate its key columns.

    Particle rows are not retained. Exact duplicate detection keeps one image-name
    string per unique particle; counts, bounded duplicate examples, and
    order-independent cryptographic identity accumulators are also retained.
    """
    star_path = Path(path)
    found_block = False
    columns: list[str] = []
    first_row: str | None = None
    handle = star_path.open("r", encoding="utf-8", errors="replace")
    try:
        iterator = iter(handle)
        for line in iterator:
            stripped = line.strip()
            if stripped == "data_particles":
                found_block = True
                break
        if not found_block:
            raise StarFormatError(f"Missing data_particles block in {star_path}")

        for line in iterator:
            stripped = line.strip()
            if stripped.startswith("data_"):
                raise StarFormatError(f"Missing particle loop in data_particles block of {star_path}")
            if stripped == "loop_":
                break
        else:
            raise StarFormatError(f"Missing particle loop in data_particles block of {star_path}")

        for line in iterator:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("_"):
                columns.append(stripped.split()[0][1:])
                continue
            first_row = line
            break

        required = {"rlnImageName", "rlnClassNumber"}
        missing = sorted(required - set(columns))
        if missing:
            raise StarFormatError(
                f"data_particles in {star_path} is missing required columns: {', '.join(missing)}"
            )
        if not columns:
            raise StarFormatError(f"Particle loop in {star_path} has no columns")

        image_index = columns.index("rlnImageName")
        class_index = columns.index("rlnClassNumber")
        optics_index = columns.index("rlnOpticsGroup") if "rlnOpticsGroup" in columns else None
        class_counts: Counter[int] = Counter()
        optics_counts: Counter[str] | None = Counter() if optics_index is not None else None
        class_optics_counts: defaultdict[int, Counter[str]] | None = (
            defaultdict(Counter) if optics_index is not None else None
        )
        identity_sums: defaultdict[int, int] = defaultdict(int)
        metadata_sums: defaultdict[int, int] = defaultdict(int)
        metadata_without_group_sums: defaultdict[int, int] = defaultdict(int)
        seen_image_names: set[str] = set()
        duplicate_count = 0
        duplicate_examples: list[str] = []
        row_count = 0
        buffered: list[str] = []
        modulus = 1 << 256

        def consume_row(row: list[str]) -> None:
            nonlocal duplicate_count, row_count
            class_text = row[class_index]
            try:
                class_number = int(class_text)
            except ValueError as exc:
                raise StarFormatError(
                    f"Invalid rlnClassNumber {class_text!r} in {star_path}"
                ) from exc
            if class_number < 1:
                raise StarFormatError(f"rlnClassNumber must be positive in {star_path}")
            image_name = row[image_index]
            if image_name in seen_image_names:
                duplicate_count += 1
                if len(duplicate_examples) < 10 and image_name not in duplicate_examples:
                    duplicate_examples.append(image_name)
            else:
                seen_image_names.add(image_name)
            digest = int.from_bytes(hashlib.sha256(image_name.encode("utf-8")).digest(), "big")
            identity_sums[class_number] = (identity_sums[class_number] + digest) % modulus
            metadata_pairs = sorted(zip(columns, row, strict=True))
            metadata_payload = "\0".join(
                item for column, value in metadata_pairs for item in (column, value)
            )
            metadata_digest = int.from_bytes(
                hashlib.sha256(metadata_payload.encode("utf-8")).digest(), "big"
            )
            metadata_sums[class_number] = (
                metadata_sums[class_number] + metadata_digest
            ) % modulus
            metadata_without_group_payload = "\0".join(
                item
                for column, value in metadata_pairs
                if column != "rlnGroupNumber"
                for item in (column, value)
            )
            metadata_without_group_digest = int.from_bytes(
                hashlib.sha256(metadata_without_group_payload.encode("utf-8")).digest(),
                "big",
            )
            metadata_without_group_sums[class_number] = (
                metadata_without_group_sums[class_number] + metadata_without_group_digest
            ) % modulus
            class_counts[class_number] += 1
            if optics_index is not None and optics_counts is not None and class_optics_counts is not None:
                optics_group = row[optics_index]
                optics_counts[optics_group] += 1
                class_optics_counts[class_number][optics_group] += 1
            row_count += 1

        row_lines = ([first_row] if first_row is not None else [])
        for line in chain(row_lines, iterator):
            stripped = line.strip()
            if not stripped:
                if buffered:
                    raise StarFormatError(f"Incomplete particle row in {star_path}")
                break
            if stripped.startswith("#"):
                continue
            if stripped.startswith(("data_", "loop_", "_")):
                if buffered:
                    raise StarFormatError(f"Incomplete particle row in {star_path}")
                break
            buffered.extend(_tokens(line))
            width = len(columns)
            while len(buffered) >= width:
                consume_row(buffered[:width])
                del buffered[:width]
        if buffered:
            raise StarFormatError(f"Incomplete particle row in {star_path}")

        return ParticleStarSummary(
            particle_count=row_count,
            columns=tuple(columns),
            class_counts=dict(sorted(class_counts.items())),
            optics_group_counts=dict(sorted(optics_counts.items())) if optics_counts is not None else None,
            class_optics_group_counts=(
                {key: dict(sorted(value.items())) for key, value in sorted(class_optics_counts.items())}
                if class_optics_counts is not None
                else None
            ),
            image_identity_by_class={
                key: f"{value:064x}" for key, value in sorted(identity_sums.items())
            },
            row_metadata_by_class={
                key: f"{value:064x}" for key, value in sorted(metadata_sums.items())
            },
            row_metadata_without_group_number_by_class={
                key: f"{value:064x}"
                for key, value in sorted(metadata_without_group_sums.items())
            },
            duplicate_image_name_count=duplicate_count,
            duplicate_image_name_examples=tuple(duplicate_examples),
        )
    finally:
        handle.close()


def pipeline_processes(path: str | Path) -> list[dict[str, str]]:
    blocks = parse_star(path)
    block = blocks.get("pipeline_processes", {})
    names = block.get("rlnPipeLineProcessName", [])
    if isinstance(names, str):
        names = [names]
    labels = block.get("rlnPipeLineProcessStatusLabel", [])
    if isinstance(labels, str):
        labels = [labels]
    numeric = block.get("rlnPipeLineProcessStatus", [])
    if isinstance(numeric, str):
        numeric = [numeric]

    rows: list[dict[str, str]] = []
    for idx, name in enumerate(names):
        status = labels[idx] if idx < len(labels) else (numeric[idx] if idx < len(numeric) else "Unknown")
        rows.append({"name": name, "status": status})
    return rows


def job_output_edges(path: str | Path) -> list[str]:
    blocks = parse_star(path)
    for block_name in ("pipeline_output_edges", "output_edges"):
        block = blocks.get(block_name, {})
        values = block.get("rlnPipeLineEdgeToNode", [])
        if isinstance(values, str):
            return [values]
        if values:
            return list(values)
    return []
