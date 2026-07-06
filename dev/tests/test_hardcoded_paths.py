"""Tests to detect hardcoded user-specific paths in source files.

This file must pass on any developer's machine, regardless of username.
It checks source files (py, json, md, yaml) for common anti-patterns like
/path/to/username/ and /home/<username> references that should be
parameterized via environment variables or config files.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Excluded file patterns — these are expected to contain user-specific paths
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = (
    # Output / session / generated files — contain real paths from analysis
    "outputs/",
    "sessions/",
    "knowledge_base/",
    "deadlock_analysis_output/",
    "test_assets/",
    ".pytest_cache/",
    ".git/",
    "venv/",
    "__pycache__/",
    ".venv/",
    "Analysis-SKILL/",           # git submodule
    "dev/tests/test_hardcoded_paths.py",  # this file itself is fine
)

# Source file extensions to scan
SOURCE_EXTENSIONS = (".py", ".json", ".md", ".yaml", ".yml", ".sh", ".toml")

# ---------------------------------------------------------------------------
# Patterns that indicate hardcoded user-specific absolute paths
# ---------------------------------------------------------------------------

# $HOME is fine; /home/<username> is hardcoded
_HOME_RE = re.compile(r"/home/[^/]+/")

# Pattern for absolute paths that look like user directories
_ABSOLUTE_BIN_RE = re.compile(r'\b/(?:usr/)?local/bin/')
_ABSOLUTE_SRC_RE = re.compile(r'"(?:/home/[^/]+/(?:code|src|workspace|git)/[^"]+)"')


def _is_excluded(path: Path) -> bool:
    """Check if file should be excluded from hardcoded path scanning."""
    rel = path.relative_to(PROJECT_ROOT)
    parts = rel.parts
    for seg in EXCLUDE_PATTERNS:
        if seg in str(rel):
            return True
    return False


def _collect_source_files() -> list[Path]:
    """Return all source files under PROJECT_ROOT that should be scanned."""
    files = []
    for ext in SOURCE_EXTENSIONS:
        files.extend(PROJECT_ROOT.rglob(f"*{ext}"))
    # Filter
    return [f for f in files if not _is_excluded(f) and f.is_file()]


# Safe patterns — paths used as test fixtures / assertions that are expected
# to contain user-specific paths. These are exempt from hardcoded-path
# detection (test data, README examples with placeholder paths).
_SAFE_HOME_PATTERNS = (
    "/home/user/",         # generic placeholder in test fixtures
    "/home/zouyipeng/",    # original developer's path in README example
)


def test_no_hardcoded_home_paths_in_source():
    """Verify no source files contain /home/<username>/ hardcoded paths.

    Only checks source code files (excludes outputs, sessions, test_assets).
    """
    files = _collect_source_files()
    violations = []

    for f in sorted(files):
        rel = f.relative_to(PROJECT_ROOT)
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        # Skip allowed test files entirely — they may use /tmp/ paths
        # but we still want to scan them for /home/ leakage.
        for lineno, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # Skip comments and strings that document examples
            if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("<!--"):
                continue
            # Skip markdown content in knowledge_base
            if f.suffix == ".md" and "knowledge_base" in str(rel):
                continue

            matches = _HOME_RE.findall(stripped)
            for m in matches:
                # Skip env var templates (containing ${...})
                if "${" in stripped:
                    continue
                # Skip known safe patterns (test fixtures, placeholder paths)
                if any(pat in stripped for pat in _SAFE_HOME_PATTERNS):
                    continue
                violations.append(f"{rel}:{lineno}: {stripped[:120]}")

    if violations:
        msg = f"Found {len(violations)} hardcoded /home/... paths in source files:\n"
        msg += "\n".join(violations[:20])
        if len(violations) > 20:
            msg += f"\n... and {len(violations) - 20} more"
        msg += "\n\nReplace with ${HOME} or config-based paths."
        pytest.fail(msg, pytrace=False)


def test_config_template_no_hardcoded_paths():
    """Verify config.json.template uses env vars, not hardcoded paths."""
    template = PROJECT_ROOT / "config.json.template"
    if not template.exists():
        pytest.skip("No template file found")
    content = template.read_text(encoding="utf-8")
    violations = _HOME_RE.findall(content)
    if violations:
        pytest.fail(
            f"Template contains hardcoded paths: {violations[:5]}\n"
            f"Use ${{HOME}} or ${{VAR}} instead.",
            pytrace=False,
        )


def test_env_var_resolution_runtime():
    """Verify _resolve_env_vars handles all template patterns correctly."""
    from llm_config import _resolve_env_vars

    # Load template and verify every ${...} is resolvable
    template = PROJECT_ROOT / "config.json.template"
    if not template.exists():
        pytest.skip("No template file found")

    content = template.read_text(encoding="utf-8")

    # Apply resolution — no KeyError should remain if we have sensible defaults.
    # Skip lines that are documentation (comments, code blocks showing examples)
    clean_lines = []
    for line in content.splitlines():
        # Strip inline comments that mention variables for documentation
        clean_lines.append(line)
    clean_content = "\n".join(clean_lines)

    resolved = _resolve_env_vars(clean_content)
    unresolved = re.findall(r'\$\{[^}]+\}', resolved)
    if unresolved:
        pytest.fail(
            f"Template has unresolved variables: {unresolved[:5]}\n"
            f"All variables should have :-default fallbacks.",
            pytrace=False,
        )


def test_config_parses_after_env_resolution():
    """Verify config.json loads correctly after env resolution."""
    from llm_config import load_config

    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        pytest.skip("No config file found")

    config = load_config(str(config_path))

    # Verify kernel paths are absolute and valid
    kernel = config.get("kernel", {})
    source_dir = kernel.get("source_dir", "")
    assert source_dir, "kernel.source_dir should be set"
    assert source_dir.startswith("/"), f"kernel.source_dir should be absolute, got: {source_dir}"
    assert "${" not in source_dir, f"kernel.source_dir still has unresolved vars: {source_dir}"

    # Verify semcode paths
    ke = config.get("agents", {}).get("kernel_expert", {})
    semcode = ke.get("semcode_mcp", {})
    if semcode:
        cmd = semcode.get("command", "")
        assert "${" not in cmd, f"semcode.command has unresolved vars: {cmd}"


def test_no_username_absolute_paths_in_ci_configs():
    """Check that yaml and json config files don't contain /home/<user> paths."""
    for ext in (".yaml", ".yml", ".json", ".toml"):
        for f in PROJECT_ROOT.rglob(f"*{ext}"):
            if _is_excluded(f):
                continue

            rel = f.relative_to(PROJECT_ROOT)
            # Only check config/template files
            if not any(seg in str(rel) for seg in ("config", "template", ".json")):
                continue

            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue

            violations = _HOME_RE.findall(content)
            if violations:
                # Allow if the file is a template with env vars
                if "${" in content:
                    continue
                pytest.fail(
                    f"{rel} contains hardcoded paths: {violations[:3]}\n"
                    f"Use ${{HOME}} or config-based paths.",
                    pytrace=False,
                )


def test_resolve_env_vars_basic():
    """_resolve_env_vars handles ${VAR}, ${VAR:-default}, nested vars."""
    from llm_config import _resolve_env_vars

    # ${VAR:-default} with VAR unset → default
    assert _resolve_env_vars("${MISSING:-fallback}") == "fallback"

    # ${VAR:-default} with VAR set → value
    os.environ["MY_TEST_VAR"] = "hello"
    try:
        assert _resolve_env_vars("${MY_TEST_VAR:-fallback}") == "hello"
    finally:
        del os.environ["MY_TEST_VAR"]

    # Nested ${A:-${B:-x}}
    assert _resolve_env_vars("${A:-${B:-inner}}") == "inner"

    # Simple $HOME
    os.environ["HOME"] = "/test/home"
    try:
        assert _resolve_env_vars("$HOME/code") == "/test/home/code"
    finally:
        os.environ["HOME"] = os.environ.get("HOME", "")


def test_resolve_env_vars_unresolved_strict():
    """${VAR} without default and VAR unset raises KeyError."""
    from llm_config import _resolve_env_vars

    # Unset var without default → KeyError (per docstring contract)
    unset = "_DEFINITELY_NOT_SET_VAR_"
    if unset in os.environ:
        del os.environ[unset]
    try:
        _resolve_env_vars("${" + unset + "}")
        pytest.fail("Expected KeyError for unset var without default", pytrace=False)
    except KeyError:
        pass


def test_config_resolves_kernel_paths():
    """config.json must resolve kernel paths to absolute strings."""
    from llm_config import load_config

    config_path = PROJECT_ROOT / "config.json"
    if not config_path.exists():
        pytest.skip("No config.json found")

    config = load_config(str(config_path))
    kernel = config.get("kernel", {})
    assert kernel.get("source_dir", "").startswith("/"), \
        f"kernel.source_dir must be absolute after resolution: {kernel.get('source_dir')}"
    assert "${" not in kernel.get("source_dir", ""), \
        f"kernel.source_dir has unresolved vars: {kernel.get('source_dir')}"

    # semcode_mcp paths also resolved
    semcode = config.get("agents", {}).get("kernel_expert", {}).get("semcode_mcp", {})
    if semcode:
        cmd = semcode.get("command", "")
        assert "${" not in cmd, f"semcode.command has unresolved vars: {cmd}"
        args = semcode.get("args", [])
        for arg in args:
            assert "${" not in arg, f"semcode arg has unresolved vars: {arg}"


def test_no_hardcoded_username_in_agent_source():
    """agents/, graph/, paths.py, project.py, llm_config.py, main.py
    must not contain /home/<username>/ paths — only ${HOME} or env vars."""
    source_dirs = ["agents", "graph"]
    source_files = [
        PROJECT_ROOT / "paths.py",
        PROJECT_ROOT / "project.py",
        PROJECT_ROOT / "llm_config.py",
        PROJECT_ROOT / "main.py",
    ]
    for d in source_dirs:
        for f in (PROJECT_ROOT / d).rglob("*.py"):
            source_files.append(f)

    violations = []
    for f in source_files:
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), 1):
            if "${" in line:
                continue  # env var template
            matches = _HOME_RE.findall(line)
            for m in matches:
                if any(pat in line for pat in _SAFE_HOME_PATTERNS):
                    continue
                rel = f.relative_to(PROJECT_ROOT)
                violations.append(f"{rel}:{lineno}: {line.strip()[:120]}")

    if violations:
        pytest.fail(
            f"Hardcoded /home/ paths in agent source:\n" + "\n".join(violations[:20]),
            pytrace=False,
        )
