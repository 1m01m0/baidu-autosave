#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from collections import deque
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECT_REQUIREMENT_FILES = (
    REPO_ROOT / "requirements.txt",
    REPO_ROOT / "requirements-test.txt",
    REPO_ROOT / "requirements-quality.txt",
)
CONSTRAINTS_FILE = REPO_ROOT / "constraints.txt"


def _iter_requirement_lines(path):
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            yield line


def _parse_pins(paths):
    pins = {}
    extras = {}
    errors = []
    for path in paths:
        for line in _iter_requirement_lines(path):
            if "==" not in line:
                errors.append(f"{path.name}: 依赖未精确 pin: {line}")
                continue
            try:
                req = Requirement(line)
            except Exception as exc:
                errors.append(f"{path.name}: 无法解析依赖 {line!r}: {exc}")
                continue
            name = canonicalize_name(req.name)
            specifiers = list(req.specifier)
            if len(specifiers) != 1 or specifiers[0].operator != "==":
                errors.append(f"{path.name}: 依赖必须使用单一 == pin: {line}")
                continue
            version = specifiers[0].version
            if name in pins and pins[name] != version:
                errors.append(f"{path.name}: {name} pin 版本冲突: {pins[name]} != {version}")
            pins[name] = version
            extras[name] = extras.get(name, set()) | set(req.extras)
    return pins, extras, errors


@lru_cache(maxsize=None)
def _installed_distribution(name):
    return metadata.distribution(name)


@lru_cache(maxsize=None)
def _installed_version(name):
    return metadata.version(name)


@lru_cache(maxsize=None)
def _parsed_distribution_requirements(name):
    dist = _installed_distribution(name)
    requirements = []
    errors = []
    package_name = dist.metadata["Name"]
    for raw_requirement in dist.requires or []:
        try:
            requirements.append(Requirement(raw_requirement))
        except Exception as exc:
            errors.append(f"{package_name}: 无法解析依赖 {raw_requirement!r}: {exc}")
    return package_name, tuple(requirements), tuple(errors)


def _requirement_applies(req, extras):
    if req.marker is None:
        return True
    active_extras = extras or {""}
    return any(req.marker.evaluate({"extra": extra}) for extra in active_extras)


def _dependency_closure(direct_names, direct_extras):
    required = {}
    errors = []
    queue = deque((name, frozenset(direct_extras.get(name, set()))) for name in direct_names)
    visited = set()

    while queue:
        name, extras = queue.popleft()
        visit_key = (name, extras)
        if visit_key in visited:
            continue
        visited.add(visit_key)

        try:
            package_name, requirements, parse_errors = _parsed_distribution_requirements(name)
        except metadata.PackageNotFoundError:
            errors.append(f"当前环境未安装依赖: {name}")
            continue
        errors.extend(parse_errors)

        for req in requirements:
            if not _requirement_applies(req, extras):
                continue
            dep_name = canonicalize_name(req.name)
            dep_extras = frozenset(req.extras)
            try:
                dep_version = _installed_version(dep_name)
            except metadata.PackageNotFoundError:
                errors.append(f"{package_name}: 依赖未安装: {dep_name}")
                continue
            if dep_name not in direct_names:
                required[dep_name] = dep_version
            queue.append((dep_name, dep_extras))
    return required, errors


def _validate_installed_direct_pins(direct_pins):
    errors = []
    for name, pinned_version in sorted(direct_pins.items()):
        try:
            installed_version = _installed_version(name)
        except metadata.PackageNotFoundError:
            errors.append(f"当前环境未安装直接依赖: {name}")
            continue
        if installed_version != pinned_version:
            errors.append(
                f"当前环境中 {name}=={installed_version} 与 pin {pinned_version} 不一致"
            )
    return errors


def validate_dependency_lock():
    direct_pins, direct_extras, direct_errors = _parse_pins(DIRECT_REQUIREMENT_FILES)
    constraint_pins, _, constraint_errors = _parse_pins((CONSTRAINTS_FILE,))
    errors = direct_errors + constraint_errors

    direct_in_constraints = sorted(set(direct_pins) & set(constraint_pins))
    if direct_in_constraints:
        errors.append(
            "constraints.txt 只能包含传递依赖，不能包含直接依赖: "
            + ", ".join(direct_in_constraints)
        )

    environment_errors = _validate_installed_direct_pins(direct_pins)
    errors.extend(environment_errors)
    if environment_errors:
        return errors

    required_transitives, closure_errors = _dependency_closure(set(direct_pins), direct_extras)
    errors.extend(closure_errors)

    missing = sorted(set(required_transitives) - set(constraint_pins))
    extra = sorted(set(constraint_pins) - set(required_transitives))
    mismatched = sorted(
        name
        for name in set(required_transitives) & set(constraint_pins)
        if constraint_pins[name] != required_transitives[name]
    )

    if missing:
        errors.append("constraints.txt 缺少传递依赖: " + ", ".join(missing))
    if extra:
        errors.append("constraints.txt 包含多余传递依赖: " + ", ".join(extra))
    for name in mismatched:
        errors.append(
            f"constraints.txt 中 {name}=={constraint_pins[name]} 与当前环境 "
            f"{required_transitives[name]} 不一致"
        )

    return errors


def main():
    errors = validate_dependency_lock()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("依赖锁定校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
